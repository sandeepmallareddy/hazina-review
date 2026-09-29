"""The provider command this tool can start: what it is called, which sign-in variables it
is handed, and what the installed copy has to offer before it is aimed at a repository."""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from hazina_scan.env import MODEL, STATIC, build_env


class ProviderUnavailable(RuntimeError):
    """The requested command is not installed. Never degraded to an unscored lane: 'not
    installed here' and 'this repository held nothing' are different facts."""


class ProviderNotIsolated(RuntimeError):
    """The installed command lacks a flag this tool will not start it without."""


class ProviderSandboxBroken(ProviderNotIsolated):
    """The command's own sandbox cannot run a command on this machine, so its model could not
    open a single file. Stopped here: every count would otherwise come back as a false zero."""


@dataclass(frozen=True)
class Provider:
    """One command-line tool, as far as this package needs to know it.

    `sign_in` names the environment variables copied across to it. They are copied BY NAME:
    nothing here reads, logs or branches on a value. `isolation` is the fixed argument sequence
    for a turn. `required` lists the switches its installed CLI must advertise first.
    `default_model` is an exact pinned id, or None when the operator must name one. Leaving
    a model choice to a CLI can change the measurement when its default changes.
    """

    name: str
    sign_in: tuple[str, ...]
    isolation: tuple[str, ...]
    default_model: str | None
    probe: tuple[str, ...] = ("--help",)
    required: tuple[str, ...] | None = None
    #: The subcommand that runs one command inside the provider's own sandbox, for a command
    #: whose model reads files by running commands there. None where file tools need no such
    #: sandbox.
    sandbox_check: tuple[str, ...] | None = None
    #: Put ahead of both prompts, for a command whose tools differ from the ones the prompts
    #: name. Empty when the prompts fit the command as they stand.
    prompt_note: str = ""
    #: The subcommand that says whether the command is signed in, without calling a model.
    status: tuple[str, ...] = ()
    #: The key in the status command's JSON answer that holds the yes or no, or None when the
    #: status command answers with its exit status.
    status_key: str | None = None
    #: The subcommand that signs in.
    login: tuple[str, ...] = ()
    #: The variables that hold a key the command signs in with instead of its own login.
    api_keys: tuple[str, ...] = ()
    #: The name a person knows the service by, for the messages they read.
    display: str = ""

    def lacking(self, help_text: str) -> tuple[str, ...]:
        """The isolation flags that `help_text` does not list as flags in their own right."""
        return tuple(
            flag for flag in (self.required or self.isolation) if not _lists(help_text, flag)
        )


_KNOWN = (
    Provider(
        name="claude",
        sign_in=(
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CONFIG_DIR",
        ),
        isolation=(
            # What the command is told so that configuration committed to the tree it is
            # aimed at -- settings, instruction files, hooks -- does not configure the agent.
            "--safe-mode",
            # Takes away the built-in tools that run commands or code, and the one that
            # fetches from the web; ignores user, project and local settings files; holds
            # the file tools to the working directories; refuses to bypass permissions.
            "--restricted",
            # Only the MCP servers named on the command line are started, and none is named.
            "--strict-mcp-config",
            # The turn is not saved as a session, so no transcript of it stays on disk.
            "--no-session-persistence",
        ),
        default_model="claude-opus-5",
        status=("auth", "status", "--json"),
        status_key="loggedIn",
        login=("auth", "login"),
        api_keys=("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"),
        display="Claude",
    ),
    Provider(
        name="codex",
        sign_in=("OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_HOME"),
        isolation=(
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--ignore-rules",
            "--ignore-user-config",
            "-c",
            "project_doc_max_bytes=0",
            "-c",
            'approval_policy="never"',
        ),
        # Pinned, as for the first provider: left to itself the command picks whatever its
        # own default is at the time, and that changes when the command is updated.
        default_model="gpt-6-sol",
        probe=("exec", "--help"),
        sandbox_check=("sandbox", "--"),
        # The prompts name Read, Grep and Glob tools and say there is no shell. This command
        # reads files only through its shell, and taken at its word it ran no command and
        # answered with an empty task list. So it is told how to read, before anything else.
        prompt_note=(
            "Note for this session: the Read, Grep and Glob tools named below are not "
            "available here. Your shell is how you read: use read-only commands such as ls, "
            "cat, head, sed -n, grep, rg and find on the current directory and on the history "
            "files named below. Where the instructions below say there is no shell, read that "
            "as: do not use git, and run nothing that changes anything. Everything else below "
            "applies as written.\n\n"
        ),
        status=("login", "status"),
        login=("login",),
        api_keys=("OPENAI_API_KEY",),
        display="Codex",
        required=(
            "--ephemeral",
            "--sandbox",
            "--skip-git-repo-check",
            "--ignore-rules",
            "--ignore-user-config",
            "-c",
            "--output-last-message",
        ),
    ),
)

