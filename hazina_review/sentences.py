"""The review's own rules for model-written text: what is masked, what may leave, what is audited.

Three layers, each a pure function over a string. `redact` masks the shapes that name something
in a repository and says, in a fixed set of labels, which shapes it found. `rejection` says why
a line may not be sent as it stands, or nothing when it may. `scrub` and `leaks` are the pair
run over a finished block: the first masks every free string once more, the second proves
nothing that names anything got through, and a finding from it stops the write.

These are the review's rules and not step 1's. Step 1 masks and judges its own documents with
`hazina_scan.redact` and `hazina_scan.schema`, and nothing here changes what they do.
"""

from __future__ import annotations

import re

from hazina_scan.vocab import TECH_NAMES

MAX_SENTENCE_WORDS = 40
#: Longer than this and a masked line is cut at a word, with an ellipsis. Never a reason to drop.
MAX_CHARS = 2000

#: Walked in this order, each over what the one before left behind. The third item is the
#: label the block may carry for it; the list of labels is closed.
_MASKS: tuple[tuple[re.Pattern, str, str], ...] = (
    (re.compile(r"\b(?:[\w.\-]+[/\\])+[\w.\-]+\b"), "[path]", "a filesystem path"),
    (
        re.compile(
            r"\b[\w\-]+\.(?:py|js|jsx|mjs|cjs|ts|tsx|go|java|rb|php|cs|rs|kt|kts|swift|"
            r"scala|c|cc|cpp|h|hpp|m|mm|sh|sql|yml|yaml|toml|json)\b"
        ),
        "[file]",
        "a filename",
    ),
    (re.compile(r"\b[0-9a-f]{7,40}\b"), "[revision]", "an object hash"),
    (re.compile(r"\b\w+\([^)]{0,80}\)"), "[function]", "a function call"),
    (re.compile(r"(?:^|\n)[ \t]*(?:def|class)\s+\w+\s*[(:].*"), "[declaration]", "a declaration"),
    (re.compile(r"(?:^|\n)[ \t]*import\s+[\w.]+.*"), "[import]", "an import statement"),
    (re.compile(r"(?:^|\n)[ \t]*from\s+[\w.]+\s+import\b.*"), "[import]", "an import statement"),
    (re.compile(r"\bfunction\s+\w+\s*\([^)]*\)"), "[function]", "a function declaration"),
    (re.compile(r"[{}]|=>|->|::|`"), "", "code punctuation"),
    (re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b"), "[identifier]", "a snake_case identifier"),
    (re.compile(r"\b[a-z]+(?:[A-Z][a-z0-9]*)+\b"), "[identifier]", "a camelCase identifier"),
)
_JOINED_CAPITALS = re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z0-9]*)+\b")
_ONE_CAPITALISED_WORD = re.compile(r"[A-Z][a-z]{2,}")
_RUN_OF_SPACE = re.compile(r"\s{2,}")
_REPEATED_PLACEHOLDER = re.compile(r"(\[\w+\])(\s+\1)+")

#: Every label `redact` can name, in the order a reader meets them.
LABELS = (
    *dict.fromkeys(label for _pattern, _mask, label in _MASKS),
    "a class name",
    "a proper name",
)


