"""An inventory of the substantial material a codebase holds, in nine categories.

Two halves. The deterministic one turns the model's answer into scored fields: pure functions,
with no I/O, no clock and no randomness, so the same answer always scores the same. The calling
one, `collect`, sends the one prompt through the provider seam and hands what comes back to the
first.
"""

from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path

from hazina_review import prompts, sentences
from hazina_review.providers.ask import Turn, ask, census_answer

CATEGORIES = (
    "complex_logic",
    "substantial_features",
    "defect_repairs",
    "work_units",
    "net_new_capabilities",
    "repo_evolutions",
    "performance_work",
    "hardening_work",
    "integration_work",
)

#: What can be read in the code today counts in full. Work known only from the commit log
#: counts for less: it backs up what the code shows and cannot stand in for it.
WEIGHTS = {
    "complex_logic": 1.0,
    "work_units": 1.0,
    "net_new_capabilities": 1.0,
    "substantial_features": 0.7,
    "defect_repairs": 0.7,
    "repo_evolutions": 0.6,
    "hardening_work": 0.5,
    "integration_work": 0.5,
    "performance_work": 0.5,
}
TESTED_REPAIR_BONUS = 0.4
_BANDS = ((0.5, 0), (3.0, 1), (7.0, 2), (14.0, 3), (28.0, 4), (55.0, 5))

#: A far-off ceiling. Reaching it is a fact the block states; nothing is cut to fit it.
SANITY_CAP = 400
MAX_THEMES = 5
THEME_CHARS = 70

#: Why a turn produced no answer. The vocabulary is fixed, word for word, so the same failure
#: is always filed under the same name. Each kind has one sentence of this tool's
#: own, which is what the block says about it in prose.
FAILURE_KINDS = (
    "rate_limited",
    "auth",
    "model_unavailable",
    "timeout",
    "no_json",
    "crashed",
    "cli_missing",
    "unknown",
)
HUMAN = {
    "rate_limited": "the API throttled or capped the request",
    "auth": "authentication was refused",
    "model_unavailable": "the requested model was refused or does not exist",
    "timeout": "the census exceeded its time budget",
    "no_json": "the census returned no parseable JSON",
    "crashed": "the CLI died before producing a result",
    "cli_missing": "the CLI could not be found or started",
    "unknown": "the CLI failed for a reason it did not explain",
}
#: What a failed turn's own words say happened, tried in this order against the end of what it
#: printed, and whether a second try could go differently. The order matters: an expired
#: credential printed beside the word "limit" is a credential problem, and sending it again
#: would only fail again. The kinds are a closed list with no name for a connection that
#: dropped, so that is filed as `crashed` and marked worth another try, while a process that
#: faulted is `crashed` and is not.
_KIND_RULES: tuple[tuple[re.Pattern, str, bool], ...] = (
    (
        re.compile(
            r"invalid[ _-]?api[ _-]?key|authentication_error|permission_error|unauthorized|"
            r"\b40[13]\b|not logged ?in|please run /?login|oauth token|"
            r"credit balance is too low",
            re.I,
        ),
        "auth",
        False,
    ),
    (
        re.compile(
            r"rate[ _-]?limit|\b429\b|too many requests|overloaded|\b529\b|quota|usage limit",
            re.I,
        ),
        "rate_limited",
        True,
    ),
    (
        re.compile(
            r"model[ _-]?not[ _-]?found|not_found_error|unknown model|invalid model|"
            r"model .{0,40}?(?:not (?:found|available|supported)|does not exist)|\b404\b",
            re.I,
        ),
        "model_unavailable",
        False,
    ),
    (
        re.compile(
            r"econnreset|etimedout|eai_again|enotfound|socket hang ?up|fetch failed|"
            r"network error|connection (?:reset|closed|refused|error)|"
            r"\b50[0234]\b|bad gateway|service unavailable|gateway time-?out|"
            r"internal server error|api_error|apiconnection",
            re.I,
        ),
        "crashed",
        True,
    ),
    (
        re.compile(
            r"segmentation fault|core dumped|bus error|stack overflow|abort(?:ed)?\b|"
            r"out of memory|heap limit|fatal error|panic:",
            re.I,
        ),
        "crashed",
        False,
    ),
)
#: How far back from the end of what was printed the rules above look.
CLASSIFY_CHARS = 4000
#: How much of each stream the block keeps as evidence, and how long the evidence may be.
DETAIL_TAIL = 400
DETAIL_LIMIT = 1000

#: Retries. Three turns at most; a wait before each retry that doubles from five seconds, is
#: never more than thirty, is spread over half to one and a half times itself so that many
#: runs refused together do not come back together, and adds up to no more than forty-five.
#: A retry is started only when, after the wait, the lane still has the smaller of two
#: minutes and a quarter of what it was given.
MAX_ATTEMPTS = 3
FIRST_WAIT = 5.0
LONGEST_WAIT = 30.0
ALL_WAITS = 45.0
WORTH_STARTING = 120.0
WORTH_STARTING_SHARE = 0.25

