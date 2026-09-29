"""What has to be true on this machine before a run starts, checked and said one line each.

The provider command, its sign-in, its sandbox, git and the output directory are outside this
tool's control, and each can stop a run part of the way through, after money has been spent.
So each is checked first, in order, and every check that fails says what to do about it. The
last check starts one small real session, built exactly as a run's turns are built, and asks
the model to read a file back: the only way to learn that the account, the model and the file
tools all work. It costs a little, and `--skip-model-check` leaves it out.

Nothing a provider command prints is repeated here. A status answer can name an account, an
organisation or an address, and a refusal can quote a key; only a yes or no, or the kind of
failure, is kept.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from hazina_review.lanes.census import classify, envelope_error
from hazina_review.providers import ask as ask_mod
from hazina_review.providers import registry
from hazina_scan import cli as scan
from hazina_scan import env

OK = "ok"
FAILED = "failed"
SKIPPED = "skipped"
WARNING = "warning"
MARKS = {OK: "✓", FAILED: "✗", SKIPPED: "–", WARNING: "!"}
#: For a terminal that cannot print the marks above.
PLAIN_MARKS = {OK: "+", FAILED: "x", SKIPPED: "-", WARNING: "!"}

INSTALLED = "Provider command"
FLAGS = "Required flags"
SIGNED_IN = "Signed in"
SANDBOX = "Sandbox"
GIT = "git"
REPO = "Repository"
OUTPUT = "Output directory"
MODEL = "Model"
BUILD = "Build check"

#: Said before every run that builds: the check changes the checkout it runs in.
BUILD_WARNING = (
    "The build check runs each repository's own install, build and test commands and changes "
    "the checkout. Run it on a throwaway copy."
)

#: Ubuntu 23.10 and later set this to 1, which stops an unconfined program such as the
#: second command's bundled bubblewrap from using the user namespaces its sandbox needs.
APPARMOR_SWITCH = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")

#: The model session's ceiling, in seconds.
MODEL_SECONDS = 120
PROBE_NAME = "hazina-review-check.txt"


@dataclass(frozen=True)
class Result:
    """One line of the checklist: what was checked, how it went, one sentence, and on a
    failure or a warning the one thing to do next."""

    name: str
    state: str
    says: str
    fix: str | None = None


def _skipped(name: str, why: str) -> Result:
    return Result(name, SKIPPED, why)


# --- 1 to 4: the provider command -------------------------------------------------------------


def check_installed(provider: str) -> tuple[Result, str | None]:
    try:
        executable = registry.resolve(provider)
    except registry.ProviderUnavailable:
        return (
            Result(
                INSTALLED,
                FAILED,
                f"The `{provider}` command was not found on this machine.",
                f"Install `{provider}`, make sure it is on your PATH, and sign in.",
            ),
            None,
        )
    return Result(INSTALLED, OK, f"`{provider}` is installed at {executable}."), executable


def check_flags(provider: str, executable: str) -> Result:
    absent = registry.missing_flags(provider, executable)
    if absent:
        return Result(
            FLAGS,
            FAILED,
            f"The installed `{provider}` does not offer {', '.join(absent)}.",
            f"Upgrade `{provider}` to its latest version.",
        )
    return Result(FLAGS, OK, "The installed version offers every flag this tool needs.")


def check_signed_in(provider: str, executable: str) -> Result:
    keys = registry.api_key_names(provider)
    if registry.api_key_set(provider):
        return Result(SIGNED_IN, OK, "An API key for this provider is set in the environment.")
    login = registry.login_command(provider)
    answer = registry.signed_in(provider, executable)
    if answer is True:
        return Result(SIGNED_IN, OK, f"`{provider}` says it is signed in.")
    if answer is False:
        return Result(
            SIGNED_IN,
            FAILED,
            f"`{provider}` says it is not signed in.",
            f"Sign in with `{login}`" + (f", or set {' or '.join(keys)}." if keys else "."),
        )
    return Result(
        SIGNED_IN,
        WARNING,
        f"Could not verify the sign-in: this version of `{provider}` did not answer a status "
        "request.",
        f"Make sure you are signed in (`{login}`); the model check below will confirm it.",
    )


def check_sandbox(provider: str, executable: str) -> Result:
    if not registry.has_sandbox(provider):
        return _skipped(SANDBOX, "Not applicable: this command's file tools need no sandbox.")
    works = registry.sandbox_works(provider, executable)
    if works is None:
        return _skipped(SANDBOX, "Not checked on this platform.")
    if works:
        return Result(SANDBOX, OK, f"`{provider}`'s sandbox can read files.")
    return Result(
        SANDBOX,
        FAILED,
        f"`{provider}`'s own sandbox cannot run a command here, so its model could not read "
        "any file.",
        _sandbox_fix(provider, executable),
    )


def _apparmor_restricts() -> bool:
    try:
        return APPARMOR_SWITCH.read_text().strip() == "1"
    except OSError:
        return False


def _bwrap_in_use(provider: str, executable: str) -> Path | None:
    """The bubblewrap the command actually runs: the system's when there is one (traced on
    Ubuntu 24.04, the command runs it even though it ships its own), otherwise the copy
    bundled beside its installation. The profile has to name the program that runs."""
    system = shutil.which("bwrap")
    if system:
        return Path(system)
    try:
        real = Path(os.path.realpath(executable))
    except (OSError, ValueError):
        return None
    for parent in list(real.parents)[:4]:
        for pattern in (
            f"node_modules/@openai/*/vendor/*/{provider}-resources/bwrap",
            f"vendor/*/{provider}-resources/bwrap",
        ):
            found = sorted(parent.glob(pattern))
            if found:
                return found[0]
    return None


def _sandbox_fix(provider: str, executable: str) -> str:
    other = registry.PROVIDERS[0]
    instead = f"Or run with `--provider {other}`, which needs no sandbox setup on this machine."
    bwrap = _bwrap_in_use(provider, executable) if _apparmor_restricts() else None
    if bwrap is None:
        return (
            "Allow programs to create user namespaces on this machine, then check with "
            f"`{provider} sandbox -- ls`.\n{instead}"
        )
    name = f"{provider}-bwrap"
    target = f'\\"{bwrap}\\"' if " " in str(bwrap) else str(bwrap)
    profile = f"/etc/apparmor.d/{name}"
    # One command, one password prompt. Two lines pasted together let the second be read as
    # the password for the first; and a here-document's closing word would not end it once
    # pasted with the indentation it is shown with.
    command = (
        'sudo sh -c \'printf "%s\\n" "abi <abi/4.0>," "include <tunables/global>" '
        f'"profile {name} {target} flags=(unconfined) {{" "  userns," "}}" > {profile} '
        f"&& apparmor_parser -r {profile}'"
    )
    undo = f"sudo sh -c 'apparmor_parser -R {profile} && rm {profile}'"
    return "\n".join(
        [
            "This system's AppArmor stops the sandbox from creating user namespaces. An",
            "administrator can allow it for the sandbox program, once, with this one command:",
            "",
            f"  {command}",
            "",
            f"Then check with `{provider} sandbox -- ls`. To undo it later:",
            f"  {undo}",
            instead,
        ]
    )


def provider_checks(provider: str) -> list[Result]:
    """Checks 1 to 4. A command that is not there skips the three that would ask it."""
    installed, executable = check_installed(provider)
    if executable is None:
        why = "Skipped: the command is not installed."
        return [installed, _skipped(FLAGS, why), _skipped(SIGNED_IN, why), _skipped(SANDBOX, why)]
    return [
        installed,
        check_flags(provider, executable),
        check_signed_in(provider, executable),
        check_sandbox(provider, executable),
    ]


# --- 5 and 6: git, the repositories and the output directory ----------------------------------


def check_git() -> Result:
    fix = "Install git and make sure it is on your PATH."
    found = shutil.which("git")
    if not found:
        return Result(GIT, FAILED, "git was not found on this machine.", fix)
    try:
        asked = subprocess.run(
            [found, "--version"],
            env=env.build_env(domain=env.STATIC),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=20,
            shell=False,
        )
    except (subprocess.SubprocessError, OSError):
        return Result(GIT, FAILED, "git is there but would not start.", fix)
    if asked.returncode != 0:
        return Result(GIT, FAILED, "git is there but would not start.", fix)
    return Result(GIT, OK, (asked.stdout or "git").strip().splitlines()[0] + ".")


def check_repo(raw: Path) -> Result:
    path = Path(raw).expanduser().resolve()
    name = f"{REPO} {path.name}"
    if not path.is_dir():
        return Result(name, FAILED, f"{path} is not a folder.", "Check the path and try again.")
    if not scan.is_git_repo(path):
        return Result(
            name,
            FAILED,
            f"not a git repository: {path}",
            "Pass the top folder of a git checkout.",
        )
    if not env.run_git(path, "rev-parse", "--verify", "--quiet", "HEAD^{commit}").strip():
        # A warning and not a stop: the run still measures it, and writes its review as not
        # completed, while the other repositories named with it are reviewed as usual.
        return Result(
            name,
            WARNING,
            f"{path} has nothing committed, so its review will not complete.",
            "Commit your work at least once, then run this again.",
        )
    return Result(name, OK, f"{path} is a git repository with commits.")


def check_uncommitted(raw: Path) -> Result | None:
    """A warning when a repository the build check will run in has changes not yet committed,
    or None. Only files git already tracks are asked about."""
    path = Path(raw).expanduser().resolve()
    changed = env.run_git(path, "status", "--porcelain", "--untracked-files=no").strip()
    if not changed:
        return None
    return Result(
        f"{REPO} {path.name}",
        WARNING,
        f"{path} has uncommitted changes, and the build check may overwrite them.",
        "Commit or stash them first, or run on a throwaway copy, or pass --no-build.",
    )


def _nearest_existing(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def check_output(out: Path, repos: list[Path]) -> Result:
    """Whether the output directory can be made and written, and is not a link or inside a
    repository being reviewed. Nothing is created. The repository and link rules are the run's
    own, asked of it rather than repeated here."""
    from hazina_review import run  # the run module imports this one's caller

    fix = "Choose another directory with --out."
    raw = Path(out).expanduser()
    absolute = Path(os.path.abspath(raw))
    if absolute.is_symlink():
        return Result(OUTPUT, FAILED, f"{absolute} is a symbolic link.", fix)
    if absolute.exists() and not absolute.is_dir():
        return Result(OUTPUT, FAILED, f"{absolute} exists and is not a folder.", fix)
    base = _nearest_existing(absolute)
    if not base.is_dir() or not os.access(base, os.W_OK | os.X_OK):
        return Result(OUTPUT, FAILED, f"{absolute} cannot be created or written here.", fix)
    if repos:
        try:
            run._selected([Path(r) for r in repos], absolute.resolve())
        except run.Refused as refused:
            return Result(OUTPUT, FAILED, str(refused), fix)
    return Result(OUTPUT, OK, f"{absolute} can be written.")


# --- 7: one real session ------------------------------------------------------------------


#: Each failure kind: what happened, and what to do. `{login}`, `{model}` and `{seconds}` are
#: filled in.
_KINDS = {
    "rate_limited": (
        "The provider refused the session because of a usage or rate limit.",
        "Wait and try again later, or check your plan's usage limits.",
    ),
    "auth": (
        "The provider refused the sign-in.",
        "Sign in again with `{login}`, then run this again.",
    ),
    "model_unavailable": (
        "The model {model} was refused or does not exist for this account.",
        "Pass a model your account can use with --model.",
    ),
    "timeout": (
        "The model did not answer within {seconds} seconds.",
        "Check your network connection and the provider's status page, then try again.",
    ),
    "crashed": (
        "The provider command stopped before it answered.",
        "Check your network connection, then run the provider command on its own to see its error.",
    ),
    "cli_missing": (
        "The provider command could not be started.",
        "Reinstall the provider command and make sure it is on your PATH.",
    ),
    "no_json": (
        "The provider command answered in a form this tool could not read.",
        "Upgrade the provider command to its latest version.",
    ),
    "unknown": (
        "The provider command failed without saying why.",
        "Run the provider command on its own with this model to see its error.",
    ),
}


def _failed_as(kind: str, provider: str, model: str, seconds: int) -> Result:
    said, fix = _KINDS.get(kind, _KINDS["unknown"])
    filled = {"login": registry.login_command(provider), "model": model, "seconds": seconds}
    return Result(MODEL, FAILED, f"{said.format(**filled)} ({kind})", fix.format(**filled))


def _answer_text(stdout: str, provider: str) -> str:
    key = ask_mod.ANSWER_KEYS.get(provider)
    if not key:
        return stdout
    try:
        envelope = json.loads(stdout)
    except (ValueError, RecursionError):
        return stdout
    said = envelope.get(key) if isinstance(envelope, dict) else None
    return said if isinstance(said, str) else ""


def check_model(provider: str, model: str | None, *, seconds: int = MODEL_SECONDS) -> Result:
    """One small real session, built by the same code, with the same flags, environment and
    model, as every turn of a run: the model is asked to read a file holding a random token and
    reply with it. The token coming back is the pass."""
    model = model or registry.DEFAULT_MODELS[provider]
    if model is None:
        return Result(
            MODEL,
            FAILED,
            f"--provider {provider} requires --model with an explicit model id.",
            "Pass a model your account can use with --model.",
        )
    token = f"hazina-{secrets.token_hex(8)}"
    with tempfile.TemporaryDirectory(prefix="hazina-review-check-") as folder:
        where = Path(folder)
        (where / PROBE_NAME).write_text(token + "\n", encoding="utf-8")
        prompt = (
            f"Read the file {PROBE_NAME} in the folder {where}. Reply with only the text it "
            "contains, and nothing else."
        )
        try:
            turn = ask_mod.ask(provider, model, prompt, where, [], seconds)
        except (registry.ProviderUnavailable, registry.ProviderNotIsolated):
            return _failed_as("cli_missing", provider, model, seconds)
    if turn.ended == "timeout":
        return _failed_as("timeout", provider, model, seconds)
    if turn.ended in ("missing", "unstartable"):
        kind = "cli_missing" if turn.ended == "missing" else "crashed"
        return _failed_as(kind, provider, model, seconds)
    if turn.returncode != 0:
        kind, _ = classify(turn.returncode, turn.stdout, turn.stderr)
        return _failed_as(kind, provider, model, seconds)
    if token in _answer_text(turn.stdout, provider):
        return Result(MODEL, OK, f"{model} answered and read a file.")
    refusal = envelope_error(turn.stdout)
    if refusal:
        kind, _ = classify(0, refusal, "")
        if kind != "unknown":
            return _failed_as(kind, provider, model, seconds)
    return Result(
        MODEL,
        FAILED,
        f"{model} answered, but not with the file's contents, so it may not be able to read files.",
        "Check that the provider command can read files in a folder you name, then try again.",
    )


# --- the whole list ---------------------------------------------------------------------------


def run_checks(
    provider: str,
    model: str | None,
    repos: list[Path],
    out: Path,
    *,
    model_check: bool = True,
    build_level: str = "none",
) -> list[Result]:
    """Every check, in order. The paid one is started only when everything before it passed.

    A run that builds is warned, once, that the build check changes the checkout, and once
    for each repository whose uncommitted changes it may overwrite. Neither stops the run.
    """
    building = build_level != "none"
    results = provider_checks(provider)
    git = check_git()
    results.append(git)
    good: list[Path] = []
    for repo in repos:
        if git.state != OK:
            results.append(_skipped(f"{REPO} {Path(repo).name}", "Skipped: git is not there."))
            continue
        found = check_repo(repo)
        results.append(found)
        if found.state != FAILED:
            good.append(Path(repo))
            dirty = check_uncommitted(repo) if building else None
            if dirty is not None:
                results.append(dirty)
    results.append(check_output(out, good))
    if building:
        results.append(Result(BUILD, WARNING, BUILD_WARNING, "Pass --no-build to leave it out."))
    if not model_check:
        results.append(
            _skipped(
                MODEL,
                "Skipped (--skip-model-check): a run may still stop on the account's limits or "
                "the model.",
            )
        )
    elif not passed(results):
        results.append(_skipped(MODEL, "Skipped: fix the failed check above first."))
    else:
        results.append(check_model(provider, model))
    return results


def passed(results: list[Result]) -> bool:
    return not any(result.state == FAILED for result in results)


def _marks(stream: TextIO) -> dict[str, str]:
    try:
        "".join(MARKS.values()).encode(getattr(stream, "encoding", None) or "ascii")
    except (UnicodeEncodeError, LookupError):
        return PLAIN_MARKS
    return MARKS


def say(results: list[Result], stream: TextIO) -> None:
    """The checklist: one line each, and the fix beneath a failure or a warning."""
    marks = _marks(stream)
    for result in results:
        print(f"{marks[result.state]} {result.name}: {result.says}", file=stream)
        if result.fix and result.state in (FAILED, WARNING):
            for line in result.fix.splitlines():
                print(f"    {line}" if line else "", file=stream)
    stream.flush()
