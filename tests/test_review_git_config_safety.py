"""The review's own git calls start nothing a repository's configuration names either."""

import sys

import pytest

from hazina_review import brief, snapshot
from tests.conftest import fired

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="shell scripts as hooks")


def test_copying_the_tree_starts_nothing_the_repository_configured(tmp_path, armed_repo):
    repo, markers = armed_repo
    result = snapshot.write_snapshot(repo, tmp_path / "copy")
    assert fired(markers) == []
    # and the deletion is still seen without reading any file's contents
    assert result["deleted_in_working_tree"] == 1
    assert not (tmp_path / "copy" / "gone.py").exists()


def test_writing_the_history_starts_nothing_the_repository_configured(tmp_path, armed_repo):
    repo, markers = armed_repo
    brief.write_brief(repo, tmp_path / "history")
    assert fired(markers) == []