#: The caller's reasons for a turn it never took. A full run takes the turn in each of these
#: cases, so none has a kind of its own: each is filed under the nearest one, and the
#: sentence beside it says what happened.
NO_TURN = {
    "nothing_to_read": ("unknown", "no committed file was there to read, so no turn was taken"),
    "copy_incomplete": (
        "unknown",
        "the scratch copy of the repository came out incomplete, so no turn was taken",
    ),
}


def died_on_a_signal(status: int | None) -> bool:
    """An exit status that says the process was killed: negative from Python, 129 to 165 from a
    shell, or a Windows exception code."""
    if status is None:
        return False
    return status < 0 or 128 < status < 166 or status >= 0xC0000000


def classify(returncode: int | None, stdout: str, stderr: str) -> tuple[str, bool]:
    """`(kind, worth another try)` from the last words the command printed, and from how it
    exited when those words name nothing."""
    said = f"{stderr}\n{stdout}"[-CLASSIFY_CHARS:]
    for pattern, kind, again in _KIND_RULES:
        if pattern.search(said):
            return kind, again
    return ("crashed" if died_on_a_signal(returncode) else "unknown"), False


def _evidence(text: str) -> str:
    """`text` masked, masked a second time if the first pass left a shape behind, and withheld
    whole if even that did not settle it; then cut to `DETAIL_LIMIT`."""
    masked, _ = sentences.redact(text)
    if sentences.contains_leak(masked):
        masked, _ = sentences.redact(masked)
    if sentences.contains_leak(masked):
        return "[detail withheld: masking could not clear it]"
    return masked[:DETAIL_LIMIT]


def tails(stdout, stderr) -> str:
    """The last words of each stream, error stream first, masked: the evidence a failure kind
    was read from. Empty when neither stream said anything."""
    parts = [
        f"{label} tail: {text[-DETAIL_TAIL:]}"
        for label, text in (
            ("standard error", (stderr or "").strip()),
            ("standard output", (stdout or "").strip()),
        )
        if text
    ]
    return _evidence(" | ".join(parts)) if parts else ""


def envelope_error(stdout: str) -> str:
    """The complaint inside an envelope the command marked as an error, or ''. A command that
    was refused can still exit cleanly, and its refusal is then only here."""
    try:
        envelope = json.loads(stdout)
    except (ValueError, TypeError, RecursionError):
        return ""
    if not isinstance(envelope, dict):
        return ""
    if envelope.get("is_error"):
        said = envelope.get("result")
        return said if isinstance(said, str) else json.dumps(envelope)[:DETAIL_LIMIT]
    error = envelope.get("error")
    if isinstance(error, dict):
        said = error.get("message")
        return said if isinstance(said, str) else json.dumps(error)[:DETAIL_LIMIT]
    return ""


def read_turn(turn: Turn, provider: str, seconds: float):
    """One turn as `(answer, kind, worth another try, evidence, exit status)`. The answer is an
    object and the kind None only when the census came back readable."""
    if turn.ended == "timeout":
        evidence = tails(turn.stdout, turn.stderr) or _evidence(
            f"the CLI printed nothing in the {seconds:.0f} seconds it was given, so nothing "
            "it said can explain the stall"
        )
        return None, "timeout", False, evidence, None
    if turn.ended in ("missing", "unstartable"):
        error = turn.error
        kind = "cli_missing" if turn.ended == "missing" else "crashed"
        said = f"{type(error).__name__}: {getattr(error, 'strerror', None) or ''}"
        return None, kind, False, _evidence(said), None
    if turn.returncode != 0:
        kind, again = classify(turn.returncode, turn.stdout, turn.stderr)
        return None, kind, again, tails(turn.stdout, turn.stderr), turn.returncode
    answer = census_answer(turn.stdout, provider)
    if answer is not None:
        return answer, None, False, "", turn.returncode
    kind, again = "no_json", False
    refusal = envelope_error(turn.stdout)
    if refusal:
        named, named_again = classify(0, refusal, "")
        if named != "unknown":
            kind, again = named, named_again
    return None, kind, again, tails(turn.stdout, turn.stderr), turn.returncode


def worth_starting(budget: float) -> float:
    """How much of the lane must be left for a retry to be worth starting."""
    return min(WORTH_STARTING, WORTH_STARTING_SHARE * max(1.0, budget))


def wait_before(attempt: int, waited: float, jitter) -> float:
    """The wait before the retry that follows attempt number `attempt`."""
    wait = min(LONGEST_WAIT, FIRST_WAIT * 2 ** (attempt - 1)) * (0.5 + jitter())
    return max(0.0, min(wait, ALL_WAITS - waited))


