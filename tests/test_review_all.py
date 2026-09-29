"""`--all DIR`: every repository directly inside one folder, as a client with a hundred of them
runs it.

Nothing here starts a provider command: the checks and the run are replaced at their seams, or
the lanes' turns are, as in the batch tests.
"""

import io

import pytest

from hazina_review import cli, preflight, run, support
from hazina_scan import cli as scan
from tests.conftest import make_repo
from tests.test_review_batch import RATE_LIMITED, _Fake, _repos, _states
from tests.test_review_run import (  # noqa: F401 -- fixtures
    _builds_nothing_unless_asked,
    _provider_checks_pass,
    ready,
)


def _folder(tmp_path):
    """`code/` holding two repositories out of name order, a plain folder, a folder with a
    repository inside it, a hidden repository and a file."""
    root = tmp_path / "code"
    root.mkdir()
    make_repo(root, {"a.py": "x = 1\n"}, name="zeta")
    make_repo(root, {"a.py": "x = 1\n"}, name="alpha")
    (root / "notes").mkdir()
    (root / "group").mkdir()
    make_repo(root / "group", {"a.py": "x = 1\n"}, name="inner")
    make_repo(root, {"a.py": "x = 1\n"}, name=".hidden")
    (root / "readme.txt").write_text("hi\n")
    return root


# --- finding the repositories -----------------------------------------------------------------


def test_only_immediate_git_subfolders_are_found_sorted_by_name(tmp_path):
    root = _folder(tmp_path)
    found, skipped = scan.repos_in(root)
    assert found == [root / "alpha", root / "zeta"]
    # nested repositories are not looked for; hidden folders and files are not folders to skip
    assert skipped == [root / "group", root / "notes"]


def test_the_scanner_finds_the_same_repositories(tmp_path):
    root = _folder(tmp_path)
    args = scan.build_parser().parse_args(["--all", str(root)])
    assert scan.select_repos(args, io.StringIO()) == [root / "alpha", root / "zeta"]


# --- the command line ---------------------------------------------------------------------------


def _handed(monkeypatch, *, checks=None):
    """What the checks and the run are handed, with both replaced."""
    seen = {}

    def _checks(provider, model, repos, out, **kwargs):
        seen["checked"] = list(repos)
        return checks or [preflight.Result(preflight.GIT, preflight.OK, "Fine.")]

    def _review_all(repos, out_dir, **options):
        seen["reviewed"] = list(repos)
        seen["options"] = options
        return {
            "results": [],
            "zip": None,
            "asked": 0,
            "stopped": None,
            "stop_kind": None,
            "repos": [],
            "out_dir": out_dir,
            "provider": options["provider"],
            "model": options["model"],
        }

    monkeypatch.setattr(preflight, "run_checks", _checks)
    monkeypatch.setattr(run, "review_all", _review_all)
    return seen


def test_all_reviews_every_repository_found_and_says_once_what_it_skipped(
    monkeypatch, tmp_path, capsys
):
    root = _folder(tmp_path)
    seen = _handed(monkeypatch)
    assert cli.main(["--all", str(root), "--out", str(tmp_path / "o")]) == 0
    assert seen["reviewed"] == [root / "alpha", root / "zeta"]
    err = capsys.readouterr().err
    assert err.count("Skipped") == 1
    assert f"Skipped 2 folders in {root} that are not git repositories." in err
    assert "notes" not in err and "group" not in err
    # the log may name them, so the run is handed them
    assert seen["options"]["skipped_folders"] == ["group", "notes"]


def test_one_folder_skipped_is_said_in_the_singular(monkeypatch, tmp_path, capsys):
    root = tmp_path / "code"
    root.mkdir()
    make_repo(root, {"a.py": "x = 1\n"}, name="api")
    (root / "notes").mkdir()
    _handed(monkeypatch)
    assert cli.main(["--all", str(root), "--out", str(tmp_path / "o"), "--check"]) == 0
    said = capsys.readouterr().out
    assert f"Skipped 1 folder in {root} that is not a git repository." in said


