import os
import subprocess

import pytest

from hazina_review import snapshot
from tests.conftest import _git

BINARY = bytes(range(256)) * 3 + b"\r\n\x00\xff\xfe tail"


def _commit_all(repo, msg="more"):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


def _entries(out):
    return sorted(p.relative_to(out).as_posix() for p in out.rglob("*"))


def test_a_tracked_file_is_present_with_identical_bytes(tmp_path, repo_builder):
    repo = repo_builder({"src/app.py": "print('hello')\n", "README.md": "# Demo\n"})
    out = tmp_path / "snap"
    result = snapshot.write_snapshot(repo, out)
    assert (out / "src/app.py").read_bytes() == b"print('hello')\n"
    assert (out / "README.md").read_bytes() == b"# Demo\n"
    assert result == {
        "files_written": 2,
        "bytes_written": len(b"print('hello')\n") + len(b"# Demo\n"),
        "credential_paths_withheld": 0,
        "deleted_in_working_tree": 0,
        "truncated": False,
    }


@pytest.mark.parametrize("staged", [False, True], ids=["unstaged", "staged"])
def test_a_tracked_file_deleted_in_the_working_tree_is_left_out(tmp_path, repo_builder, staged):
    # Still at HEAD, gone from the directory: a reader of the working tree would not find it,
    # so the copy must not hold it either. Staged or not makes no difference.
    repo = repo_builder({"keep.py": "k = 1\n", "gone.py": "g = 1\n", "sub/also.py": "a = 1\n"})
    if staged:
        _git(repo, "rm", "-q", "gone.py", "sub/also.py")
    else:
        (repo / "gone.py").unlink()
        (repo / "sub" / "also.py").unlink()
    out = tmp_path / "snap"
    result = snapshot.write_snapshot(repo, out)
    assert _entries(out) == ["keep.py"]
    assert result["files_written"] == 1 and result["deleted_in_working_tree"] == 2


def test_a_file_renamed_in_the_working_tree_is_left_out_under_its_old_name(tmp_path, repo_builder):
    repo = repo_builder({"old.py": "o = 1\n"})
    _git(repo, "mv", "old.py", "new.py")
    out = tmp_path / "snap"
    result = snapshot.write_snapshot(repo, out)
    # the new name is not at HEAD, so the copy holds neither
    assert _entries(out) == [] and result["deleted_in_working_tree"] == 1


def test_an_untracked_env_file_never_enters_the_snapshot(tmp_path, repo_builder):
    repo = repo_builder({"app.py": "print(1)\n"})
    (repo / ".env").write_text("KEY=LEAK_UNTRACKED\n")
    (repo / ".gitignore").write_text(".env\n")
    out = tmp_path / "snap"
    result = snapshot.write_snapshot(repo, out)
    assert _entries(out) == ["app.py"]
    # never tracked, so there was nothing to withhold: it simply is not there
    assert result["credential_paths_withheld"] == 0


def test_tracked_credential_paths_are_withheld_and_counted(tmp_path, repo_builder):
    repo = repo_builder(
        {
            "app.py": "print(1)\n",
            ".env": "KEY=LEAK_TRACKED\n",
            "svc/credentials/aws": "LEAK_CRED_DIRECTORY\n",
            "keys/server.PEM": "LEAK_PEM\n",
        }
    )
    out = tmp_path / "snap"
    result = snapshot.write_snapshot(repo, out)
    assert _entries(out) == ["app.py"]
    assert result["credential_paths_withheld"] == 3
    assert result["files_written"] == 1


def test_an_uncommitted_edit_does_not_appear(tmp_path, repo_builder):
    repo = repo_builder({"app.py": "print('committed')\n"})
    (repo / "app.py").write_text("print('LEAK_UNCOMMITTED')\n")
    (repo / "staged.py").write_text("LEAK_STAGED = 1\n")
    _git(repo, "add", "staged.py")
    out = tmp_path / "snap"
    snapshot.write_snapshot(repo, out)
    assert (out / "app.py").read_bytes() == b"print('committed')\n"
    assert _entries(out) == ["app.py"]


def test_the_snapshot_holds_no_git_directory(tmp_path, repo_builder):
    repo = repo_builder({"app.py": "print(1)\n", "pkg/mod.py": "X = 1\n"})
    out = tmp_path / "snap"
    snapshot.write_snapshot(repo, out)
    assert not any(part.lower() == ".git" for p in out.rglob("*") for part in p.parts)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symbolic links")