def error_line(kind: str, returncode, attempts: int, budget: int, provider: str) -> str:
    said = [f"census failed ({kind}): {HUMAN.get(kind, HUMAN['unknown'])}"]
    if returncode is not None:
        said.append(f"{provider} exited {returncode}")
    if kind == "timeout":
        said.append(f"timed out after {budget}s")
    return "; ".join([*said, f"{attempts} attempt(s)", "criteria unscored"])


#: The one name this block goes by, on a block that scored and on one that did not.
PROBE = "material_census"
COULD_NOT_ASSESS_CHARS = 300

#: Every label the block may carry for what the masking took out.
REMOVED_KINDS = sentences.LABELS


def _clamp(value, lo: int, hi: int, default: int = 0) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def entries(value):
    """What an answer's field holds, walked the way scoring walks it. Anything falsy is
    nothing; a string is its characters and a mapping its keys, as iterating them gives. A
    value that cannot be walked at all holds nothing."""
    value = value or []
    try:
        return list(value)
    except TypeError:
        return []


def _clean(examples) -> tuple[list[str], list[str]]:
    """The entries that may go out, masked, and the labels of what was masked in them.

    Each string is masked and then judged; one that fails is dropped whole and what was masked
    in it is not counted. At most `SANITY_CAP` are kept.
    """
    kept: list[str] = []
    removed: set[str] = set()
    for entry in entries(examples):
        if not isinstance(entry, str) or not entry.strip():
            continue
        masked, labels = sentences.redact(entry)
        if not masked or sentences.validate_sentence(masked):
            continue
        kept.append(masked)
        removed.update(labels)
        if len(kept) >= SANITY_CAP:
            break
    return kept, sorted(removed)


def weighted_total(counts: dict, with_test: int = 0) -> float:
    total = sum(w * int(counts.get(name) or 0) for name, w in WEIGHTS.items())
    repairs = int(counts.get("defect_repairs") or 0)
    return total + TESTED_REPAIR_BONUS * min(int(with_test or 0), repairs)


def depth_band(total: float) -> int:
    for threshold, band in _BANDS:
        if total < threshold:
            return band
    return 6


def unmeasured() -> dict:
    """Every measurement this lane produces, explicitly null. A zero says 'we looked and the
    repository holds nothing', which is a verdict about the repository. What happened was a
    verdict about the run."""
    return {
        "logic_depth": None,
        "self_contained": None,
        "minable_ideas_total": None,
        "n_minable_ideas": None,
        "defect_repairs_with_regression_test": None,
        **{f"n_{name}": None for name in CATEGORIES},
    }


def unscored(
    provider: str,
    model: str | None,
    kind: str,
    *,
    attempts: int = 1,
    returncode: int | None = None,
    again: bool = False,
    detail: str = "",
    seconds: float = 0,
    why: str | None = None,
) -> dict:
    """The whole block for a lane that produced no answer: every measurement null, the kind,
    and what happened in this tool's own sentences.

    `seconds` is what the lane was given. `detail` is the masked evidence the kind was read
    from. `why` replaces the sentence for the kind, for a turn the caller never took.
    """
    human = why or HUMAN.get(kind, HUMAN["unknown"])
    said = error_line(kind, returncode, attempts, max(1, int(seconds)), provider)
    if why is not None:
        said = said.replace(HUMAN[kind], why, 1)
    reason = f"material census unavailable: {human}"
    if kind == "timeout":
        reason += (
            f"; the lane was capped at {int(seconds)} seconds and every material measurement "
            "is reported unmeasured rather than scored low"
        )
    return {
        **unmeasured(),
        "probe": PROBE,
        "provider": provider,
        "model": model,
        "scored": False,
        "ok": False,
        "error": said,
        "census_failure_kind": kind,
        "census_retryable": bool(again),
        "census_attempts": attempts,
        "census_error_detail": detail,
        "timed_out": kind == "timeout",
        "logic_depth_unavailable_reason": reason,
    }


def no_turn(provider: str, model: str | None, reason: str, seconds: float = 0) -> dict:
    """The block for a turn the caller never took, for one of the reasons in `NO_TURN`."""
    kind, why = NO_TURN[reason]
    return unscored(provider, model, kind, attempts=0, seconds=seconds, why=why)


