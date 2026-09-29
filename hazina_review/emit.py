"""What may leave the machine from a review, declared leaf by leaf.

Three steps, in this order, before either block is written. The declaration: every field is
held to the kind it was declared as, an undeclared one stops the run, and a line of prose that
names anything goes out empty. The masking, on the `material` block only: every string that is
not from a closed vocabulary is masked once more. The audit: every string that is not exempt is
looked at again, and one that still names something stops the run.

The kinds, the prose rules and the masking are the review's own (`hazina_review.sentences`),
built to give the expected result for the same block. Step 1's schema supplies the
generic containers only.
"""

from __future__ import annotations

from hazina_review import sentences
from hazina_review.lanes import mining
from hazina_review.lanes.census import CATEGORIES, FAILURE_KINDS, PROBE, REMOVED_KINDS
from hazina_review.providers.registry import PROVIDERS
from hazina_scan.schema import (
    BOOL,
    HANDLE,
    TIMESTAMP,
    VERSION,
    EmissionRefused,
    Enum,
    ListOf,
    Number,
    Object,
    Prose,
    Token,
)

NUMBER = Number()
MODEL_ID = Token("model id", r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,80}")
#: The one field this tool adds to step 1's row: 32 lowercase hex characters (`record`).
RECORD_ID = Token("record id", r"[0-9a-f]{32}")

#: The words this tool's own notes may use without being read as something from a repository.
#: The list is fixed, so the same note is always judged the same way.
OWN_WORDS = frozenset(
    {
        *PROVIDERS,
        "--no-llm",
        "--no-mine",
        "--build",
        "--review",
        "--model",
        "--provider",
        "CLI",
        "JSON",
        "API",
        "PATH",
        "LOC",
        "KB",
        "OK",
        "llm_disabled",
        "mine_disabled",
        "rate_limited",
        "model_unavailable",
        "cli_missing",
        "no_json",
        "logic_depth",
        "self_contained",
        "material_census",
        "git_history",
        "code_structure",
        "mined_task_summaries",
        "task_type_counts",
        "measurement.json",
        "codebase_repos.json",
        "codebase_repos.csv",
        "codebase_repo_mining.json",
        "REPO_INTRINSIC",
        "ENVIRONMENT",
        "TIMEOUT",
        "UNCLASSIFIED",
        "NONE",
        "ImportError",
        "ModuleNotFoundError",
        "OSError",
        "FileNotFoundError",
        "PermissionError",
        "TimeoutExpired",
        "JSONDecodeError",
        "ValueError",
        "RuntimeError",
        "MemoryError",
        "UnicodeDecodeError",
        "NotADirectoryError",
        "IsADirectoryError",
    }
)


class Line(Prose):
    """One line of prose under the review's rules, emptied whole when any rule refuses it."""

    def apply(self, value, where):
        if value is None:
            return None
        if not isinstance(value, str):
            raise EmissionRefused(f"{where}: expected a line of prose")
        if not value.strip():
            return ""
        if sentences.rejection(value, self.max_words, self.allow):
            return ""
        return " ".join(value.split())


class TaggedSentence(Prose):
    """`<task type>: <sentence>`. The tag must be one of ours; the sentence is judged alone."""

    def __init__(self):
        super().__init__(sentences.MAX_SENTENCE_WORDS)

    def apply(self, value, where):
        if value is None:
            return None
        if not isinstance(value, str):
            raise EmissionRefused(f"{where}: expected a tagged task sentence")
        tag, _, sentence = value.partition(": ")
        if tag not in mining.TASK_TYPES:
            raise EmissionRefused(f"{where}: unknown task tag")
        return "" if sentences.validate_sentence(sentence) else f"{tag}: {sentence}"


SENTENCE = Line(sentences.MAX_SENTENCE_WORDS)
SUMMARY = Line(120, label="summary")
OWN_PROSE = Line(120, allow=OWN_WORDS, label="tool note")

