"""A review reads only the repository and writes only its own output folder.

Each test here reproduces one reported way a symbolic link could carry data across either
boundary: a tracked link that reaches a file outside the repository, and a link waiting in a
reused output folder that redirects a write. Provider turns are stubbed as in
test_review_run.py; nothing here starts a provider command.
"""

import json
import zipfile

import pytest

from hazina_review import run
from hazina_review.lanes import census, mining
from hazina_review.providers import registry
from hazina_review.providers.ask import Turn
from tests.conftest import PY_FILES, _git, make_repo

PROVIDER = registry.PROVIDERS[0]
OUTSIDE_HOLDER = "Faraway Holdings Ltd"


@pytest.fixture
def ready(monkeypatch):
    """Past the two stop conditions, with a provider that answers an empty assessment."""
    empty = {**{name: [] for name in census.CATEGORIES}, "self_contained": 2}
    monkeypatch.setattr(run, "resolve", lambda provider: "/nowhere/provider")
    monkeypatch.setattr(run, "isolation_flags", lambda provider, executable: [])
    monkeypatch.setattr(census, "ask", lambda *a, **k: _turn(json.dumps(empty)))
    monkeypatch.setattr(mining, "ask", lambda *a, **k: _turn("[]"))


def _turn(text):
    return Turn("exited", 0, json.dumps({"result": text}), "")


def _review(repo, out):
    return run.review(repo, out, provider=PROVIDER, model=None, budget_seconds=600)


def _link_repo(tmp_path, links: dict[str, str]):
    """A repository whose `links` (name -> target) are committed as symbolic links."""
    files = {k: v for k, v in PY_FILES.items() if k != "LICENSE"}
    repo = make_repo(tmp_path, files)
    for name, target in links.items():
        (repo / name).symlink_to(target)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "links")
    return repo


def test_a_tracked_licence_link_to_a_file_outside_the_repository_is_not_read(ready, tmp_path):
    outside = tmp_path / "elsewhere" / "LICENSE.txt"
    outside.parent.mkdir()
    outside.write_text(f"Copyright (c) 2024 {OUTSIDE_HOLDER}\n\nMIT License\n", encoding="utf-8")
    repo = _link_repo(tmp_path, {"LICENSE": str(outside)})
    out = tmp_path / "o"

    result = _review(repo, out)

    assert result["error"] is None
    with zipfile.ZipFile(out.parent / run.ZIP_NAME) as archive:
        packed = "".join(archive.read(n).decode("utf-8") for n in archive.namelist())
    assert "Faraway" not in packed
    local = (out / repo.name / "measurement.json").read_text(encoding="utf-8")
    assert "Faraway" not in local


def test_a_link_at_the_repository_output_folder_is_refused_before_anything_runs(
    ready, tmp_path, py_repo
):
    out = tmp_path / "o"
    out.mkdir()
    (out / py_repo.name).symlink_to(py_repo, target_is_directory=True)
    before = sorted(p.name for p in py_repo.iterdir())

    with pytest.raises(run.Refused, match="symbolic link"):
        _review(py_repo, out)

    assert sorted(p.name for p in py_repo.iterdir()) == before
    assert not (out.parent / run.ZIP_NAME).exists()


def test_a_link_at_an_output_file_name_never_overwrites_its_target(ready, tmp_path, py_repo):
    victim = tmp_path / "unrelated.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    out = tmp_path / "o"
    (out / py_repo.name).mkdir(parents=True)
    (out / py_repo.name / "measurement.json").symlink_to(victim)

    with pytest.raises(run.Refused, match="symbolic link"):
        _review(py_repo, out)

    assert victim.read_text(encoding="utf-8") == "keep me\n"


def test_a_link_at_the_zip_never_overwrites_its_target(ready, tmp_path, py_repo):
    victim = tmp_path / "unrelated.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    out = tmp_path / "o"
    out.mkdir()
    (tmp_path / run.ZIP_NAME).symlink_to(victim)

    with pytest.raises(run.Refused, match="symbolic link"):
        _review(py_repo, out)
    assert victim.read_text(encoding="utf-8") == "keep me\n"