PROVIDERS = tuple(known.name for known in _KNOWN)
AUTH_VARS = {known.name: known.sign_in for known in _KNOWN}
DEFAULT_MODELS = {known.name: known.default_model for known in _KNOWN}

_HELP_SECONDS = 20


def _known(provider: str) -> Provider:
    for known in _KNOWN:
        if known.name == provider:
            return known
    raise ValueError(f"{provider!r} is not a provider; choose one of {PROVIDERS}")


def _lists(help_text: str, flag: str) -> bool:
    """A longer flag that merely contains `flag` proves nothing about the one asked for, so
    the match has to end where the flag does, on both sides."""
    return re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", help_text) is not None


def _help_text(executable: str, probe: tuple[str, ...]) -> str:
    """What the provider's help command prints, or nothing when it cannot be had.

    This is the only way the command is started before it is known to be safe to aim, so it
    is handed no credential, and it starts in a directory made for the purpose and empty: the
    operator is often standing in the very repository that is about to be measured."""
    try:
        with tempfile.TemporaryDirectory(prefix="hazina-review-probe-") as nowhere:
            asked = subprocess.run(
                [executable, *probe],
                cwd=nowhere,
                env=build_env(domain=STATIC),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                timeout=_HELP_SECONDS,
                shell=False,
            )
    except (subprocess.SubprocessError, OSError):
        return ""
    return asked.stdout or ""


@functools.cache
def help_of(executable: str, probe: tuple[str, ...] = ("--help",)) -> str:
    """`_help_text`, asked once for each executable however many turns a run takes. Help that
    could not be had is remembered as well: it lists nothing, and that stops the run."""
    return _help_text(executable, probe)


#: Platforms where the second command sandboxes its commands itself and can be asked to prove it.
_SANDBOX_CHECKED = ("linux", "darwin")
_PROBE_NAME = "hazina-review-probe.txt"
_PROBE_TEXT = "hazina review sandbox probe\n"


@functools.cache
def sandbox_reads(executable: str, check: tuple[str, ...]) -> bool:
    """Whether the provider's sandbox can read one file, asked once per run with no model call.

    A command whose model reads files by running commands inside its own sandbox answers
    with an empty assessment when that sandbox cannot start, and an empty assessment scores
    as a measured zero. On Linux the usual cause is a system that forbids the user
    namespaces the sandbox is built on. So a known file is put in an empty directory and the
    sandbox is asked to print it: anything but that file's text means it cannot read. No
    credential is handed over; this starts no session with any model."""
    try:
        with tempfile.TemporaryDirectory(prefix="hazina-review-sandbox-") as nowhere:
            (Path(nowhere) / _PROBE_NAME).write_text(_PROBE_TEXT, encoding="utf-8")
            asked = subprocess.run(
                [executable, *check, "cat", _PROBE_NAME],
                cwd=nowhere,
                env=build_env(passthrough=("HOME", "USERPROFILE", "CODEX_HOME"), domain=STATIC),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                timeout=_HELP_SECONDS,
                shell=False,
            )
    except (subprocess.SubprocessError, OSError):
        return False
    return asked.returncode == 0 and _PROBE_TEXT.strip() in (asked.stdout or "")


def prompt_note(provider: str) -> str:
    """What goes ahead of both prompts for this provider; empty for most."""
    return _known(provider).prompt_note


def resolve(provider: str) -> str:
    found = shutil.which(_known(provider).name)
    if not found:
        raise ProviderUnavailable(
            f"the `{provider}` command is not on this machine. Install it and sign in. This "
            f"run stops here rather than reporting an empty result that would read as "
            f"'nothing was found'."
        )
    return found


