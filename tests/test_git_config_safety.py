"""A repository's own git configuration must not run anything while it is being measured.

`.git/config` travels with a local checkout, and several of its settings name a program for
git to start. `armed_repo` points each at a script that leaves a marker; after the measuring
call, no marker may exist.
"""

import sys

import pytest

from hazina_scan import env
from tests.conftest import _git, fired

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="shell scripts as hooks")


@pytest.mark.parametrize(
    "args",
    [
        ("log", "-p", "--all"),
        ("log", "--all", "--numstat", "--format=%H"),
        ("show", "HEAD"),
        ("diff", "HEAD~1", "HEAD"),
    ],
)
def test_the_shared_git_runner_starts_nothing_the_repository_configured(armed_repo, args):
    repo, markers = armed_repo
    env.run_git(repo, *args)
    assert fired(markers) == []


def test_the_traps_are_real(armed_repo):
    # Guard against a test that passes because nothing could ever fire: plain git, without
    # the overrides, does start the configured programs. The signature checker is left out:
    # it only runs for a signed commit, and these are not signed, so its override is not
    # proven here.
    repo, markers = armed_repo
    _git(repo, "diff", "HEAD")
    _git(repo, "log", "-p", "-1")
    assert {"fsmonitor", "textconv", "extdiff", "clean"} <= set(fired(markers))