def test_a_tracked_symlink_is_neither_a_link_nor_its_target(tmp_path, repo_builder):
    outside = tmp_path / "outside-key"
    outside.write_text("LEAK_THROUGH_LINK\n")
    repo = repo_builder({"app.py": "print(1)\n"})
    os.symlink(outside, repo / "deploy-key")
    os.symlink("app.py", repo / "alias.py")
    _commit_all(repo)
    out = tmp_path / "snap"
    result = snapshot.write_snapshot(repo, out)
    assert _entries(out) == ["app.py"]
    assert not any(p.is_symlink() for p in out.rglob("*"))
    assert result["files_written"] == 1


def test_a_submodule_is_skipped(tmp_path, repo_builder):
    repo = repo_builder({"app.py": "print(1)\n"})
    head = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{head},vendor/lib")
    _git(repo, "commit", "-q", "-m", "submodule")
    out = tmp_path / "snap"
    assert snapshot.write_snapshot(repo, out)["files_written"] == 1
    assert _entries(out) == ["app.py"]


def test_a_name_with_a_space_and_a_non_ascii_character_round_trips(tmp_path, repo_builder):
    repo = repo_builder({"docs/café notes.md": "crème brûlée\n"})
    out = tmp_path / "snap"
    snapshot.write_snapshot(repo, out)
    assert (out / "docs/café notes.md").read_bytes() == "crème brûlée\n".encode()


def test_a_binary_file_round_trips_byte_for_byte(tmp_path, repo_builder):
    repo = repo_builder({"app.py": "print(1)\n"})
    (repo / "blob.bin").write_bytes(BINARY)
    _commit_all(repo)
    out = tmp_path / "snap"
    result = snapshot.write_snapshot(repo, out)
    assert (out / "blob.bin").read_bytes() == BINARY
    assert result["bytes_written"] == len(BINARY) + len(b"print(1)\n")


def test_line_endings_are_not_converted(tmp_path, repo_builder):
    repo = repo_builder({".gitattributes": "*.txt text eol=crlf\n"})
    (repo / "note.txt").write_bytes(b"one\ntwo\n")
    _commit_all(repo)
    out = tmp_path / "snap"
    snapshot.write_snapshot(repo, out)
    assert (out / "note.txt").read_bytes() == b"one\ntwo\n"


def test_the_size_ceiling_stops_the_copy_and_says_so(tmp_path, repo_builder):
    repo = repo_builder({"a.txt": "a" * 60, "b.txt": "b" * 60, "c.txt": "c" * 60})
    out = tmp_path / "snap"
    result = snapshot.write_snapshot(repo, out, max_total_bytes=130)
    assert result["truncated"] is True
    assert result["files_written"] == 2 and result["bytes_written"] == 120
    assert _entries(out) == ["a.txt", "b.txt"]


def test_a_path_that_would_leave_the_snapshot_is_refused(tmp_path, repo_builder):
    """Git will not stage such a path, but a tree object can be written by hand to hold one."""
    repo = repo_builder({"app.py": "print(1)\n"})
    blob = _git(repo, "rev-parse", "HEAD:app.py").strip()
    inner = _git_in(repo, "mktree", f"100644 blob {blob}\tescaped.txt\n").strip()
    tree = _git_in(repo, "mktree", f"100644 blob {blob}\tapp.py\n040000 tree {inner}\t..\n").strip()
    commit = _git(repo, "commit-tree", tree, "-m", "crafted").strip()
    _git(repo, "update-ref", "HEAD", commit)
    out = tmp_path / "deep" / "snap"
    result = snapshot.write_snapshot(repo, out)
    assert _entries(out) == ["app.py"]
    assert not (tmp_path / "deep" / "escaped.txt").exists()
    assert result["files_written"] == 1


@pytest.mark.parametrize(
    "path",
    [
        "../up.txt",
        "a/../../up.txt",
        "/etc/passwd",
        "a//b",
        "./a",
        ".git/config",
        "sub/.GIT/hooks/x",
        "",
    ],
)
def test_unsafe_relative_paths_are_recognised(path):
    assert not snapshot.is_safe_relative_path(path)


def test_ordinary_relative_paths_are_safe():
    for path in ("app.py", "src/pkg/mod.py", "docs/café notes.md", ".gitignore", "a/..b/c"):
        assert snapshot.is_safe_relative_path(path), path


def test_a_directory_that_is_not_a_repository_yields_an_empty_snapshot(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "app.py").write_text("print(1)\n")
    out = tmp_path / "snap"
    result = snapshot.write_snapshot(plain, out)
    assert result["files_written"] == 0 and _entries(out) == []


def _git_in(repo, command, text):
    proc = subprocess.run(
        ["git", "-C", str(repo), command],
        input=text,
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.environ["PATH"], "HOME": str(repo.parent), "GIT_CONFIG_NOSYSTEM": "1"},
    )
    return proc.stdout
