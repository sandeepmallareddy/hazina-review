from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="run tests that make real provider calls (these cost money)",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="needs --live; real provider calls cost money")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


def _git(repo: Path, *args: str, env: dict | None = None) -> str:
    base = {
        "PATH": os.environ["PATH"],
        "HOME": str(repo.parent),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if env:
        base.update(env)
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=base, check=True
    )
    return proc.stdout


def make_repo(
    root: Path,
    files: dict[str, str],
    commits: list[dict] | None = None,
    remote: str | None = None,
    name: str = "repo",
) -> Path:
    """Build a git repository under `root`.

    files    initial working-tree contents, path -> text.
    commits  optional list of {"msg", "files" (path -> text), "name", "email", "date"};
             each becomes one commit. When omitted, `files` is committed once as
             "initial" by Dev One <dev1@example.com> on 2024-01-15.
    remote   optional origin URL.
    name     directory name under `root` to create the repository at (default "repo").
    """
    repo = root / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Dev One")
    _git(repo, "config", "user.email", "dev1@example.com")
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    if commits is None:
        commits = [
            {
                "msg": "initial",
                "files": {},
                "name": "Dev One",
                "email": "dev1@example.com",
                "date": "2024-01-15T10:00:00+00:00",
            }
        ]
    for c in commits:
        for rel, text in c.get("files", {}).items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        _git(repo, "add", "-A")
        stamp = c.get("date", "2024-01-15T10:00:00+00:00")
        env = {
            "GIT_AUTHOR_NAME": c.get("name", "Dev One"),
            "GIT_AUTHOR_EMAIL": c.get("email", "dev1@example.com"),
            "GIT_COMMITTER_NAME": c.get("name", "Dev One"),
            "GIT_COMMITTER_EMAIL": c.get("email", "dev1@example.com"),
            "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_DATE": stamp,
        }
        _git(repo, "commit", "-q", "--allow-empty", "-m", c["msg"], env=env)
    if remote:
        _git(repo, "remote", "add", "origin", remote)
    return repo


@pytest.fixture
def repo_builder(tmp_path):
    def build(files, commits=None, remote=None, name="repo"):
        return make_repo(tmp_path, files, commits, remote, name)

    return build


PY_FILES = {
    "pyproject.toml": '[project]\nname = "demo"\nversion = "0.1"\n'
    'dependencies = ["requests"]\n'
    '[project.optional-dependencies]\ndev = ["pytest", "pytest-cov"]\n',
    "src/demo/__init__.py": "",
    "src/demo/core.py": "def add(a, b):\n    if a > b:\n        return a + b\n    return b + a\n",
    "tests/test_core.py": (
        "from demo.core import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    ),
    "README.md": "# Demo\n\n## Installation\n\npip install demo\n\n## Usage\n\nimport demo\n",
    "LICENSE": "Copyright (c) 2024 Acme Corp\n\nMIT License\n",
    ".github/workflows/ci.yml": "name: ci\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
    "    steps:\n      - uses: actions/checkout@v4\n"
    "      - run: pip install -e .\n"
    "      - run: pytest\n",
}


@pytest.fixture
def py_repo(tmp_path):
    return make_repo(tmp_path, PY_FILES, remote="git@github.com:acme/demo.git")


def work(n: int, lines: int = 12) -> dict[str, str]:
    """Two source files rewritten whole: a commit of these clears the history brief's bar."""
    return {
        name: "".join(f"{name[4]}{n}_{i} = {i}\n" for i in range(lines))
        for name in ("src/engine.py", "src/rules.py")
    }


def _keys(marker: str) -> str:
    return "".join(f"KEY_{i}={marker}\n" for i in range(12))


@pytest.fixture
def git_repo_with_env_file(tmp_path):
    return make_repo(
        tmp_path,
        {**work(0), ".env": "SUPER_SECRET_VALUE=1\n"},
        name="with-env",
    )


@pytest.fixture
def git_repo_with_many_commits(tmp_path):
    commits = [{"msg": f"step {n}", "files": work(n)} for n in range(40)]
    return make_repo(tmp_path, {}, commits, name="many")


@pytest.fixture
def git_repo_with_credential_history(tmp_path):
    """Credential-shaped files in every position a history can put them: added, modified and
    deleted; nested; differently cased; renamed; alone in a commit; under a directory that is
    itself credential-shaped. Each carries its own `LEAK_` marker, and every commit is large
    enough for the brief to want a patch of it."""
    commits = [
        {
            "msg": "add",
            "files": {**work(1), ".env": _keys("LEAK_ADDED"), "keys/server.PEM": _keys("LEAK_PEM")},
        },
        {"msg": "modify", "files": {**work(2), ".env": _keys("LEAK_MODIFIED")}},
    ]
    repo = make_repo(tmp_path, {}, commits, name="credential-history")

    def commit(msg: str, files: dict[str, str]) -> None:
        for rel, text in files.items():
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", msg)

    _git(repo, "rm", "-q", ".env", "keys/server.PEM")
    commit("delete", {})
    commit(
        "nested",
        {
            **work(3),
            "config/deep/.env": _keys("LEAK_NESTED"),
            ".ENV.Production": _keys("LEAK_UPPERCASE"),
            "my dir/secrets.yml": _keys("LEAK_SPACED"),
        },
    )
    _git(repo, "mv", "config/deep/.env", "config/deep/.env.bak")
    commit("rename", {".ENV.Production": _keys("LEAK_REWRITTEN")})
    commit(
        "alone",
        {"config/deep/.env.bak": _keys("LEAK_ALONE"), "my dir/secrets.yml": _keys("LEAK_AGAIN")},
    )
    commit(
        "directories",
        {".env/prod": _keys("LEAK_ENV_DIRECTORY"), "svc/credentials/aws": _keys("LEAK_CRED_DIR")},
    )
    return repo


# --- a repository whose own git configuration tries to start programs ------------------------

#: Settings a `.git/config` can use to make git start a program, each pointed at a script that
#: leaves a marker file. `log.showSignature` rides along; its checker only runs for a signed
#: commit, so no marker for it is expected here.
_TRAPS = {
    "fsmonitor": ("core.fsmonitor",),
    "gpg": ("gpg.program",),
    "textconv": ("diff.trap.textconv",),
    "extdiff": ("diff.external", "diff.trap.command"),
    "clean": ("filter.trap.clean",),
}


@pytest.fixture
def armed_repo(tmp_path, repo_builder):
    """`(repo, markers)`: every program-starting setting set to a trap, a dirty working tree."""
    repo = repo_builder({"src/app.py": "a = 1\n", "src/lib.py": "b = 2\n", "gone.py": "g = 1\n"})
    (repo / ".gitattributes").write_text("* diff=trap filter=trap\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "attributes")
    (repo / "src/app.py").write_text("a = 1\nb = 3\nc = 4\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "second")
    markers = {}
    for name, keys in _TRAPS.items():
        markers[name] = tmp_path / f"{name}.ran"
        script = tmp_path / f"{name}.sh"
        script.write_text(f"#!/bin/sh\ntouch '{markers[name]}'\ncat >/dev/null 2>&1\nexit 0\n")
        script.chmod(0o755)
        for key in keys:
            _git(repo, "config", key, str(script))
    _git(repo, "config", "log.showSignature", "true")
    # dirty, so any command comparing the working tree has to look at file contents
    (repo / "src/lib.py").write_text("b = 2\n# edited\n")
    os.utime(repo / "src/lib.py", (1, 1))
    (repo / "gone.py").unlink()
    return repo, markers


def fired(markers: dict) -> list[str]:
    return sorted(name for name, marker in markers.items() if marker.exists())
