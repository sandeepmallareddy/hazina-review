"""The one place a provider command is started."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from hazina_review.providers.registry import AUTH_VARS, PROVIDERS, isolation_flags, resolve
from hazina_scan.env import MODEL, build_env, run

#: Named to the command as the tools it may use without asking, and a headless turn has nobody
#: to ask. All three read. This is a permission and not a removal: what takes away the tools
#: that run commands, and holds the file tools to the directories named with `--add-dir`, is
#: `--restricted`, one of the flags the registry will not start the command without.
TOOLS = "Read Grep Glob"

#: A command that was signed in with its own login keeps that login under the operator's home
#: directory, so the model domain is the one that keeps the real one.
_HOME_VARS = ("HOME", "USERPROFILE")
#: Where each command's JSON envelope keeps the model's own words. A command with no entry
#: writes its final message to a file, which is read as it stands.
ANSWER_KEYS = {PROVIDERS[0]: "result"}


def build_argv(
    executable: str,
    model: str | None,
    prompt: str,
    read_dirs: list[Path],
    flags: list[str],
    *,
    provider: str = "claude",
    output_path: Path | None = None,
) -> list[str]:
    """Build an argument list without invoking a shell; the second CLI reads prompt on stdin."""
    if provider == "codex":
        if output_path is None or model is None:
            raise ValueError("this provider needs an output path and an explicit model")
        # Its --add-dir expands writable roots. In read-only mode the history is named in the
        # prompt. The CLI writes its final answer to this file.
        return [
            executable,
            "exec",
            *flags,
            "--model",
            model,
            "--output-last-message",
            str(output_path),
            "-",
        ]
    argv = [executable, "-p", prompt, "--output-format", "json", "--allowedTools", TOOLS]
    for d in read_dirs:
        argv += ["--add-dir", str(d)]
    if model:
        argv += ["--model", model]
    return argv + flags


def _decode(chunk: str):
    """`chunk` as JSON, or `_NOTHING` when it is not JSON. Nesting too deep to decode is not
    JSON either."""
    try:
        return json.loads(chunk)
    except (ValueError, RecursionError):
        return _NOTHING


_NOTHING = object()
#: A fenced block holding one object, found anywhere in a reply; the census reads only this.
_FENCED_OBJECT = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
#: A fenced block holding anything, found anywhere in a reply; the task list reads this.
_FENCED_ANYTHING = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _widest(text: str, opener: str, closer: str) -> str | None:
    """From the first `opener` to the last `closer`, or None when there is no such span."""
    first, last = text.find(opener), text.rfind(closer)
    return text[first : last + 1] if 0 <= first < last else None


def census_answer(stdout: str, provider: str):
    """The census object in what the command printed, or None.

    The envelope is opened when there is one: an object under its answer key is the answer
    itself, and a string there is the text searched. Anything else, including a command that
    prints no envelope, has the whole of what it printed searched. Two places are looked in,
    in order: the first fenced block holding an object, then everything from the first `{` to
    the last `}`. What a fence held that does not decode is passed over, and what the span
    held that does not decode ends the search. Only an object can come back.
    """
    text = stdout or ""
    key = ANSWER_KEYS.get(provider)
    envelope = _decode(text) if key else _NOTHING
    if isinstance(envelope, dict):
        inner = envelope.get(key)
        if isinstance(inner, dict):
            return inner
        if isinstance(inner, str):
            text = inner
    fenced = _FENCED_OBJECT.search(text)
    if fenced:
        found = _decode(fenced.group(1))
        if found is not _NOTHING:
            return found
    span = _widest(text, "{", "}")
    if span is None:
        return None
    found = _decode(span)
    return None if found is _NOTHING else found


def json_in(text: str | None):
    """The first reading of `text` that decodes, or None: the whole of it, the first fenced
    block, everything from the first `[` to the last `]`, then from the first `{` to the last
    `}`. Any JSON value counts, a string or a number included."""
    if not text:
        return None
    text = text.strip()
    fenced = _FENCED_ANYTHING.search(text)
    candidates = (
        text,
        fenced.group(1).strip() if fenced else None,
        _widest(text, "[", "]"),
        _widest(text, "{", "}"),
    )
    for chunk in candidates:
        if chunk is not None:
            found = _decode(chunk)
            if found is not _NOTHING:
                return found
    return None


def mining_answer(text: str, provider: str):
    """The task list in what the command printed, or None when nothing in it decodes.

    Read twice. The first reading looks for the envelope, and a string under its answer key
    is what the second reading searches; otherwise the second reading searches everything that
    was printed again. So an envelope whose answer is not a string is itself the answer: one
    object, which the lane counts as one task.
    """
    envelope = json_in(text)
    key = ANSWER_KEYS.get(provider)
    if key and isinstance(envelope, dict) and isinstance(envelope.get(key), str):
        text = envelope[key]
    return json_in(text)


@dataclass(frozen=True)
class Turn:
    """What one started command left behind, for the lane to read.

    `ended` is `exited`, `timeout`, `missing` (there was nothing to start) or `unstartable`
    (it was there and would not start). `stdout` is what it printed, except that for a command
    that writes its final message to a file, and did write the file, it is that file. After a
    timeout both streams hold whatever had been printed by then. `error` is the operating
    system's complaint when the command did not start.
    """

    ended: str
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: BaseException | None = None


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value or ""


def ask(
    provider: str,
    model: str | None,
    prompt: str,
    repo: Path,
    read_dirs: list[Path],
    timeout: float,
) -> Turn:
    """Start one turn and say how it ended. Only a missing or un-isolatable command raises;
    everything else, from a timeout to a command the system would not start, is a `Turn` for
    the lane to name, so one bad turn cannot sink a whole run."""
    executable = resolve(provider)
    flags = isolation_flags(provider, executable)
    # `run` takes a caller's environment as it comes, so this is the only check it gets:
    # built from nothing, the names below and the harmless base, and refused if denied.
    env = build_env(passthrough=(*_HOME_VARS, *AUTH_VARS[provider]), domain=MODEL)
    output_path = None
    try:
        if provider == "codex":
            with tempfile.NamedTemporaryFile(
                prefix="hazina-review-codex-", suffix=".txt", delete=False
            ) as handle:
                output_path = Path(handle.name)
        argv = build_argv(
            executable,
            model,
            prompt,
            [repo, *read_dirs],
            flags,
            provider=provider,
            output_path=output_path,
        )
        input_kwargs = {"input_text": prompt} if provider == "codex" else {}
        proc = run(argv, domain=MODEL, cwd=repo, env=env, timeout=timeout, **input_kwargs)
        stdout = proc.stdout or ""
        if output_path is not None and output_path.exists():
            stdout = output_path.read_text(encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as expired:
        return Turn("timeout", stdout=_text(expired.stdout), stderr=_text(expired.stderr))
    except FileNotFoundError as missing:
        return Turn("missing", error=missing)
    except (OSError, ValueError) as refused:
        # Not executable, or an argument the platform will not pass.
        return Turn("unstartable", error=refused)
    finally:
        if output_path is not None:
            output_path.unlink(missing_ok=True)
    return Turn("exited", proc.returncode, stdout, proc.stderr or "")
