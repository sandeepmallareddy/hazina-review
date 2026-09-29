"""The scanner writes only inside its own output folder, and never through a link.

Each case reproduces a reported way a symbolic link waiting in a reused output folder could
redirect a write: at the per-repository folder, at one of its files, at the local index, at
the zip, or inside the zip as a member.
"""

from __future__ import annotations

import os

import pytest

from hazina_scan import cli, fileguard, orchestrator

posix_only = pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")


# --- writing: the output folder ------------------------------------------------------------


@posix_only
def test_a_link_at_the_repository_output_folder_is_refused(py_repo, tmp_path, capsys):
    out = tmp_path / "o"
    out.mkdir()
    (out / py_repo.name).symlink_to(py_repo, target_is_directory=True)
    before = sorted(p.name for p in py_repo.iterdir())

    assert cli.main([str(py_repo), "--out", str(out), "--no-build"]) == 2

    err = capsys.readouterr().err
    assert "symbolic link" in err and "remove" in err.lower()
    assert sorted(p.name for p in py_repo.iterdir()) == before


@posix_only
@pytest.mark.parametrize("name", ["measurement.json", "codebase_repos.csv", "codebase_repos.json"])
def test_a_link_at_an_output_file_is_refused_and_its_target_kept(py_repo, tmp_path, capsys, name):
    victim = tmp_path / "unrelated.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    out = tmp_path / "o"
    (out / py_repo.name).mkdir(parents=True)
    (out / py_repo.name / name).symlink_to(victim)

    assert cli.main([str(py_repo), "--out", str(out), "--no-build"]) == 2

    assert "symbolic link" in capsys.readouterr().err
    assert victim.read_text(encoding="utf-8") == "keep me\n"


@posix_only
@pytest.mark.parametrize("where", ["index", "zip"])
def test_a_link_at_the_index_or_zip_is_refused_and_its_target_kept(
    py_repo, tmp_path, capsys, where
):
    victim = tmp_path / "unrelated.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    out = tmp_path / "o"
    out.mkdir()
    link = out / cli.INDEX_NAME if where == "index" else tmp_path / cli.ZIP_NAME
    link.symlink_to(victim)

    assert cli.main([str(py_repo), "--out", str(out), "--no-build"]) == 2

    assert "symbolic link" in capsys.readouterr().err
    assert victim.read_text(encoding="utf-8") == "keep me\n"


@posix_only
def test_every_write_refuses_a_link_placed_after_the_check(tmp_path):
    """The second guard: a link that appears between the check and the write is still refused."""
    victim = tmp_path / "unrelated.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    out = tmp_path / "o"
    (out / "r").mkdir(parents=True)
    (out / "r" / "measurement.json").symlink_to(victim)

    with pytest.raises(fileguard.UnsafeOutput, match="symbolic link"):
        orchestrator.write_outputs(out / "r", {"status": "measured"}, {}, out_root=out)
    with pytest.raises(fileguard.UnsafeOutput, match="symbolic link"):
        fileguard.write_text(out / "r" / "measurement.json", "x", out)
    assert victim.read_text(encoding="utf-8") == "keep me\n"


@posix_only
def test_the_zip_refuses_a_linked_member(tmp_path):
    victim = tmp_path / "secret.txt"
    victim.write_text("do not pack\n", encoding="utf-8")
    out = tmp_path / "o"
    (out / "r").mkdir(parents=True)
    (out / "r" / "measurement.json").write_text("{}\n", encoding="utf-8")
    (out / "r" / "extra.json").symlink_to(victim)

    with pytest.raises(fileguard.UnsafeOutput, match="symbolic link"):
        cli.write_zip(out, [("repo-abc", "r")])
    assert not (tmp_path / cli.ZIP_NAME).exists()