def missing_flags(provider: str, executable: str) -> tuple[str, ...]:
    """The flags this tool requires that the installed command does not list."""
    known = _known(provider)
    return known.lacking(help_of(executable, known.probe))


def has_sandbox(provider: str) -> bool:
    """Whether the command's model reads files through a sandbox of the command's own."""
    return _known(provider).sandbox_check is not None


def sandbox_works(provider: str, executable: str) -> bool | None:
    """Whether the command's own sandbox can read a file, or None when there is nothing to ask:
    the command has no such sandbox, or this platform is not one it can be asked on."""
    known = _known(provider)
    if known.sandbox_check is None or sys.platform not in _SANDBOX_CHECKED:
        return None
    return sandbox_reads(executable, known.sandbox_check)


def api_key_names(provider: str) -> tuple[str, ...]:
    return _known(provider).api_keys


def api_key_set(provider: str) -> bool:
    """Whether one of the command's key variables is set. Only whether: the value is not read."""
    return any(os.environ.get(name) for name in _known(provider).api_keys)


def display_name(provider: str) -> str:
    """The service's name as a person would write it in a sentence."""
    known = _known(provider)
    return known.display or known.name


def version_line(executable: str) -> str | None:
    """The first line the command prints for `--version`, or None when it prints nothing.

    Started as the help probe is, with no credential and in an empty directory made for the
    purpose. Kept for the support log, which scrubs it like every other line it writes."""
    said = _help_text(executable, ("--version",)).strip()
    return said.splitlines()[0][:200] if said else None


def login_command(provider: str) -> str:
    """What the operator types to sign in, as they would type it."""
    return " ".join((provider, *_known(provider).login))


#: What a status command that answers with its exit status prints when it is signed out.
_SIGNED_OUT = re.compile(r"not logged in|logged out|not signed in|signed out", re.I)


def signed_in(provider: str, executable: str) -> bool | None:
    """Whether the command says it is signed in, or None when it could not be asked.

    Free: no model is called. The status command is started as a turn would be, with the
    sign-in variables and the home directory, in an empty directory made for the purpose.
    Only the yes or no is kept. What it printed besides, which can name an account, an
    organisation or an address, is dropped here and never returned, logged or stored."""
    known = _known(provider)
    if not known.status:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="hazina-review-status-") as nowhere:
            asked = subprocess.run(
                [executable, *known.status],
                cwd=nowhere,
                env=build_env(passthrough=("HOME", "USERPROFILE", *known.sign_in), domain=MODEL),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=_HELP_SECONDS,
                shell=False,
            )
    except (subprocess.SubprocessError, OSError):
        return None
    if known.status_key is not None:
        try:
            answer = json.loads(asked.stdout or "")
        except (ValueError, RecursionError):
            return None
        found = answer.get(known.status_key) if isinstance(answer, dict) else None
        return found if isinstance(found, bool) else None
    if asked.returncode == 0:
        return True
    if _SIGNED_OUT.search(f"{asked.stdout or ''}\n{asked.stderr or ''}"):
        return False
    return None


def isolation_flags(provider: str, executable: str) -> list[str]:
    """The flags every turn is started with, or a stop when the installed command lacks one."""
    known = _known(provider)
    absent = missing_flags(provider, executable)
    if absent:
        raise ProviderNotIsolated(
            f"the installed `{provider}` command does not offer {', '.join(absent)}. This tool "
            f"requires those controls before starting a turn, so the measured repository cannot "
            f"configure its own measurement. "
            f"Upgrade the command to a version that offers every one of them, then run this "
            f"again."
        )
    if sandbox_works(provider, executable) is False:
        raise ProviderSandboxBroken(
            f"the `{provider}` command's own sandbox cannot run a command on this machine, so "
            f"its model could not open a single file and every count would come back as a "
            f"false zero. On Linux this is usually a system setting that stops programs creating "
            f"user namespaces (Ubuntu 24.04 and later restrict them through AppArmor); allow "
            f"the sandbox to create them, check with `{provider} sandbox -- ls`, then run this "
            f"again."
        )
    return list(known.isolation)
