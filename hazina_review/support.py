"""What a batch tells the person running it, what it keeps so it can carry on, and the file they
can send us when something goes wrong.

A batch runs on a machine nobody at Hazina Labs can see. So three things are looked after here.
The words: every problem is said in plain language with the one command that deals with it.
The progress file: after every repository the whole state of the batch is written down, so
`--resume` can carry on with exactly the options it began with. And the support log: a plain
record of what happened, safe to email, that never holds the model's answers, a file's
contents, a sign-in detail or a full home-directory path.

Neither file is ever packed into the zip, and neither is removed by the clean-up of an earlier
version's local files.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import stat
import sys
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from hazina_review import __version__
from hazina_review.providers import registry
from hazina_scan import orchestrator

PROGRESS_NAME = "hazina-review-progress.json"
LOG_NAME = "hazina-review-log.txt"
SUPPORT_ADDRESS = "partners@hazinalabs.com"
PROGRESS_FORMAT = 1

DONE = "done"
INCOMPLETE = "incomplete"
NOT_STARTED = "not started"

#: Kinds of this module's own, beside the census's failure kinds.
NOT_ISOLATED = "not_isolated"
INTERRUPTED = "interrupted"
ERROR = "error"
GONE = "gone"

#: Problems that would meet every repository the same way: carrying on would only turn the
#: rest of the batch into rows that say nothing, some of them billed.
STOP_KINDS = ("rate_limited", "auth", "model_unavailable", "cli_missing", NOT_ISOLATED)

#: How the build check is sized, as the scanner sizes it, and the level a run that recorded
#: none of them ran at: a run started before the build check was part of a review.
BUILD_OPTIONS = {
    "build_level": "none",
    "build_budget_seconds": orchestrator.DEFAULT_BUILD_BUDGET_SECONDS,
    "full_attempt_seconds": orchestrator.DEFAULT_FULL_ATTEMPT_SECONDS,
    "timeout_build": orchestrator.DEFAULT_TIMEOUT_BUILD,
    "max_build_projects": orchestrator.DEFAULT_MAX_BUILD_PROJECTS,
}

#: The options a run is started with, recorded so a resumed run uses the same ones.
RECORDED = (
    "provider",
    "model",
    "budget_seconds",
    "mine_n",
    "census_timeout",
    "mine_timeout",
    *BUILD_OPTIONS,
)

#: The kinds a build check that did not complete leaves on its repository.
BUILD_TIMED_OUT = "build_timed_out"
BUILD_UNAVAILABLE = "build_unavailable"

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_NOCTTY = getattr(os, "O_NOCTTY", 0)


# --- the run's own two files -------------------------------------------------------------------


class UnsafeRunFile(ValueError):
    """Something other than this run's own file is at the log's or the progress file's name.
    Nothing was written to it."""


def _unsafe(path: Path, why: str) -> UnsafeRunFile:
    return UnsafeRunFile(
        f"refusing to use {path}: it is {why}, and writing to it could change a file outside "
        "the output directory or wait forever. Remove it, or choose another directory for --out."
    )


def _why_unsafe(status: os.stat_result) -> str | None:
    """What is wrong with the file `status` describes, or None for a file of this run's own:
    a regular file with no other name. A second name (a hard link) may be anywhere on the
    same disk, so a write through this one would land there too."""
    if stat.S_ISLNK(status.st_mode):
        return "a symbolic link"
    if not stat.S_ISREG(status.st_mode):
        return "not a regular file"
    if status.st_nlink != 1:
        return "a hard link, a second name for a file that may be anywhere on the disk"
    return None


def check_run_file(path: Path) -> None:
    """Raise `UnsafeRunFile` when something is at `path` and it is not a file this run may
    write: a link, a pipe, a device, a folder, or a file with another name elsewhere."""
    try:
        status = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return
    why = _why_unsafe(status)
    if why is not None:
        raise _unsafe(Path(path), why)


def check_run_files(out_dir: Path) -> None:
    """The support log's and the progress file's names in `out_dir`, checked before anything
    else a run does: before the checks, the last of which is billed."""
    out_dir = Path(out_dir).expanduser()
    for name in (LOG_NAME, PROGRESS_NAME):
        check_run_file(out_dir / name)


def _open_own(path: Path, flags: int) -> int:
    """Open one of the run's own files and check what was opened before a byte is written.

    Never through a link, and never waiting on a pipe. The opened descriptor must be a regular
    file with one name, and the very file now at `path` in the output directory: a file put
    in place of the one checked is refused rather than written.
    """
    path = Path(path)
    check_run_file(path)
    try:
        fd = os.open(path, flags | _NOFOLLOW | _NONBLOCK | _NOCTTY, 0o600)
    except OSError:
        # A link or a pipe nobody reads, put at the name since it was checked.
        if os.path.lexists(path):
            check_run_file(path)
            raise _unsafe(path, "not a regular file") from None
        raise
    try:
        opened = os.fstat(fd)
        why = _why_unsafe(opened)
        if why is None:
            here = os.lstat(Path(os.path.realpath(path.parent)) / path.name)
            if (here.st_dev, here.st_ino) != (opened.st_dev, opened.st_ino):
                why = "no longer the file that was checked"
        if why is not None:
            raise _unsafe(path, why)
        if _NONBLOCK and hasattr(os, "set_blocking"):
            os.set_blocking(fd, True)
        return fd
    except BaseException:
        os.close(fd)
        raise


# --- the words ---------------------------------------------------------------------------------


def duration(seconds: float) -> str:
    """`5s`, `5m 12s`, `1h 02m 03s`."""
    whole = max(0, int(round(seconds)))
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def short(seconds: float) -> str:
    """A running lane's age, to the minute once it has one: `45s`, `4m`, `1h 05m`."""
    whole = max(0, int(seconds))
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        return f"{whole // 60}m"
    return f"{whole // 3600}h {whole % 3600 // 60:02d}m"


