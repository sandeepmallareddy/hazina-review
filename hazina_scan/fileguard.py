"""The one way this package reads a measured repository's files and writes its own.

A checkout is not trusted to be what it looks like. A tracked symbolic link can point at any
file on the machine, a named pipe blocks whoever opens it until something writes to it, and a
device can answer forever. So a file from the repository is read only when it is a regular
file in its own right -- not a link, not a pipe, not a device -- and when its resolved path is
still inside the repository root. Anything else reads as absent: empty text, no bytes, False.

Two checks stand behind every read. The first is made on the path before it is opened
(`os.lstat`, which does not follow a link, and the resolved path against the root). The
second is made by the open itself where the platform offers it: `O_NOFOLLOW` refuses a link
that appeared since the check, `O_NONBLOCK` keeps a pipe that appeared since the check from
blocking the open, and the opened descriptor is checked again as a regular file before a
byte is read. Windows has neither flag, and the first check still stands there.

Directories are walked the same way: a linked directory is never entered, and only regular
files are listed.

Writes go the other way. Every file this package writes lands inside an output directory the
operator chose, and a link found at any output path -- the per-repository folder, a file in
it, the local index, the zip -- is refused rather than written through, because the write
would land wherever the link points. The refusal names the path and what to do about it.
"""

from __future__ import annotations

import io
import os
import stat
from collections.abc import Iterator
from pathlib import Path

#: How a repository file is opened: read only, never through a link, never waiting on a pipe.
#: `O_BINARY` matters on Windows only, where it stops the C runtime translating line endings
#: before Python's own decoding sees them.
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOCTTY", 0)
    | getattr(os, "O_BINARY", 0)
)

#: How an output file is opened: created or truncated, never through a link, and never
#: waiting on a pipe that appeared at the name since it was checked.
_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_TRUNC
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOCTTY", 0)
    | getattr(os, "O_BINARY", 0)
)


class UnsafeOutput(ValueError):
    """An output path is a link, or resolves outside the output directory. Nothing was written."""


# ---------------------------------------------------------------------------
# Reading the repository
# ---------------------------------------------------------------------------


def _inside(path: Path | str, root: Path | str) -> bool:
    """Is `path`, with every link in it resolved, at or under `root` resolved the same way?"""
    try:
        real = Path(os.path.realpath(path))
        base = Path(os.path.realpath(root))
    except (OSError, ValueError):
        return False
    return real == base or real.is_relative_to(base)


def is_regular_file(path: Path | str, root: Path | str | None) -> bool:
    """A regular file, not a link, and inside `root`.

    `root=None` is for a path whose only unchecked component is its own name: a file
    directly under a directory already known to be inside the repository, or one `walk`
    produced (it never enters a linked directory, so what it yields is inside the root it
    started from by construction). The file itself being no link is then what keeps the
    read inside, and the containment check would only repeat itself for every file of a
    large tree.
    """
    try:
        mode = os.lstat(path).st_mode
    except (OSError, ValueError):
        return False
    if not stat.S_ISREG(mode):
        return False
    return root is None or _inside(path, root)


def is_directory(path: Path | str, root: Path | str | None) -> bool:
    """A real directory, not a link to one, and inside `root`."""
    try:
        mode = os.lstat(path).st_mode
    except (OSError, ValueError):
        return False
    if not stat.S_ISDIR(mode):
        return False
    return root is None or _inside(path, root)


def exists(path: Path | str, root: Path | str | None) -> bool:
    """What `Path.exists()` answers, for a regular file or a real directory inside `root` only."""
    return is_regular_file(path, root) or is_directory(path, root)


def open_binary(path: Path | str, root: Path | str | None) -> io.BufferedReader:
    """Open a repository file for reading, or raise `OSError` when it is not one to read."""
    if not is_regular_file(path, root):
        raise OSError(f"not a regular file inside the repository: {path}")
    fd = os.open(path, _READ_FLAGS)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"not a regular file: {path}")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def open_text(
    path: Path | str,
    root: Path | str | None,
    *,
    encoding: str | None = None,
    errors: str | None = None,
) -> io.TextIOWrapper:
    """`open(path, encoding=..., errors=...)` for a repository file, under the same checks.

    The decoding is exactly the built-in `open`'s -- the same default encoding and the same
    newline handling -- so a file reads the same text through here as it did before.
    """
    handle = open_binary(path, root)
    try:
        return io.TextIOWrapper(handle, encoding=encoding, errors=errors)
    except BaseException:
        handle.close()
        raise


def read_bytes(path: Path | str, root: Path | str | None, max_bytes: int = -1) -> bytes:
    """The file's bytes, `max_bytes` at most. Raises `OSError` for anything not read."""
    with open_binary(path, root) as handle:
        return handle.read(max_bytes)


