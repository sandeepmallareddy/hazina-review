"""Enumerate task ideas, counting a task even when its description must stay local."""

from __future__ import annotations

from pathlib import Path

from hazina_review import prompts, sentences
from hazina_review.lanes.census import NO_TURN, classify, envelope_error
from hazina_review.providers.ask import Turn, ask, mining_answer

TASK_TYPES = ("net_new", "agentic", "bug_repair", "repo_evolution")
COUNT_KEYS = ("total_candidates", *(f"n_{kind}" for kind in TASK_TYPES))
#: A full run asks for up to 150. The earlier default of 40 is not the number a measurement
#: uses, and the ceiling is in the prompt, so a lower one asks for less.
DEFAULT_N = 150
#: The fields of a task that are turned into lists of strings. One that is present, not
#: empty, and cannot be walked stops the lane.
_WALKED = ("files", "symbols", "subsystems", "source_commit_shas", "test_files")

_NONE_FOUND = (
    "the agentic census completed and enumerated no candidate that cleared the bar; "
    "these zeros are measured, not a failed lane"
)
_CEILING = (
    "the agentic census reached its ceiling of {n} ideas, so this count is a floor "
    "rather than a total; raise the ceiling to enumerate the rest"
)
_UNFINISHED = (
    "{why}; the lane did not finish, so every task-type count is reported unmeasured "
    "rather than as zero"
)


def task_type(item: dict) -> str:
    claimed = str(item.get("task_type") or item.get("strategy") or "").strip().lower()
    return claimed if claimed in TASK_TYPES else "agentic"


def description(item: dict) -> str:
    """The task's one line as the model wrote it, or "" when it may not be sent."""
    text = str(item.get("summary") or item.get("why_interesting") or "").strip()
    return "" if not text or sentences.validate_sentence(text) else text


def items(answer) -> list[dict]:
    """The tasks in an answer: a lone object is one task, and anything not an object is none."""
    listed = answer if isinstance(answer, list) else [answer]
    return [item for item in listed if isinstance(item, dict)]


def _unwalkable(item: dict) -> bool:
    for key in _WALKED:
        value = item.get(key, []) or []
        try:
            iter(value)
        except TypeError:
            return True
    return False


def score(answer, n: int = DEFAULT_N) -> dict:
    """Every task returned counts once, as in a full run, even past the
    ceiling the request named; omitted descriptions do not erase tasks. Reaching the ceiling
    is reported."""
    if type(n) is not int or n < 1:
        raise ValueError("the task ceiling must be a positive integer")
    tasks = items(answer)
    counts = {f"n_{kind}": 0 for kind in TASK_TYPES}
    summaries = []
    for item in tasks:
        kind = task_type(item)
        counts[f"n_{kind}"] += 1
        sentence = description(item)
        if sentence:
            summaries.append(f"{kind}: {sentences.scrub(sentence)}")
    block = {
        "total_candidates": len(tasks),
        **counts,
        "scored": bool(tasks),
        "mined_task_summaries": summaries,
    }
    if not tasks:
        block["mine_unavailable_reason"] = sentences.scrub(_NONE_FOUND)
    elif len(tasks) >= n:
        block["mine_unavailable_reason"] = sentences.scrub(_CEILING.format(n=n))
    return block


#: How much of the error stream a failed turn's sentence quotes.
STDERR_QUOTED = 300


def why_stopped(turn: Turn, provider: str, seconds: float) -> str:
    """What stopped the lane, said in a fixed form for each event: the
    exit status and the start of the error stream, the ceiling a turn ran into, or that the
    command was not there. The block masks it before it goes anywhere."""
    if turn.ended == "timeout":
        return f"{provider} agent timed out after {int(seconds)}s"
    if turn.ended == "missing":
        return f"{provider} CLI not found on PATH"
    if turn.ended == "unstartable":
        return f"{provider} agent could not run: {turn.error}"
    if turn.returncode != 0:
        return f"{provider} exited {turn.returncode}: {turn.stderr.strip()[:STDERR_QUOTED]}"
    return f"{provider} agent completed, but could not parse JSON from the agent's output."


def unscored(provider: str, model: str | None, why: str) -> dict:
    """The block for a lane that did not finish: null counts, and `why` in the sentence."""
    return {
        **dict.fromkeys(COUNT_KEYS),
        "provider": provider,
        "model": model,
        "scored": False,
        "mined_task_summaries": [],
        "mine_unavailable_reason": sentences.scrub(_UNFINISHED.format(why=why)),
    }


def no_turn(provider: str, model: str | None, reason: str) -> dict:
    """The block for a turn the caller never took, for one of the census's `NO_TURN` reasons."""
    return unscored(provider, model, NO_TURN[reason][1])


#: A task that cannot be converted stops the miner, and a full run reports the exit and the
#: start of the trace.
UNCONVERTIBLE = "agentic mining exited 1: Traceback (most recent call last): TypeError"


def prompt(n: int, history: Path | str) -> str:
    """The instruction for a ceiling of `n` tasks and a brief under `history`."""
    return prompts.render("mining", n=n, history=history)


def failure_kind(turn: Turn) -> str:
    """What stopped a turn that gave no task list, filed under the census's kinds and read the
    census's way, for the caller's progress lines. The block keeps its own sentence."""
    if turn.ended == "timeout":
        return "timeout"
    if turn.ended in ("missing", "unstartable"):
        return "cli_missing" if turn.ended == "missing" else "crashed"
    if turn.returncode != 0:
        return classify(turn.returncode, turn.stdout, turn.stderr)[0]
    refusal = envelope_error(turn.stdout)
    named = classify(0, refusal, "")[0] if refusal else "unknown"
    return named if named != "unknown" else "no_json"


def collect(
    repo: Path,
    brief_dir: Path,
    *,
    provider: str,
    model: str | None,
    timeout: float,
    n: int = DEFAULT_N,
    outcome: dict | None = None,
) -> tuple[dict, list | dict | None]:
    """The task list, as `(block, answer)`. `outcome`, when given, is filled as the census
    fills it: the failure kind (None when the list was read) and the exit status."""
    outcome = {} if outcome is None else outcome
    turn = ask(provider, model, prompt(n, brief_dir), repo, [brief_dir], timeout)
    outcome.update(kind=None, returncode=turn.returncode)
    answer = mining_answer(turn.stdout, provider) if turn.ended == "exited" else None
    if turn.ended != "exited" or turn.returncode != 0 or answer is None:
        outcome["kind"] = failure_kind(turn)
        return unscored(provider, model, why_stopped(turn, provider, timeout)), None
    if any(_unwalkable(item) for item in items(answer)):
        outcome["kind"] = "unknown"
        return unscored(provider, model, UNCONVERTIBLE), answer
    return {**score(answer, n), "provider": provider, "model": model}, answer