def reason(kind: str | None, provider: str, model: str | None) -> str:
    """What happened, as a clause a person reads, for one of the failure kinds."""
    name = registry.display_name(provider)
    said = {
        "rate_limited": f"{name}'s usage limit was reached",
        "auth": f"{name} refused the sign-in",
        "model_unavailable": f"the account cannot use the model {model}",
        "cli_missing": f"the `{provider}` command could not be found or started",
        NOT_ISOLATED: f"the `{provider}` command lacks a safety switch this tool needs",
        "timeout": "it ran out of time",
        "no_json": "the model's answer could not be read",
        "crashed": f"the `{provider}` command stopped before it answered",
        "unknown": f"the `{provider}` command failed without saying why",
        INTERRUPTED: "the run was stopped with Ctrl-C",
        ERROR: "this tool ran into an unexpected problem",
        GONE: "its folder is no longer there",
        "nothing_to_read": "there was nothing committed to read",
        "copy_incomplete": "the temporary copy of the repository came out incomplete",
        BUILD_TIMED_OUT: "the build check ran out of time",
        BUILD_UNAVAILABLE: "the build check could not be completed",
    }
    return said.get(kind or "unknown", said["unknown"])


def build_outcome(block: dict | None) -> tuple[str, str]:
    """How a build check ended, as `(how, words)`: `how` is `DONE`, `NOT_STARTED` or
    `INCOMPLETE`, and `words` says what came of it. Only the block's yes-or-no facts are read,
    never what a command printed."""
    block = block or {}
    if block.get("build_skipped"):
        return NOT_STARTED, "the run's time was used up"
    if block.get("timed_out"):
        return INCOMPLETE, "it ran out of time"
    if not block.get("ok"):
        return INCOMPLETE, "it ran into a problem"
    built = block.get("build_ok")
    if built is False:
        return DONE, "the repository did not build"
    if built is None:
        return INCOMPLETE, "this machine could not build it"
    ran = block.get("build_and_tests_ran")
    if ran:
        return DONE, "built, tests ran"
    if ran is None and block.get("tests_discovered"):
        return DONE, "built, tests listed"
    return DONE, "built, no tests ran"


#: How many incomplete repositories the summary names before it points to the log instead.
NAMED = 10


def _repositories(count: int) -> str:
    return f"{count} {'repository' if count == 1 else 'repositories'}"


def resume_command(out_dir: Path) -> str:
    return f"hazina-review --resume {shlex.quote(str(out_dir))}"