MATERIAL = Object(
    {
        "task_type_counts": Object({name: NUMBER for name in mining.COUNT_KEYS}),
        "probe": Enum("probe", (PROBE,)),
        "provider": Enum("provider", PROVIDERS),
        "model": MODEL_ID,
        "scored": BOOL,
        "ok": BOOL,
        "error": OWN_PROSE,
        "logic_depth_unavailable_reason": OWN_PROSE,
        "census_platform_note": OWN_PROSE,
        "timed_out": BOOL,
        "census_hit_cap": BOOL,
        "census_failure_kind": Enum("census failure", FAILURE_KINDS),
        "census_retryable": BOOL,
        "census_attempts": NUMBER,
        "census_error_detail": SENTENCE,
        "logic_depth": NUMBER,
        "self_contained": NUMBER,
        "minable_ideas_total": NUMBER,
        "n_minable_ideas": NUMBER,
        "defect_repairs_with_regression_test": NUMBER,
        **{f"n_{name}": NUMBER for name in CATEGORIES},
        "themes": ListOf(SENTENCE),
        # An object and not a map: the nine names are this tool's own, so a tenth can only be
        # a bug, and a map would fold it under another key and write it.
        "minable_ideas": Object({name: ListOf(SENTENCE) for name in CATEGORIES}),
        "material_summary": SUMMARY,
        "what_i_could_not_assess": SENTENCE,
        # Words of this tool's own choosing about what was masked, and never the thing masked.
        "redacted_from_examples": ListOf(
            Enum("kind of thing removed", REMOVED_KINDS, unknown="other")
        ),
    }
)

MINING = Object(
    {
        "repo_id": HANDLE,
        "measurer_version": VERSION,
        "provider": Enum("provider", PROVIDERS),
        "model": MODEL_ID,
        "mined_at": TIMESTAMP,
        **{name: NUMBER for name in mining.COUNT_KEYS},
        "scored": BOOL,
        "mine_unavailable_reason": OWN_PROSE,
        "mined_task_summaries": ListOf(TaggedSentence()),
    }
)

#: Fields whose value was matched against a closed vocabulary or a pattern. Masking them again
#: could only damage them, and auditing them would refuse what they exist to carry.
_MATERIAL_EXEMPT = frozenset(
    {"probe", "provider", "model", "census_failure_kind", "redacted_from_examples"}
)
_MINING_EXEMPT = frozenset(
    {"repo_id", "measurer_version", "provider", "model", "mined_at", "mined_task_summaries"}
)


def _mask(node, exempt: frozenset[str], key: str | None = None):
    """Every string under `node` scrubbed, except under an exempt key."""
    if isinstance(node, dict):
        return {name: _mask(value, exempt, name) for name, value in node.items()}
    if isinstance(node, list):
        return [_mask(value, exempt, key) for value in node]
    if isinstance(node, str) and key not in exempt:
        return sentences.scrub(node)
    return node


def audit(node, exempt: frozenset[str], where: str, key: str | None = None) -> None:
    """Refuse the write if any string not under an exempt key still names something."""
    if isinstance(node, dict):
        for name, value in node.items():
            audit(value, exempt, f"{where}.{name}", name)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            audit(value, exempt, f"{where}[{index}]", key)
    elif isinstance(node, str) and key not in exempt:
        found = sentences.leaks(node)
        if found:
            raise EmissionRefused(f"refusing to write: {where} still holds {found}")


def record_id(value) -> str:
    """The row's `record_id`, as declared. It is always present: an empty one is refused too."""
    out = RECORD_ID.apply(value, "codebase_repos.record_id")
    if out is None:
        raise EmissionRefused("codebase_repos.record_id: expected a record id, got nothing")
    return out


def material_block(block: dict) -> dict:
    out = _mask(MATERIAL.apply(block, "material"), _MATERIAL_EXEMPT)
    audit(out, _MATERIAL_EXEMPT, "material")
    return out


def mining_block(block: dict) -> dict:
    out = MINING.apply(block, "mining")
    audit(out, _MINING_EXEMPT, "mining")
    return out