def read_text(
    path: Path | str,
    root: Path | str | None,
    max_chars: int = -1,
    *,
    encoding: str | None = None,
    errors: str | None = "replace",
) -> str:
    """The file's text, `max_chars` characters at most, or "" for anything not read."""
    try:
        with open_text(path, root, encoding=encoding, errors=errors) as handle:
            return handle.read(max_chars)
    except (OSError, ValueError):
        return ""


def walk(root: Path | str) -> Iterator[tuple[str, list[str], list[str]]]:
    """`os.walk(root)`, never entering a linked directory and listing only regular files.

    Top-down and in the same order `os.walk` visits and lists: the caller may prune the
    directory names in place, as with `os.walk`. A link is in neither list, and neither is a
    pipe, a socket or a device, so none of them reaches a reader. The entry types come from
    the directory listing itself, so this costs no more system calls than `os.walk` does.
    """
    stack = [os.fspath(root)]
    while stack:
        top = stack.pop()
        try:
            with os.scandir(top) as listing:
                entries = list(listing)
        except OSError:
            continue
        dirnames: list[str] = []
        filenames: list[str] = []
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    dirnames.append(entry.name)
                elif entry.is_file(follow_symlinks=False):
                    filenames.append(entry.name)
            except OSError:
                continue
        yield top, dirnames, filenames
        stack.extend(os.path.join(top, name) for name in reversed(dirnames))


# ---------------------------------------------------------------------------
# Writing the outputs
# ---------------------------------------------------------------------------


def _link_refused(path: Path, what: str) -> UnsafeOutput:
    return UnsafeOutput(
        f"refusing to write {what} {path}: it is a symbolic link, and writing through it "
        f"would change whatever it points at. Remove the link, or choose an empty "
        f"directory for --out."
    )


def check_output(path: Path | str, out_root: Path | str, what: str = "the output") -> Path:
    """Refuse `path` when it, or any folder between it and `out_root`, is a link, when it
    resolves outside `out_root`, or when it is something other than a file or a folder.
    Returns the path unchanged when it is safe to write.

    `out_root` is the directory the operator chose, already resolved; a link the operator
    used to name it is theirs to use. Everything below it is this tool's to create, so a link
    found there was put there by something else.
    """
    path, out_root = Path(path), Path(out_root)
    outside = UnsafeOutput(
        f"refusing to write {what} {path}: it is outside the output directory {out_root}. "
        f"Choose an empty directory for --out."
    )
    if path == out_root or not path.is_relative_to(out_root):
        raise outside
    here = path
    while here != out_root:
        if here.is_symlink():
            raise _link_refused(here, what)
        here = here.parent
    if not _inside(path, out_root):
        raise outside
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        return path  # not there yet, which is the ordinary case
    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise UnsafeOutput(
            f"refusing to write {what} {path}: it is not a regular file. Remove it, or "
            f"choose an empty directory for --out."
        )
    return path


def make_output_dir(path: Path | str, out_root: Path | str) -> Path:
    """Create an output folder inside `out_root`, refusing a link anywhere on the way."""
    path = check_output(path, out_root, "the output folder")
    path.mkdir(parents=True, exist_ok=True)
    return check_output(path, out_root, "the output folder")


def _open_for_write(path: Path | str, out_root: Path | str) -> int:
    path = check_output(path, out_root, "the output file")
    try:
        fd = os.open(path, _WRITE_FLAGS, 0o666)
    except OSError:
        # `O_NOFOLLOW` refused a link that appeared after the check.
        if path.is_symlink():
            raise _link_refused(path, "the output file") from None
        raise
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise UnsafeOutput(f"refusing to write the output file {path}: not a regular file.")
    return fd


def open_output(path: Path | str, out_root: Path | str, *, newline: str | None = None):
    """Open an output file for writing as UTF-8 text, never through a link."""
    fd = _open_for_write(path, out_root)
    try:
        return os.fdopen(fd, "w", encoding="utf-8", newline=newline)
    except BaseException:
        os.close(fd)
        raise


def open_output_binary(path: Path | str, out_root: Path | str):
    """Open an output file for writing as bytes, never through a link."""
    fd = _open_for_write(path, out_root)
    try:
        return os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        raise


def write_text(path: Path | str, text: str, out_root: Path | str) -> Path:
    """`Path.write_text(text, encoding="utf-8")` for an output file, never through a link.

    Lines end in "\n" on every platform. Windows would otherwise write "\r\n", and the record
    id is computed over the bytes as written, as the receiver reads them: a row that does not
    open with `{\n  "record_id"` cannot be sealed.
    """
    with open_output(path, out_root, newline="\n") as handle:
        handle.write(text)
    return Path(path)


def read_output_text(path: Path | str, out_root: Path | str) -> str | None:
    """An earlier run's output file, or None when there is none. Refuses a link like a write."""
    path = check_output(path, out_root, "the output file")
    if not path.exists():
        return None
    with open_binary(path, None) as handle:
        return handle.read().decode("utf-8")