def support_line(out_dir: Path) -> str:
    return f"If you need help, email {Path(out_dir) / LOG_NAME} to {SUPPORT_ADDRESS}."


def _dash(stream) -> str:
    try:
        "—".encode(getattr(stream, "encoding", None) or "ascii")
    except (UnicodeEncodeError, LookupError):
        return "-"
    return "—"


def _stopped(kind: str, provider: str, model: str | None, detail: str | None) -> list[str]:
    """The first line of a stop message, and what to do before resuming."""
    name = registry.display_name(provider)
    if kind == "rate_limited":
        return [
            f"The run stopped: your {name} account reached its usage limit.",
            "When the limit resets, carry on with:",
        ]
    if kind == "auth":
        return [
            f"The run stopped: {name} refused the sign-in. You are not signed in, or the "
            "sign-in has expired.",
            "Sign in with:",
            f"    {registry.login_command(provider)}",
            "then carry on with:",
        ]
    if kind == "model_unavailable":
        return [
            f"The run stopped: this {name} account cannot use the model {model}.",
            "Once the account can use it, carry on with:",
        ]
    if kind == "cli_missing":
        return [
            f"The run stopped: {detail}"
            if detail
            else f"The run stopped: the `{provider}` command could not be found or started.",
            f"Install `{provider}`, make sure it is on your PATH, and sign in. Then carry on with:",
        ]
    if kind == NOT_ISOLATED:
        first = (
            f"The run stopped: {detail}"
            if detail
            else (
                f"The run stopped: the installed `{provider}` command lacks a safety switch this "
                "tool needs."
            )
        )
        return [first, f"Upgrade `{provider}` to its latest version if needed. Then carry on with:"]
    return ["The run was stopped with Ctrl-C.", "To carry on where it stopped:"]


def report(done: dict, stream=None) -> tuple[list[str], int]:
    """The end of a run as the person reads it, and the exit code.

    0 when every repository is done; 1 when the run went to the end and some repository is
    not; 2 when it stopped early.
    """
    stream = stream or sys.stderr
    dash = _dash(stream)
    repos: list[Repo] = done["repos"]
    out_dir, provider, model = done["out_dir"], done["provider"], done["model"]
    kind = done["stop_kind"]
    finished = sum(repo.state == DONE for repo in repos)
    unfinished = [repo for repo in repos if repo.state == INCOMPLETE]
    waiting = sum(repo.state == NOT_STARTED for repo in repos)
    # A count, then only what needs attention: a batch can be hundreds long, and a line per
    # repository buries the few that did not complete. The log keeps every one.
    if finished == len(repos):
        done = "done" if len(repos) == 1 else f"all {_repositories(len(repos))} done"
        return ["", f"Summary: {done}."], 0
    counts = f"Summary: {finished} of {_repositories(len(repos))} done"
    counts += f", {len(unfinished)} incomplete" if unfinished else ""
    counts += f", {waiting} not started" if waiting else ""
    lines = ["", counts + "."]
    for repo in unfinished[:NAMED]:
        lines.append(f"  {repo.folder} {dash} {reason(repo.kind, provider, model)}")
    if len(unfinished) > NAMED:
        lines.append(f"  and {len(unfinished) - NAMED} more, listed in {Path(out_dir) / LOG_NAME}")
    resume = f"    {resume_command(out_dir)}"
    lines.append("")
    if kind is not None:
        first, *todo = _stopped(kind, provider, model, done.get("stop_detail"))
        kept = (
            f"Nothing that was finished is lost: the zip holds the {finished} finished "
            f"{'repository' if finished == 1 else 'repositories'}."
            if finished
            else "Nothing had finished yet."
        )
        lines += [first, kept, *todo, resume]
        if kind == "model_unavailable":
            lines += [
                "Or start a new run with --model <a model your account can use> and a new",
                "--out. A resumed run always keeps the model it started with, so that every",
                "result in one batch comes from one model.",
            ]
    else:
        lines += [
            "Some repositories did not complete. What could be measured is written, and",
            "counts that could not be measured are left empty. To try just those again:",
            resume,
        ]
    lines.append(support_line(out_dir))
    return lines, 2 if kind is not None else 1