def score(payload: dict, attempts: int = 1) -> dict:
    """Score one answer. A category's count is the length of its surviving list, so a number
    and its evidence cannot disagree."""
    cleaned = {name: _clean(payload.get(name)) for name in CATEGORIES}
    ideas = {name: kept for name, (kept, _) in cleaned.items()}
    counts = {name: len(kept) for name, kept in ideas.items()}
    # read from the lists as received: a category this long is worth saying so about even
    # when cleaning leaves little of it
    hit_cap = any(len(entries(payload.get(name))) >= SANITY_CAP for name in CATEGORIES)
    with_test = _clamp(
        payload.get("defect_repairs_with_regression_test"), 0, counts["defect_repairs"]
    )
    total = weighted_total(counts, with_test)
    themes = []
    for entry in entries(payload.get("themes"))[:MAX_THEMES]:
        masked = sentences.redact(str(entry))[0]
        if masked:
            themes.append(masked[:THEME_CHARS])
    summary, summary_removed = sentences.redact(str(payload.get("material_summary") or ""))
    # the sentences that were kept and the summary, which are what the masking is let loose
    # on and then sent. A theme is masked too, but it is a label and not an example.
    removed = set(summary_removed).union(*(gone for _, gone in cleaned.values()))
    could_not = " ".join(str(payload.get("what_i_could_not_assess") or "").split())
    return {
        "probe": PROBE,
        "themes": themes,
        **{f"n_{name}": n for name, n in counts.items()},
        "defect_repairs_with_regression_test": with_test,
        "minable_ideas_total": round(total, 1),
        "n_minable_ideas": sum(counts.values()),
        "minable_ideas": ideas,
        "self_contained": _clamp(payload.get("self_contained"), 0, 4),
        "logic_depth": depth_band(total),
        "material_summary": summary,
        "census_hit_cap": hit_cap,
        "timed_out": False,
        "census_attempts": attempts,
        "redacted_from_examples": sorted(removed),
        # Tidied and cut, and neither masked nor judged: the boundary judges it, as one
        # sentence, and a sentence that names anything goes out empty.
        "what_i_could_not_assess": could_not[:COULD_NOT_ASSESS_CHARS],
    }


def prompt(history: Path | str = "{history}") -> str:
    """The instruction, with `history` named as the directory the brief was written to. Left
    out, the placeholder stands, which is how the text is read on its own."""
    return prompts.render("census", history=history)


def collect(
    repo: Path,
    brief_dir: Path,
    *,
    provider: str,
    model: str | None,
    timeout: float,
    sleep=None,
    jitter=None,
    now=None,
    outcome: dict | None = None,
) -> tuple[dict, dict | None]:
    """The census, as `(block, answer)`. The block is the whole `material` block, scored or not.
    The answer is the model's own words, or None when no turn came back readable. It stays out
    of the block because the block goes to the emission boundary and the answer must never
    reach it.

    `timeout` is the whole lane's, retries and waits included: each turn is given what is left
    of it. A turn that fails with a kind worth another try is sent again, within the limits
    set above; any other failure ends the lane. Either way the result is nulls with the
    failure named, and never a zero. `sleep`, `jitter` and `now` stand in for the clock and
    the random spread in tests.

    `outcome`, when given, is filled with how the lane ended for the caller's own progress
    lines and support log: the failure kind (None when it scored) and the last exit status.
    Nothing in it goes into the block.
    """
    outcome = {} if outcome is None else outcome
    sleep = sleep or time.sleep
    jitter = jitter or random.random
    now = now or time.monotonic
    asked = prompt(brief_dir)
    budget = float(max(1, int(timeout)))
    ends = now() + budget
    enough = worth_starting(budget)
    waited = 0.0
    attempts = 0
    while True:
        attempts += 1
        seconds = max(1.0, ends - now())
        turn = ask(provider, model, asked, repo, [brief_dir], seconds)
        answer, kind, again, detail, status = read_turn(turn, provider, seconds)
        if answer is not None:
            break
        wait = wait_before(attempts, waited, jitter)
        left_after = ends - now() - wait
        stopped = ""
        if not again:
            pass
        elif attempts >= MAX_ATTEMPTS:
            stopped = f"the ceiling of {MAX_ATTEMPTS} attempts was reached"
        elif left_after < enough:
            stopped = (
                f"only {max(0.0, left_after):.0f} of the {budget:.0f} second budget would "
                f"remain after backing off, below the {enough:.0f} seconds a census needs to "
                "be worth starting"
            )
        else:
            sleep(wait)
            waited += wait
            continue
        if stopped:
            detail = _evidence(
                " | ".join(part for part in (detail, f"no further attempt: {stopped}") if part)
            )
        block = unscored(
            provider,
            model,
            kind or "unknown",
            attempts=attempts,
            returncode=status,
            again=again,
            detail=detail,
            seconds=timeout,
        )
        outcome.update(kind=kind or "unknown", returncode=status)
        return block, None
    outcome.update(kind=None, returncode=status)
    block = {
        **score(answer, attempts),
        "provider": provider,
        "model": model,
        "scored": True,
        "ok": True,
    }
    return block, answer
