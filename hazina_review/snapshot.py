"""A scratch copy of the repository for the agent to read, in place of the working tree.

`hazina_review.brief` withholds credential-shaped files from the history. The agent's read
tools are aimed at a directory too, and a live `.env` sitting in the working tree is readable
there; asking a model not to open it is not a control. So the agent is aimed at this copy: the
files git tracks at `HEAD`, minus every credential-shaped path. A live `.env` is normally
untracked and so never enters the copy at all; a tracked one is left out when its own name or
the name of any directory above it has one of the shapes the history withholds. That is wider
than the history, which judges a file by its own name alone.

The copy is made with `git ls-tree` and one `git cat-file --batch`, not `git archive`. An
archive is filtered through the repository's own attributes (`export-subst`, `export-ignore`,
line-ending conversion), so it is not byte-for-byte; and ls-tree states each entry's mode
before any content is asked for, so a symbolic link or a submodule is passed over by what it
is rather than picked out of an archive afterwards.

One thing IS read from the working tree: which tracked paths are missing from it. A file
deleted there but not yet committed is still at `HEAD`, and a copy holding it would show the
agent a file that nobody reading the working tree would find. Those paths are left out.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
import threading
from pathlib import Path

from hazina_review.brief import CREDENTIAL_GLOBS
from hazina_scan.env import build_env, git_argv, git_ceiling_seconds

_GIT_TIMEOUT = 600
_CHUNK = 1 << 20


def is_credential_path(path: str) -> bool:
    """True when any segment of `path`, the file or a directory above it, has a credential
    shape, whatever its case."""
    return any(
        fnmatch.fnmatchcase(part.lower(), glob.lower())
        for part in path.split("/")
        for glob in CREDENTIAL_GLOBS
    )


def is_safe_relative_path(path: str) -> bool:
    """True when `path` can only name something beneath the directory it is joined to.

    Git refuses to stage a path that climbs out of the repository or passes through a `.git`,
    but a tree object can be written by hand, and a repository under review is not trusted to
    be well formed. A `.git` of any case is refused because a directory or a file of that name
    is where git reads its configuration from.
    """
    if not path or path.startswith("/"):
        return False
    # a separator and a drive on Windows; ordinary characters in a name anywhere else
    if os.name == "nt" and ("\\" in path or ":" in path):
        return False
    return all(part not in ("", ".", "..") and part.lower() != ".git" for part in path.split("/"))


def _timeout() -> int:
    ceiling = git_ceiling_seconds()
    return _GIT_TIMEOUT if ceiling is None else min(_GIT_TIMEOUT, ceiling)


def _git_argv(repo: Path, *args: str) -> list[str]:
    return git_argv(repo, *args)


def _tracked_blobs(repo: Path) -> list[tuple[str, str]]:
    """`(object id, path)` for every regular file at `HEAD`, or nothing when git will not say.

    Read as bytes: `run_git` decodes with replacement, which would silently rename a file
    whose name is not valid UTF-8.
    """
    timeout = _timeout()
    if timeout <= 0:
        return []
    try:
        proc = subprocess.run(
            _git_argv(repo, "ls-tree", "-r", "-z", "--full-tree", "HEAD"),
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            shell=False,
            env=build_env(passthrough=("HOME", "USERPROFILE")),
        )
    except (subprocess.SubprocessError, OSError):
        return []
    if proc.returncode != 0:
        return []
    entries = []
    for record in proc.stdout.split(b"\0"):
        meta, tab, raw_path = record.partition(b"\t")
        fields = meta.split()
        if not tab or len(fields) != 3:
            continue
        mode, kind, oid = (field.decode("ascii", "replace") for field in fields)
        # 120000 is a symbolic link and 160000 a submodule; both arrive with a type or a mode
        # other than a regular file's, and neither is followed nor recreated
        if kind == "blob" and mode.startswith("100"):
            entries.append((oid, os.fsdecode(raw_path)))
    return entries


def _deleted_in_working_tree(repo: Path) -> set[str]:
    """The paths tracked at `HEAD` that the working tree no longer has, staged or not.

    Nothing when git will not say, which leaves the copy as `HEAD` has it: a repository with
    no working tree at all has nothing deleted from it.

    Asked as two questions that never read a file's contents: what the index has deleted
    since `HEAD`, and which index entries are missing from the directory. Comparing contents
    would run any clean filter the repository's own configuration names, on this machine.
    """
    deleted: set[str] = set()
    for args in (
        ("diff", "--cached", "--name-only", "-z", "--no-renames", "--diff-filter=D", "HEAD"),
        ("ls-files", "--deleted", "-z"),
    ):
        timeout = _timeout()
        if timeout <= 0:
            return set()
        try:
            proc = subprocess.run(
                _git_argv(repo, *args),
                capture_output=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                shell=False,
                env=build_env(passthrough=("HOME", "USERPROFILE")),
            )
        except (subprocess.SubprocessError, OSError):
            return set()
        if proc.returncode != 0:
            return set()
        deleted |= {os.fsdecode(raw) for raw in proc.stdout.split(b"\0") if raw}
    return deleted


def _read_exactly(stream, count: int, sink=None) -> bool:
    while count > 0:
        chunk = stream.read(min(count, _CHUNK))
        if not chunk:
            return False
        if sink is not None:
            sink.write(chunk)
        count -= len(chunk)
    return True


def write_snapshot(repo: Path, out: Path, *, max_total_bytes: int = 2_000_000_000) -> dict:
    """Write the files tracked at `HEAD` under `out`, and return the counts.

    Content comes from git's object store, never from the working tree, so an untracked or
    ignored file cannot enter the copy and neither can an uncommitted edit. A tracked file the
    working tree has deleted stays out as well, counted under `deleted_in_working_tree`, so
    the copy never holds a file the working tree does not. No filter, hook or attribute of
    the repository's is run to produce it.

    `truncated` means the copy is known to be incomplete: the size ceiling was reached, or git
    stopped answering partway. A directory that is not a repository, or has no commit yet,
    yields an empty copy and zero counts. `out` is expected to be a directory of the caller's
    own making, empty or absent.
    """
    out.mkdir(parents=True, exist_ok=True)
    result = {
        "files_written": 0,
        "bytes_written": 0,
        "credential_paths_withheld": 0,
        "deleted_in_working_tree": 0,
        "truncated": False,
    }

    wanted = []
    gone = _deleted_in_working_tree(repo)
    for oid, path in _tracked_blobs(repo):
        if is_credential_path(path):
            result["credential_paths_withheld"] += 1
        elif path in gone:
            result["deleted_in_working_tree"] += 1
        elif is_safe_relative_path(path):
            wanted.append((oid, path))
    timeout = _timeout()
    if not wanted or timeout <= 0:
        return result

    try:
        proc = subprocess.Popen(
            _git_argv(repo, "cat-file", "--batch"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            env=build_env(passthrough=("HOME", "USERPROFILE")),
        )
    except OSError:
        result["truncated"] = True
        return result
    # One question, one answer, so neither pipe can fill while the other is being waited on.
    # The watchdog turns a git that has stopped answering into a short read.
    watchdog = threading.Timer(timeout, proc.kill)
    watchdog.daemon = True
    watchdog.start()
    try:
        for oid, path in wanted:
            try:
                proc.stdin.write(oid.encode("ascii") + b"\n")
                proc.stdin.flush()
                header = proc.stdout.readline().split()
            except OSError:
                header = []
            if len(header) != 3 or header[1] != b"blob" or not header[2].isdigit():
                result["truncated"] = True
                break
            size = int(header[2])
            if result["bytes_written"] + size > max_total_bytes:
                result["truncated"] = True
                break
            # a path the filesystem will not take (a name too long, a file where a directory
            # is needed) is passed over, and its content is still read off the pipe
            try:
                target = out / path
                target.parent.mkdir(parents=True, exist_ok=True)
                sink = open(target, "wb")
            except OSError:
                sink = None
            try:
                complete = _read_exactly(proc.stdout, size, sink)
            finally:
                if sink is not None:
                    sink.close()
            if not (complete and _read_exactly(proc.stdout, 1)):
                if sink is not None:
                    target.unlink(missing_ok=True)
                result["truncated"] = True
                break
            if sink is None:
                continue
            result["files_written"] += 1
            result["bytes_written"] += size
    finally:
        watchdog.cancel()
        proc.kill()
        for pipe in (proc.stdin, proc.stdout):
            try:
                pipe.close()
            except OSError:
                pass
        proc.wait()
    return result