# --- the progress file -------------------------------------------------------------------------


class NoProgress(ValueError):
    """There is no run here that can be carried on, and the message says why."""


@dataclass
class Repo:
    """One repository of a batch, as the progress file keeps it. `packed` says whether its
    folder goes into the zip."""

    path: str
    folder: str
    state: str = NOT_STARTED
    kind: str | None = None
    packed: bool = False


@dataclass
class Progress:
    out_dir: Path
    options: dict
    repos: list[Repo]
    stopped: str | None = None
    tool_version: str = __version__

    @property
    def path(self) -> Path:
        return self.out_dir / PROGRESS_NAME

    def save(self) -> None:
        """Written whole to a new file beside it and moved into place, so a run killed
        part-way through leaves either the last copy or this one, never half of one."""
        document = {
            "format": PROGRESS_FORMAT,
            "tool_version": self.tool_version,
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "options": self.options,
            "stopped": self.stopped,
            "repos": [asdict(repo) for repo in self.repos],
        }
        temporary = self.out_dir / f".{PROGRESS_NAME}.tmp-{os.getpid()}"
        check_run_file(self.path)
        # The temporary name is this run's alone. Whatever an earlier run left there is only
        # unnamed, which never touches what a link or a second name points at; the file is
        # then made new, and refused if anything else is there by the time it is opened.
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        fd = _open_own(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(document, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            check_run_file(self.path)
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    @classmethod
    def load(cls, out_dir: Path) -> Progress:
        out_dir = Path(out_dir).expanduser().resolve()
        path = out_dir / PROGRESS_NAME
        start_new = "Start a new run by naming the repositories instead."
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise NoProgress(f"{path} is not a regular file, so it is not read. {start_new}")
        if not path.is_file():
            raise NoProgress(
                f"There is nothing to resume in {out_dir}: no earlier run left its progress "
                f"file ({PROGRESS_NAME}) there. Check the folder you gave. {start_new}"
            )
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            # A run recorded before the build check was part of a review ran without one.
            recorded = {**BUILD_OPTIONS, **document["options"]}
            options = {key: recorded[key] for key in RECORDED}
            repos = [
                Repo(
                    path=str(item["path"]),
                    folder=str(item["folder"]),
                    state=str(item["state"]),
                    kind=item.get("kind"),
                    packed=bool(item.get("packed")),
                )
                for item in document["repos"]
            ]
            version = str(document["tool_version"])
        except (OSError, ValueError, KeyError, TypeError):
            raise NoProgress(
                f"There is nothing to resume in {out_dir}: its progress file could not be "
                f"read. {start_new}"
            ) from None
        if options["provider"] not in registry.PROVIDERS or not repos:
            raise NoProgress(f"The progress file in {out_dir} is not one this tool wrote.")
        if version != __version__:
            raise NoProgress(
                f"The run in {out_dir} was started with hazina-review {version}, and this is "
                f"{__version__}. Results in one batch must come from one version, so start a "
                "new run with a new --out."
            )
        return cls(out_dir=out_dir, options=options, repos=repos)


# --- the support log ---------------------------------------------------------------------------

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
#: What a credential tends to look like on its own: a known prefix, a signed web token, or a
#: long run of letters and digits mixed that is not a plain identifier such as a folder's id.
_TOKENS = re.compile(
    r"\b(?:sk|pk|rk|tok|ghp|gho|ghs|ghu|github_pat|xox[abpr])[-_][A-Za-z0-9_\-]{8,}"
    r"|\beyJ[A-Za-z0-9_\-]{10,}(?:\.[A-Za-z0-9_\-]+)*"
    r"|\b(?![0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b)"
    r"(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{32,}"
)
#: Environment variables whose values are never written, by the shape of their names.
_SECRET_NAMES = re.compile(r"KEY|TOKEN|SECRET|PASS|AUTH|CREDENTIAL|COOKIE|SESSION", re.I)


def _secret_values() -> list[str]:
    named = {name for names in registry.AUTH_VARS.values() for name in names}
    values = {
        value
        for name, value in os.environ.items()
        if (name in named or _SECRET_NAMES.search(name)) and len(value) >= 6
    }
    return sorted(values, key=len, reverse=True)


def scrub(text: str) -> str:
    """`text` with every secret-shaped thing taken out: the values of credential variables,
    email addresses, token-shaped strings; and the home directory written as `~`."""
    for value in _secret_values():
        text = text.replace(value, "[removed]")
    home = str(Path.home())
    if len(home) > 1:
        text = text.replace(home, "~")
    text = _EMAIL.sub("[email address removed]", text)
    return _TOKENS.sub("[removed]", text)


#: The variables a provider command may need to find its sign-in or its settings. The log says
#: of each whether it is set and whether a provider command is handed it: the name and a yes or
#: no, never the value. Enough to see, on a machine nobody here can reach, what a command that
#: cannot find its sign-in was and was not given.
ENVIRONMENT_NAMES = (
    "HOME",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "USERPROFILE",
    "XDG_CONFIG_HOME",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
)


def environment_lines(provider: str) -> list[str]:
    """Two lines for the log: the provider-relevant variables, then the provider's own sign-in
    variables, each by name with whether it is set and whether the command is handed it."""
    handed = registry.provider_env(provider)

    def said(name: str) -> str:
        if not os.environ.get(name):
            return f"{name}: not set"
        return f"{name}: set, {'passed' if name in handed else 'not passed'}"

    return [
        "provider environment (names only): " + "; ".join(said(name) for name in ENVIRONMENT_NAMES),
        "provider sign-in variables (names only): "
        + "; ".join(said(name) for name in registry.AUTH_VARS[provider]),
    ]


def _system() -> str:
    """The operating system, and on a Mac the macOS version, which the kernel's does not say."""
    said = f"{platform.system()} {platform.release()} ({platform.machine()})"
    mac = platform.mac_ver()[0] if sys.platform == "darwin" else ""
    return f"{said}, macOS {mac}" if mac else said


def provider_version(executable: str) -> str:
    return registry.version_line(executable) or "unknown"


def _git_version() -> str:
    from hazina_review import preflight  # preflight's own check, asked again here

    return preflight.check_git().says.rstrip(".")


class Log:
    """The support log: appended to, never rewritten, one timestamped line at a time, and
    every line scrubbed on its way in."""

    def __init__(self, out_dir: Path) -> None:
        self.path = Path(out_dir) / LOG_NAME
        self._lock = threading.Lock()
        check_run_file(self.path)

    def _append(self, lines: list[str]) -> None:
        text = "".join(f"{line}\n" for line in lines)
        with self._lock:
            fd = _open_own(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(text)

    def write(self, message: str) -> None:
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._append([f"{stamp} {scrub(line)}" for line in str(message).splitlines() or [""]])

    def begin(
        self,
        *,
        resumed: bool,
        provider: str,
        executable: str,
        options: dict,
        out_dir: Path,
        repos: list[Repo],
        checklist,
    ) -> None:
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        kind = "resumed" if resumed else "new"
        self._append(["", f"==== hazina-review run started {stamp} ({kind} run) ===="])
        self.write(f"tool: hazina-review {__version__}")
        self.write(f"system: {_system()}")
        self.write(f"python: {platform.python_version()}")
        self.write(f"git: {_git_version()}")
        self.write(f"provider: {provider}, version {provider_version(executable)}")
        for line in environment_lines(provider):
            self.write(line)
        self.write(
            f"options: model {options['model']}, budget {options['budget_seconds']}s, task "
            f"ceiling {options['mine_n']}, census limit {options['census_timeout']}s, mining "
            f"limit {options['mine_timeout']}s"
        )
        level = options.get("build_level", "none")
        if level == "none":
            self.write("build check: none")
        else:
            self.write(
                f"build check: {level}, its share {options['build_budget_seconds']}s, a full "
                f"attempt {options['full_attempt_seconds']}s, one command "
                f"{options['timeout_build']}s, up to {options['max_build_projects']} projects"
            )
        self.write(f"output: {out_dir}")
        states = ", ".join(f"{repo.folder} ({repo.state})" for repo in repos)
        self.write(f"repositories: {len(repos)}: {states}")
        for result in checklist or []:
            self.write(f"check {result.state}: {result.name}: {result.says}")
