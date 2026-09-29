"""The scanner reads only regular files inside the repository, and never through a link.

Each case reproduces a reported way a symbolic link or a special file could carry the scan
across the repository's boundary or stall it: a link to a file outside the repository whose
content would be measured and written out, and a link to a FIFO, or a FIFO itself, that
would block a reader for good while the run's budget waits on it.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from hazina_scan import fileguard, identity, orchestrator, structure, tree
from tests.conftest import PY_FILES, _git, make_repo

posix_only = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs and symlinks")


def _finishes(fn, fifos, seconds: float = 20.0):
    """Run `fn` in a thread and say whether it came back within `seconds`.

    A reader stuck on one of `fifos` is let go afterwards by opening the other end, again and
    again until the thread is done, so a failing test never leaves the suite hanging on a
    worker the interpreter would wait for at exit.
    """
    box: dict = {}

    def target():
        box["value"] = fn()

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(seconds)
    finished = not worker.is_alive()
    deadline = time.monotonic() + 120
    while worker.is_alive() and time.monotonic() < deadline:
        for fifo in fifos:
            try:
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass
        worker.join(0.05)
    return finished, box.get("value")


# --- reading: special files --------------------------------------------------------------


@posix_only
def test_a_source_file_linked_to_a_fifo_does_not_stall_the_tree_walk(tmp_path):
    repo = make_repo(tmp_path, {"main.py": "print('hi')\n"})
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    (repo / "source.py").symlink_to(fifo)

    done, block = _finishes(lambda: tree.collect(repo), [fifo])

    assert done, "the tree walk blocked on a link to a FIFO"
    assert block["loc_by_language"] == {"Python": 1}


@posix_only
def test_a_fifo_in_the_working_tree_is_skipped_by_every_reader(tmp_path):
    repo = make_repo(tmp_path, dict(PY_FILES))
    fifos = [repo / name for name in ("src/demo/stuck.py", "package.json", "LICENSE-2")]
    fifos += [repo / "README.txt", repo / "Cargo.toml"]
    for fifo in fifos:
        os.mkfifo(fifo)

    for fn in (
        lambda: tree.collect(repo),
        lambda: identity.collect(repo),
        lambda: structure.collect(repo),
        lambda: orchestrator.repo_digest(repo),
    ):
        done, _ = _finishes(fn, fifos)
        assert done


@posix_only
def test_the_whole_measurement_finishes_past_a_linked_fifo(tmp_path):
    repo = make_repo(tmp_path, dict(PY_FILES))
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    for name in ("source.py", "package.json", "go.mod", "README.rst"):
        (repo / name).symlink_to(fifo)

    done, _ = _finishes(lambda: orchestrator.measure(repo), [fifo], seconds=60)

    assert done


# --- reading: links that leave the repository ----------------------------------------------


@posix_only
def test_a_licence_link_to_a_file_outside_the_repository_is_not_read(tmp_path):
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("Copyright (c) 2024 Faraway Holdings Ltd\n", encoding="utf-8")
    repo = make_repo(tmp_path, {"main.py": "x = 1\n"})
    (repo / "LICENSE").symlink_to(outside)
    (repo / "README.md").symlink_to(outside)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "links")

    assert identity.from_licence(repo) == []
    assert "faraway" not in identity._readme_text(repo)
    assert "Faraway" not in str(identity.collect(repo))


@posix_only
def test_the_guard_reads_a_regular_file_inside_and_nothing_else(tmp_path):
    root = tmp_path / "repo"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.txt").write_text("inside\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (root / "link.txt").symlink_to(outside)
    (root / "inner_link.txt").symlink_to(root / "sub" / "a.txt")
    (root / "linked_dir").symlink_to(tmp_path, target_is_directory=True)

    assert fileguard.read_text(root / "sub" / "a.txt", root) == "inside\n"
    assert fileguard.read_bytes(root / "sub" / "a.txt", root, 3) == b"ins"
    assert fileguard.read_text(root / "link.txt", root) == ""
    assert fileguard.read_text(root / "inner_link.txt", root) == ""
    assert fileguard.read_text(root / "linked_dir" / "outside.txt", root) == ""
    assert not fileguard.exists(root / "linked_dir", root)
    assert fileguard.exists(root / "sub", root) and fileguard.is_directory(root / "sub", root)
    with pytest.raises(OSError):
        fileguard.read_bytes(root / "link.txt", root)


@posix_only
def test_a_linked_directory_is_never_walked_or_read_through(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "big.py").write_text("x = 1\n" * 50, encoding="utf-8")
    (outside / "package.json").write_text('{"dependencies": {"react": "1"}}', encoding="utf-8")
    repo = make_repo(tmp_path, {"main.py": "x = 1\n"})
    (repo / "vendor_link").symlink_to(outside, target_is_directory=True)
    (repo / "linked.py").symlink_to(outside / "big.py")

    block = tree.collect(repo)

    assert block["total_loc"] == 1
    assert fileguard.read_text(repo / "vendor_link" / "package.json", repo) == ""
    assert not fileguard.is_regular_file(repo / "vendor_link" / "package.json", repo)
    assert not fileguard.is_regular_file(repo / "linked.py", repo)
    assert fileguard.is_regular_file(repo / "main.py", repo)


def test_an_ordinary_repository_measures_the_same(py_repo):
    """The guard changes nothing for a tree with no links and no special files in it."""
    block = tree.collect(py_repo)
    assert block["loc_by_language"] == {"Python": 7}
    assert block["ci_present"] is True
    assert identity.from_licence(py_repo) == ["Acme Corp"]