def test_all_combines_with_named_repositories_and_reviews_each_once(monkeypatch, tmp_path):
    root = _folder(tmp_path)
    other = make_repo(tmp_path, {"a.py": "x = 1\n"}, name="other")
    seen = _handed(monkeypatch)
    argv = [str(other), "--all", str(root), str(root / "zeta"), str(root / "alpha" / ".")]
    assert cli.main([*argv, "--out", str(tmp_path / "o")]) == 0
    assert seen["reviewed"] == [root / "alpha", root / "zeta", other]
    assert seen["checked"] == seen["reviewed"]


@pytest.mark.parametrize("check_only", [False, True])
def test_no_repository_found_stops_before_any_check_or_session(
    monkeypatch, tmp_path, capsys, check_only
):
    root = tmp_path / "code"
    root.mkdir()
    (root / "notes").mkdir()

    def _never(*a, **k):
        pytest.fail("a check or a session was started with no repository to review")

    monkeypatch.setattr(preflight, "run_checks", _never)
    monkeypatch.setattr(preflight, "check_model", _never)
    monkeypatch.setattr(run, "review_all", _never)
    extra = ["--check"] if check_only else []
    assert cli.main(["--all", str(root), "--out", str(tmp_path / "o"), *extra]) == 2
    said = capsys.readouterr()
    assert (
        f"No git repositories found directly inside {root}. Point --all at the folder that "
        "holds your repositories." in said.err
    )
    assert said.out == "" and "Traceback" not in said.err
    assert not (tmp_path / "o").exists()


