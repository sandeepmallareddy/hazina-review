"""The record id: a check, carried in each repository's own files, that they are as written.

Each repository's `codebase_repos.json` opens with one line, `  "record_id": "<32 hex>",`.
The id is the first 32 lowercase hex characters of an HMAC-SHA256 over the four files of that
repository, taken as the exact bytes written to disk, with the id itself blanked to 32 zeros.
Whoever holds the key can tell a file changed after the run from one the run wrote; without
the key the id cannot be recomputed. The key lives in this module, lightly disguised: that
keeps it out of a casual search, and is not meant to stop someone determined.

The message, in full:

    b"hazina-record-v1\\n"
    then, for each of the four names in sorted order,
    name + b"\\n" + str(len(data)) + b"\\n" + data + b"\\n"

`compute`, `verify` and `strip` are the whole of the format. The receiving side's Python checks
and this tool's own tests use them; `seal` is what a run calls once the four files are on disk.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable
from pathlib import Path

from hazina_scan import fileguard

#: The four files of one repository, in the order they are hashed.
FILES = (
    "codebase_repo_mining.json",
    "codebase_repos.csv",
    "codebase_repos.json",
    "measurement.json",
)
ROW = "codebase_repos.json"
VERSION = b"hazina-record-v1\n"
LENGTH = 32
#: What the id is written as before it is known, and what it counts as while hashing.
BLANK = "0" * LENGTH

_LINE = re.compile(rb'\A(\{\n  "record_id": ")([0-9a-f]{32})(",\n)')

# The key, as two halves that mean nothing until each is combined with a mask derived below.
_HIGH = (149, 97, 87, 22, 190, 69, 134, 177, 202, 9, 184, 187, 150, 17, 165, 88)
_LOW = (112, 204, 127, 100, 203, 60, 125, 121, 53, 129, 54, 24, 169, 182, 24, 241)


def production_key() -> bytes:
    """The 32-byte key a run signs with."""
    mask = hashlib.sha256(b"hazina-record-v1 mask").digest()
    return bytes(a ^ b for a, b in zip((*_HIGH, *_LOW), mask, strict=True))


def _key_bytes(key: bytes | str) -> bytes:
    return key.encode("utf-8") if isinstance(key, str) else bytes(key)


def _record_line(row: bytes) -> re.Match:
    found = _LINE.match(row)
    if found is None:
        raise ValueError(f"{ROW} does not open with its record_id line")
    return found


def compute(files: dict[str, bytes], key: bytes | str) -> str:
    """The record id of one repository's four files, as bytes, under `key`.

    `files` maps each of the four names to its exact bytes. The id already in the row, if
    any, is blanked before hashing, so the files as written and as they were before the id
    went in give the same answer. Raises `ValueError` when the names are not exactly the four
    or the row does not open with its record_id line. A text key is taken as its UTF-8 bytes.
    """
    if set(files) != set(FILES):
        raise ValueError(f"expected exactly the files {', '.join(FILES)}")
    message = [VERSION]
    for name in FILES:
        data = bytes(files[name])
        if name == ROW:
            found = _record_line(data)
            data = found[1] + BLANK.encode("ascii") + found[3] + data[found.end() :]
        message += [name.encode("utf-8"), b"\n", str(len(data)).encode("ascii"), b"\n"]
        message += [data, b"\n"]
    digest = hmac.new(_key_bytes(key), b"".join(message), hashlib.sha256).hexdigest()
    return digest[:LENGTH]


def verify(files: dict[str, bytes], keys: Iterable[bytes | str] | bytes | str) -> bool:
    """True when the id written in the row is the one `compute` gives under any of `keys`.

    False, never an exception, for anything else: a changed byte in any file, a missing or
    extra file, a row without its record_id line, or no key that fits. Several keys are
    accepted so that a key can be replaced without refusing what the old one signed.
    """
    if isinstance(keys, (bytes, str)):
        keys = [keys]
    try:
        written = _record_line(bytes(files[ROW]))[2].decode("ascii")
        return any(hmac.compare_digest(compute(files, key), written) for key in keys)
    except (KeyError, TypeError, ValueError):
        return False


def strip(row: bytes) -> bytes:
    """`codebase_repos.json` without its record_id line: exactly what the row is when written
    without one. Raises `ValueError` when the row does not open with that line."""
    found = _record_line(row)
    return found[1][:2] + row[found.end() :]


def seal(folder: Path, out_root: Path) -> str:
    """Write the record id into `folder`'s row, over the blank it was written with.

    The four files are read back exactly as they are on disk, the id is computed over them,
    and the blank is replaced in place, byte for byte and at the same length: nothing is
    serialised again after hashing. Returns the id.
    """
    from hazina_review import emit

    files = {}
    for name in FILES:
        path = fileguard.check_output(Path(folder) / name, out_root, "the output file")
        with fileguard.open_binary(path, None) as handle:
            files[name] = handle.read()
    row = files[ROW]
    found = _record_line(row)
    if found[2] != BLANK.encode("ascii"):
        raise ValueError(f"{ROW} was not written with a blank record_id")
    value = emit.record_id(compute(files, production_key()))
    sealed = found[1] + value.encode("ascii") + found[3] + row[found.end() :]
    with fileguard.open_output_binary(Path(folder) / ROW, out_root) as handle:
        handle.write(sealed)
    return value