def _cut(text: str) -> str:
    if len(text) <= MAX_CHARS:
        return text
    return text[:MAX_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:") + "..."


def redact(text) -> tuple[str, list[str]]:
    """`(masked text, sorted labels of what was masked)`. Masks; never refuses."""
    if not text:
        return "", []
    out = " ".join(str(text).split())
    found: list[str] = []
    for pattern, mask, label in _MASKS:
        if pattern.search(out):
            found.append(label)
            out = pattern.sub(mask, out)

    def joined(hit: re.Match) -> str:
        if hit.group(0) in TECH_NAMES:
            return hit.group(0)
        found.append("a class name")
        return "[identifier]"

    out = _JOINED_CAPITALS.sub(joined, out)
    if _ONE_CAPITALISED_WORD.fullmatch(out) and out not in TECH_NAMES:
        found.append("a proper name")
        out = "[identifier]"
    out = _RUN_OF_SPACE.sub(" ", out).strip()
    out = _REPEATED_PLACEHOLDER.sub(r"\1", out)
    return _cut(out), sorted(set(found))


def contains_leak(text: str) -> str | None:
    """The label of the first shape `redact` would still mask in `text`, or None."""
    if not text:
        return None
    for pattern, _mask, label in _MASKS:
        if pattern.search(text):
            return label
    if any(word not in TECH_NAMES for word in _JOINED_CAPITALS.findall(text)):
        return "a class name"
    if _ONE_CAPITALISED_WORD.fullmatch(text) and text not in TECH_NAMES:
        return "a proper name"
    return None


_ADDRESS = re.compile(r"\b[\w.\-+]+@[\w.\-]+\.[A-Za-z]{2,}\b")
_SHOUTED = re.compile(r"\b(?:[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+|[A-Z][A-Z0-9]+)\b")


def scrub(text: str) -> str:
    """`redact`, then addresses and words in capitals, each masked in its place."""
    out = redact(text)[0]
    return _SHOUTED.sub("[identifier]", _ADDRESS.sub("[email]", out))


def leaks(text: str) -> str | None:
    """What `scrub` would still find in `text`, named, or None when there is nothing."""
    found = contains_leak(text)
    if found:
        return found
    if _ADDRESS.search(text):
        return "an email address"
    if _SHOUTED.search(text):
        return "an uppercase identifier"
    return None


#: What stops a line being sent, most specific first, each with the reason given for it.
#: A quotation is two straight apostrophes up to eighty characters apart, whatever is in front
#: of the first, so two contractions close together read as one.
_REFUSALS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"[/\\]"), "it has a path separator"),
    (re.compile(r"https?:|\bwww\."), "it has a link"),
    (re.compile(r"@"), "it has an address or a handle"),
    (re.compile(r"[A-Za-z_]\w*\.[A-Za-z_]"), "it has a dotted name or a file name"),
    (re.compile(r"(?:^|\s)\.[A-Za-z0-9]{1,6}\b"), "it has a file extension"),
    (re.compile(r"[A-Za-z0-9]+_[A-Za-z0-9]+"), "it has a name joined by underscores"),
    (re.compile(r"\b[a-z]+[A-Z][A-Za-z0-9]*\b"), "it has a capital inside a word"),
    (re.compile(r"[\"`]|'[^']{1,80}'"), "it has a quotation"),
    (re.compile(r"[{}\[\]<>=|*#$]|::|->|=>"), "it has code punctuation"),
    (re.compile(r"\b[0-9a-f]{7,}\b"), "it has an object hash"),
    (re.compile(r"\b[A-Z]{2,}[A-Z0-9]*\b"), "it has a word in capitals"),
)
#: Refused only in text a model wrote. This tool's own notes use them, and are marked as its
#: own by being judged with a non-empty `allow`.
_BRACKETS = (re.compile(r"[()]"), "it has code punctuation")
_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]*")


def rejection(
    text, max_words: int = MAX_SENTENCE_WORDS, allow: frozenset[str] = frozenset()
) -> str | None:
    """Why `text` may not be sent as a line of prose, or None when it may.

    Each word in `allow` is replaced before any rule is tried, longest first, so a note this
    tool wrote is not refused for its own vocabulary. A capital is ordinary only at the start
    or after a full stop, a question mark or an exclamation mark, or on a public technology.
    """
    if not isinstance(text, str) or not text.strip():
        return "it is empty"
    probe = " ".join(text.split())
    words = len(probe.split(" "))
    if words > max_words:
        return f"it runs to {words} words, more than {max_words}"
    for token in sorted(allow, key=len, reverse=True):
        probe = probe.replace(token, "ok")
    for pattern, reason in (*_REFUSALS, _BRACKETS) if not allow else _REFUSALS:
        if pattern.search(probe):
            return reason
    for hit in _WORD.finditer(probe):
        word = hit.group(0)
        if not word[:1].isupper() or word in TECH_NAMES:
            continue
        before = probe[: hit.start()].rstrip()
        if before and before[-1] not in ".!?":
            return f"it has the capitalised name {word!r}"
    return None


def validate_sentence(text) -> str | None:
    """`rejection` for one line a model wrote."""
    return rejection(text, MAX_SENTENCE_WORDS)