def test_all_on_something_that_is_not_a_folder_is_refused(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(preflight, "run_checks", lambda *a, **k: pytest.fail("checked"))
    assert cli.main(["--all", str(tmp_path / "nowhere")]) == 2
    assert "not a folder" in capsys.readouterr().err


def test_all_cannot_be_given_with_resume(tmp_path, capsys):
    assert cli.main(["--resume", str(tmp_path / "o"), "--all", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "--all" in err and "--resume" in err
    assert not (tmp_path / "o").exists()


@pytest.mark.usefixtures("ready")
def test_resume_carries_on_with_the_recorded_list_not_the_folder_as_it_is_now(
    monkeypatch, tmp_path, capsys
):
    fake = _Fake(monkeypatch, {"second": RATE_LIMITED})
    root = tmp_path / "code"
    root.mkdir()
    _repos(root, ("first", "second", "third"))
    (root / "notes").mkdir()
    out = tmp_path / "o"
    assert cli.main(["--all", str(root), "--out", str(out)]) == 2
    assert "skipped, not git repositories: notes" in (out / support.LOG_NAME).read_text()
    # the folder changes before the resume: a new repository appears
    make_repo(root, {"marker.py": "name = 'late'\n"}, name="late")
    fake.failing.clear()
    fake.asked.clear()
    assert cli.main(["--resume", str(out)]) == 0
    assert fake.repos_asked() == ["second", "third"]
    assert _states(out) == {"first": "done", "second": "done", "third": "done"}


# --- the checklist and the note before a long batch --------------------------------------------


def _repo_lines(names, problems=()):
    """One repository line each: fine, or a warning for a name in `problems`."""
    return [
        preflight.Result(f"{preflight.REPO} {name}", preflight.WARNING, "No commits.", "Commit.")
        if name in problems
        else preflight.Result(f"{preflight.REPO} {name}", preflight.OK, "Fine.")
        for name in names
    ]


def _said(results, repos, log=None):
    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    preflight.say(results, stream, repos=repos, log=log)
    return stream.buffer.getvalue().decode("utf-8").splitlines()


def test_five_repositories_keep_a_line_each():
    names = [f"r{i}" for i in range(5)]
    lines = _said(_repo_lines(names, problems={"r2"}), repos=5)
    assert [line.split(":")[0] for line in lines if not line.startswith(" ")] == [
        f"{'!' if name == 'r2' else '✓'} Repository {name}" for name in names
    ]


def test_more_than_five_are_one_line_and_a_line_for_each_problem():
    names = [f"r{i}" for i in range(124)]
    git = preflight.Result(preflight.GIT, preflight.OK, "git 2.4.")
    output = preflight.Result(preflight.OUTPUT, preflight.OK, "Can be written.")
    results = [git, *_repo_lines(names, problems={"r7", "r50"}), output]
    lines = _said(results, repos=124, log="o/hazina-review-log.txt")
    assert lines[0].startswith("✓ git")
    assert lines[1] == "✓ Repositories: 122 of 124 found are git repositories with commits."
    assert lines[2] == "! Repository r7: No commits." and lines[3] == "    Commit."
    assert lines[4] == "! Repository r50: No commits." and lines[5] == "    Commit."
    assert lines[6].startswith("✓ Output directory") and len(lines) == 7


def test_more_than_five_all_fine_is_one_line():
    names = [f"r{i}" for i in range(6)]
    assert _said(_repo_lines(names), repos=6) == [
        "✓ Repositories: 6 found, each a git repository with commits."
    ]


def test_more_than_ten_problems_name_ten_and_point_to_the_support_file():
    names = [f"r{i:02d}" for i in range(30)]
    results = _repo_lines(names, problems=set(names[:13]))
    lines = _said(results, repos=30, log="o/hazina-review-log.txt")
    named = [line for line in lines if line.startswith("! ")]
    assert len(named) == 10
    assert lines[-1] == "  and 3 more (listed in the support file, o/hazina-review-log.txt)"


def test_with_no_support_file_to_come_every_problem_is_shown():
    # A --check, or a check that stops the run, writes no log: the screen is the only record,
    # and the person is told to copy these lines to us.
    names = [f"r{i:02d}" for i in range(30)]
    lines = _said(_repo_lines(names, problems=set(names[:13])), repos=30)
    assert len([line for line in lines if line.startswith("! ")]) == 13
    assert not any("more" in line for line in lines)


def test_a_long_batch_is_told_roughly_how_long_it_takes(monkeypatch, tmp_path, capsys):
    root = tmp_path / "code"
    root.mkdir()
    for i in range(6):
        make_repo(root, {"a.py": "x = 1\n"}, name=f"r{i}")
    _handed(monkeypatch)
    assert cli.main(["--all", str(root), "--out", str(tmp_path / "o")]) == 0
    err = capsys.readouterr().err
    assert (
        "6 repositories: roughly 15 to 60 minutes each; you can stop with Ctrl-C and carry on "
        "later with --resume." in err
    )
    assert "$" not in err


def test_a_short_batch_and_a_check_are_not_told(monkeypatch, tmp_path, capsys):
    root = tmp_path / "code"
    root.mkdir()
    for i in range(6):
        make_repo(root, {"a.py": "x = 1\n"}, name=f"r{i}")
    _handed(monkeypatch)
    assert cli.main(["--all", str(root), "--out", str(tmp_path / "o"), "--check"]) == 0
    five = [str(root / f"r{i}") for i in range(5)]
    assert cli.main([*five, "--out", str(tmp_path / "o")]) == 0
    said = capsys.readouterr()
    assert "roughly" not in said.out + said.err


def test_a_run_of_many_shows_the_compact_checklist(monkeypatch, tmp_path, capsys):
    root = tmp_path / "code"
    root.mkdir()
    for i in range(6):
        make_repo(root, {"a.py": "x = 1\n"}, name=f"r{i}")
    monkeypatch.setattr(run, "review_all", lambda *a, **k: pytest.fail("the run began"))
    # the real checks; the provider's are answered as a working command would (autouse)
    assert cli.main(["--all", str(root), "--out", str(tmp_path / "o"), "--check"]) == 0
    out = capsys.readouterr().out
    assert "✓ Repositories: 6 found, each a git repository with commits." in out
    assert "Repository r0" not in out
