"""One run, end to end, with the provider turn stubbed at the lane's seam.

Nothing here starts a provider command. `census.ask` is replaced in every test that reaches
it, and the two checks that come before anything else are replaced wherever a test has to
get past them.
"""

import json
import zipfile

import pytest

from hazina_review import brief, cli, preflight, record, run, snapshot, support
from hazina_review.lanes import census, mining
from hazina_review.providers import ask as ask_mod
from hazina_review.providers import registry
from hazina_review.providers.ask import Turn
from hazina_scan import cli as scan_cli
from hazina_scan import orchestrator
from tests.conftest import PY_FILES, make_repo, work

PROVIDER = registry.PROVIDERS[0]
GOOD = "add cursor pagination to a list endpoint backed by a relational store"
NAMED = "rewrite src/auth/session.py to use rotating tokens"


def _assessment(**changes):
    return {**{name: [] for name in census.CATEGORIES}, "self_contained": 2, **changes}


def _turn(answer):
    """A turn that exited cleanly with `answer` as the model's words."""
    return Turn("exited", 0, json.dumps({"result": json.dumps(answer)}), "")


def _answers(answer, note=None):
    if note == "timeout":
        return lambda *args, **kwargs: Turn("timeout")
    if note == "unparseable":
        return lambda *args, **kwargs: Turn("exited", 0, json.dumps({"result": "no answer"}))
    return lambda *args, **kwargs: _turn(answer)


@pytest.fixture(autouse=True)
def _provider_checks_pass(monkeypatch):
    """The checks before a run that ask the provider command, answered as a working command
    would answer them. The repository and output checks stay real: the run is stopped by them
    before anything is written, and the tests below hold that true."""
    ok = [preflight.Result(name, preflight.OK, "Fine.") for name in ("1", "2", "3", "4")]
    monkeypatch.setattr(preflight, "provider_checks", lambda provider: list(ok))
    monkeypatch.setattr(
        preflight, "check_model", lambda *a, **k: preflight.Result("7", preflight.OK, "Fine.")
    )


@pytest.fixture(autouse=True)
def _builds_nothing_unless_asked():
    """The command line runs the build check by default, and the build check executes the
    repository's own install and test commands. The tests that drive the command line are
    about something else, so a run they start without a build flag builds nothing; the tests
    of the build check put the real default back, or name a level. Imported by every test file
    that drives the command line.

    Set and put back by hand rather than with `monkeypatch`: asking for that fixture here
    would change the order in which other fixtures' own replacements are undone.
    """
    was = cli.DEFAULTS["build_level"]
    cli.DEFAULTS["build_level"] = "none"
    try:
        yield
    finally:
        cli.DEFAULTS["build_level"] = was


@pytest.fixture
def ready(monkeypatch):
    """Past the two stop conditions, with a provider that answers one usable sentence."""
    monkeypatch.setattr(run, "resolve", lambda provider: "/nowhere/provider")
    monkeypatch.setattr(run, "isolation_flags", lambda provider, executable: [])
    monkeypatch.setattr(census, "ask", _answers(_assessment(complex_logic=[GOOD, NAMED])))
    monkeypatch.setattr(mining, "ask", _answers([]))


def _review(repo, out, **overrides):
    options = {"provider": PROVIDER, "model": None, "budget_seconds": 600, **overrides}
    return run.review(repo, out, **options)


#: The two files a run keeps beside the folders for itself, never packed.
SUPPORT_FILES = (support.PROGRESS_NAME, support.LOG_NAME)


def _folders(out):
    return sorted(p.name for p in out.iterdir() if p.is_dir())


def _zip_names(out):
    with zipfile.ZipFile(out.parent / run.ZIP_NAME) as archive:
        return archive.namelist()


# --- the two stop conditions, and what is refused at the command line ---------------------


@pytest.mark.parametrize("stop", [registry.ProviderUnavailable, registry.ProviderNotIsolated])
def test_a_stop_condition_ends_the_run_before_anything_is_measured(
    monkeypatch, tmp_path, py_repo, capsys, stop
):
    def _stops(*_):
        raise stop("the command cannot be used on this machine")

    monkeypatch.setattr(run, "resolve", lambda provider: "/nowhere/provider")
    monkeypatch.setattr(
        run, "resolve" if stop is registry.ProviderUnavailable else "isolation_flags", _stops
    )
    monkeypatch.setattr(census, "ask", lambda *a, **k: pytest.fail("a turn was started"))
    out = tmp_path / "out" / "o"
    assert cli.main([str(py_repo), "--out", str(out)]) == 2
    err = capsys.readouterr().err
    assert "cannot be used on this machine" in err and "Traceback" not in err
    assert not out.parent.exists()


def test_a_model_id_shaped_like_a_path_is_refused_before_anything_runs(
    monkeypatch, tmp_path, py_repo, capsys
):
    monkeypatch.setattr(run, "resolve", lambda provider: pytest.fail("the run began"))
    with pytest.raises(SystemExit) as refused:
        cli.main([str(py_repo), "--out", str(tmp_path / "o"), "--model", "../models/m1"])
    assert refused.value.code == 2
    assert "--model" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()


def test_a_directory_that_is_not_a_repository_is_refused(ready, tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert cli.main([str(plain), "--out", str(tmp_path / "o")]) == 2
    assert "not a git repository" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()


def test_output_is_never_written_inside_a_repository_being_reviewed(ready, py_repo, capsys):
    assert cli.main([str(py_repo), "--out", str(py_repo / "o")]) == 2
    assert "inside" in capsys.readouterr().err
    assert not (py_repo / "o").exists()


# --- what a run writes ---------------------------------------------------------------------


FOUR = [
    "codebase_repo_mining.json",
    "codebase_repos.csv",
    "codebase_repos.json",
    "measurement.json",
]


def test_a_run_writes_exactly_the_four_files_in_the_repositorys_own_folder(
    ready, tmp_path, py_repo
):
    out = tmp_path / "o"
    result = _review(py_repo, out)
    assert result["material"]["n_complex_logic"] == 1
    assert _folders(out) == [py_repo.name]
    assert sorted(p.name for p in (out / py_repo.name).iterdir()) == FOUR
    assert sorted(result["written"]) == sorted(out / py_repo.name / name for name in FOUR)


def test_the_zip_holds_each_repository_under_its_folder_name(ready, tmp_path, py_repo):
    out = tmp_path / "o"
    _review(py_repo, out)
    assert sorted(_zip_names(out)) == [f"{py_repo.name}/{name}" for name in FOUR]
    with zipfile.ZipFile(out.parent / run.ZIP_NAME) as archive:
        for name in FOUR:
            packed = archive.read(f"{py_repo.name}/{name}")
            assert packed == (out / py_repo.name / name).read_bytes()


def test_two_repositories_with_one_folder_name_get_a_numbered_second_folder(ready, tmp_path):
    for parent in "abc":
        (tmp_path / parent).mkdir()
    first = make_repo(tmp_path / "a", PY_FILES, name="api")
    second = make_repo(tmp_path / "b", {"a.py": "x = 1\n"}, name="api")
    third = make_repo(tmp_path / "c", {"b.py": "y = 2\n"}, name="api")
    out = tmp_path / "o"
    assert cli.main([str(first), str(second), str(third), "--out", str(out)]) == 0
    assert _folders(out) == ["api", "api-2", "api-3"]
    assert sorted(_zip_names(out)) == sorted(
        f"{folder}/{name}" for folder in ("api", "api-2", "api-3") for name in FOUR
    )


def test_a_numbered_folder_never_lands_on_another_repositorys_own_name(ready, tmp_path):
    # `api`, `api` and `api-2`: numbering the second `api` as `api-2` would put two
    # repositories in one folder, the later review over the earlier one, and the zip would
    # hold the same path twice.
    for parent in "abc":
        (tmp_path / parent).mkdir()
    repos = [
        make_repo(tmp_path / "a", PY_FILES, name="api"),
        make_repo(tmp_path / "b", {"a.py": "x = 1\n"}, name="api"),
        make_repo(tmp_path / "c", {"b.py": "y = 2\n"}, name="api-2"),
    ]
    out = tmp_path / "o"
    assert cli.main([*map(str, repos), "--out", str(out)]) == 0
    folders = ["api", "api-3", "api-2"]
    assert _folders(out) == sorted(folders)
    members = _zip_names(out)
    assert len(members) == len(set(members)) == 3 * len(FOUR)
    assert sorted(members) == sorted(f"{folder}/{name}" for folder in folders for name in FOUR)
    for repo, folder in zip(repos, folders, strict=True):
        written = json.loads((out / folder / "measurement.json").read_text())
        assert written["repo_digest"] == orchestrator.repo_digest(repo)


def test_the_measurement_is_step_ones_with_one_block_added(ready, tmp_path, py_repo):
    out = tmp_path / "o"
    _review(py_repo, out)
    written = json.loads((out / py_repo.name / "measurement.json").read_text())
    row, measurement = orchestrator.measure(py_repo)
    material = written.pop("material")
    for document in (written, measurement):
        document.pop("measured_at")
        document.pop("measurer_version")
    assert written == measurement
    assert material["scored"] is True and material["minable_ideas"]["complex_logic"] == [GOOD]
    # the record id is this tool's own, the one field step 1's row does not have
    stamped = (out / py_repo.name / "codebase_repos.json").read_bytes()
    written_row = json.loads(record.strip(stamped))
    assert set(written_row) == set(row)


def test_every_file_a_review_writes_carries_this_tools_version(ready, tmp_path, py_repo):
    out = tmp_path / "o"
    _review(py_repo, out)
    stamp = f"hazina-review@{run.__version__}"
    for name in ("measurement.json", "codebase_repo_mining.json"):
        written = json.loads((out / py_repo.name / name).read_text())
        assert written["measurer_version"] == stamp, name


def test_nothing_the_model_named_reaches_the_zip(ready, tmp_path, py_repo):
    out = tmp_path / "o"
    _review(py_repo, out)
    with zipfile.ZipFile(out.parent / run.ZIP_NAME) as archive:
        packed = "".join(archive.read(name).decode() for name in archive.namelist())
    assert GOOD in packed and "session" not in packed


def test_a_run_given_no_model_asks_for_the_pinned_one_and_records_it(
    ready, monkeypatch, tmp_path, py_repo
):
    # Left to choose for itself the command may pick a stronger model than the pinned one,
    # and a stronger model finds more.
    seen = {}

    def _ask(provider, model, prompt, repo_dir, read_dirs, timeout):
        seen["model"] = model
        return _turn(_assessment(complex_logic=[GOOD]))

    monkeypatch.setattr(census, "ask", _ask)
    material = _review(py_repo, tmp_path / "o", model=None)["material"]
    assert seen["model"] == registry.DEFAULT_MODELS[PROVIDER]
    assert material["model"] == registry.DEFAULT_MODELS[PROVIDER]


def test_the_requested_model_is_recorded(ready, tmp_path, py_repo):
    assert _review(py_repo, tmp_path / "o", model="m1.5")["material"]["model"] == "m1.5"


# --- the agent reads a copy, and the copy does not outlive the run -------------------------


def test_the_agent_is_pointed_at_a_copy_that_is_gone_afterwards(ready, monkeypatch, tmp_path):
    repo = make_repo(tmp_path, PY_FILES)
    (repo / "scratch-notes.txt").write_text("never committed")
    seen = {}

    def _ask(provider, model, prompt, repo_dir, read_dirs, timeout):
        seen.update(repo=repo_dir, brief=read_dirs[0], timeout=timeout)
        assert (repo_dir / "src" / "demo" / "core.py").is_file()
        assert not (repo_dir / ".git").exists()
        assert not (repo_dir / "scratch-notes.txt").exists()
        assert (read_dirs[0] / brief.OVERVIEW_NAME).is_file()
        return _turn(_assessment())

    monkeypatch.setattr(census, "ask", _ask)
    _review(repo, tmp_path / "o")
    assert seen["repo"] != repo and repo not in seen["repo"].parents
    assert tmp_path not in seen["repo"].parents
    assert not seen["repo"].exists() and not seen["brief"].exists()
    assert 1 <= seen["timeout"] <= 600


def test_the_copy_is_removed_when_the_turn_blows_up(ready, monkeypatch, tmp_path, py_repo):
    seen = {}

    def _ask(provider, model, prompt, repo_dir, read_dirs, timeout):
        seen["repo"] = repo_dir
        raise RuntimeError("the seam broke")

    monkeypatch.setattr(census, "ask", _ask)
    assert "the seam broke" in _review(py_repo, tmp_path / "o")["error"]
    assert not seen["repo"].exists()


# --- a lane that fails is still a run -------------------------------------------------------


def test_a_failed_lane_still_produces_a_complete_run(ready, monkeypatch, tmp_path, py_repo, capsys):
    monkeypatch.setattr(census, "ask", _answers(None, "timeout"))
    out = tmp_path / "o"
    assert cli.main([str(py_repo), "--out", str(out)]) == 1
    material = json.loads((out / py_repo.name / "measurement.json").read_text())["material"]
    assert material["scored"] is False and material["census_failure_kind"] == "timeout"
    assert material["logic_depth"] is None and material["n_minable_ideas"] is None
    assert any(n.endswith("measurement.json") for n in _zip_names(out))
    assert "did not complete" in capsys.readouterr().err


# --- what the operator is told ---------------------------------------------------------------


def test_the_operator_is_told_what_was_written_and_nothing_else(ready, tmp_path, py_repo, capsys):
    out = tmp_path / "o"
    assert cli.main([str(py_repo), "--out", str(out)]) == 0
    said = capsys.readouterr().out
    # two lines however many repositories: where the results are, and the zip to send
    assert f"Results: {out}" in said
    assert f"Zip to send: {out.parent / run.ZIP_NAME}" in said
    assert "wrote " not in said and f"{py_repo.name}: measured" not in said
    assert ".local." not in said and "index" not in said and "repo-" not in said


# --- several repositories ---------------------------------------------------------------------


def test_each_repository_gets_its_own_folder(ready, tmp_path):
    first = make_repo(tmp_path, PY_FILES, name="first")
    second = make_repo(tmp_path, {"a.py": "x = 1\n"}, name="second")
    out = tmp_path / "o"
    assert cli.main([str(first), str(second), "--out", str(out)]) == 0
    assert sorted(p.name for p in (out / "first").iterdir()) == FOUR
    assert sorted(p.name for p in (out / "second").iterdir()) == FOUR
    assert {name.split("/")[0] for name in _zip_names(out)} == {"first", "second"}


def test_one_repository_failing_does_not_sink_the_others(ready, monkeypatch, tmp_path, capsys):
    first = make_repo(tmp_path, PY_FILES, name="first")
    second = make_repo(tmp_path, {"a.py": "x = 1\n"}, name="second")
    turns = []

    def _ask(*args, **kwargs):
        turns.append(1)
        if len(turns) == 1:
            raise RuntimeError("the seam broke")
        return _turn(_assessment(complex_logic=[GOOD]))

    monkeypatch.setattr(census, "ask", _ask)
    out = tmp_path / "o"
    assert cli.main([str(first), str(second), "--out", str(out)]) == 1
    assert "FAILED" in capsys.readouterr().err
    assert {name.split("/")[0] for name in _zip_names(out)} == {"second"}
    assert (out / "second" / "measurement.json").is_file()


# --- a repository with nothing to read is never worth a turn ---------------------------------


def _unread(ready, monkeypatch, repo, out):
    monkeypatch.setattr(census, "ask", lambda *a, **k: pytest.fail("a turn was started"))
    assert cli.main([str(repo), "--out", str(out)]) == 1
    material = json.loads((out / repo.name / "measurement.json").read_text())["material"]
    assert material["scored"] is False and material["census_failure_kind"] == "unknown"
    assert census.NO_TURN["nothing_to_read"][1] in material["error"]
    assert material["census_attempts"] == 0 and material["n_minable_ideas"] is None
    assert any(n.endswith("measurement.json") for n in _zip_names(out))


def test_a_repository_with_no_commit_is_never_sent_to_the_model(ready, monkeypatch, tmp_path):
    repo = make_repo(tmp_path, {"a.py": "x = 1\n"}, commits=[], name="fresh")
    _unread(ready, monkeypatch, repo, tmp_path / "o")


def test_a_copy_that_comes_back_empty_is_never_sent_to_the_model(ready, monkeypatch, tmp_path):
    repo = make_repo(tmp_path, {".env": "KEY=1\n"}, name="onlyenv")
    _unread(ready, monkeypatch, repo, tmp_path / "o")


# --- a stage of our own that fails is never scored, and never paid for ------------------------


def _no_turn(monkeypatch, repo, out, reason, **overrides):
    """Review `repo` with every way of starting a turn rigged to fail the test, and check the
    block that comes back is the unscored one filed under the kind `reason` maps to, saying
    in prose which reason it was."""
    kind, said = census.NO_TURN[reason]
    monkeypatch.setattr(census, "ask", lambda *a, **k: pytest.fail("a turn was started"))
    monkeypatch.setattr(ask_mod, "run", lambda *a, **k: pytest.fail("a child was spawned"))
    result = _review(repo, out, **overrides)
    assert result["error"] is None
    material = json.loads((out / repo.name / "measurement.json").read_text())["material"]
    assert material["scored"] is False and material["ok"] is False
    assert material["census_failure_kind"] == kind and material["census_attempts"] == 0
    assert said in material["error"] and said in material["logic_depth_unavailable_reason"]
    assert material["logic_depth"] is None and material["n_defect_repairs"] is None
    assert material["minable_ideas_total"] is None and material["n_minable_ideas"] is None
    return material


NONE_SUBSTANTIVE = "- commits that look like real multi-file development: 0\n"


def _taken(monkeypatch, repo, out, **overrides):
    """Review `repo` and hand back what each turn was given and the block it came back as."""
    seen = []

    def _ask(provider, model, prompt, repo_dir, read_dirs, timeout):
        seen.append(
            {
                "overview": (read_dirs[0] / brief.OVERVIEW_NAME).read_text(),
                "patches": sorted(p.name for p in (read_dirs[0] / brief.DIFF_SUBDIR).iterdir()),
                "timeout": timeout,
            }
        )
        return _turn(_assessment(complex_logic=[GOOD]))

    monkeypatch.setattr(census, "ask", _ask)
    material = _review(repo, out, **overrides)["material"]
    assert material["scored"] is True and material["census_attempts"] == 1
    return seen[0], material


def test_a_history_git_would_not_give_up_still_gets_its_turn(ready, monkeypatch, tmp_path):
    # One pathspec this git rejects, so every patch comes back empty. The turn is still
    # taken on what is there.
    commits = [{"msg": f"fix {n}", "files": work(n)} for n in range(5)]
    repo = make_repo(tmp_path, {}, commits, name="five")
    rejected = (*brief._EXCLUDE_PATHSPECS, ":(exclude,glob,icase,nonsense)**/x")
    monkeypatch.setattr(brief, "_EXCLUDE_PATHSPECS", rejected)
    seen, _ = _taken(monkeypatch, repo, tmp_path / "o")
    assert seen["patches"] == []


def test_a_history_holding_no_substantive_commit_is_read_and_the_turn_is_taken(
    ready, monkeypatch, tmp_path
):
    # Five one-line fixes clear no bar, so the brief lists nothing and writes no patch.
    commits = [{"msg": f"fix {n}", "files": {"a.py": f"x = {n}\n"}} for n in range(5)]
    repo = make_repo(tmp_path, {}, commits, name="small")
    seen, _ = _taken(monkeypatch, repo, tmp_path / "o")
    assert NONE_SUBSTANTIVE in seen["overview"] and seen["patches"] == []


def test_a_log_walk_that_named_nothing_still_gets_its_turn(ready, monkeypatch, tmp_path, py_repo):
    real = brief.run_git

    def _no_log(repo, *args, **kwargs):
        return "" if args[0] == "log" else real(repo, *args, **kwargs)

    monkeypatch.setattr(brief, "run_git", _no_log)
    seen, _ = _taken(monkeypatch, py_repo, tmp_path / "o")
    assert NONE_SUBSTANTIVE in seen["overview"]


def test_a_history_with_no_patch_in_it_still_gets_its_turn(ready, monkeypatch, tmp_path, py_repo):
    real = brief.run_git

    def _blank_show(repo, *args, **kwargs):
        return "" if args[0] == "show" else real(repo, *args, **kwargs)

    monkeypatch.setattr(brief, "run_git", _blank_show)
    seen, _ = _taken(monkeypatch, py_repo, tmp_path / "o")
    assert seen["patches"] == []


@pytest.mark.parametrize("ceiling", [100, 1])
def test_a_copy_known_to_be_incomplete_is_never_scored(ready, monkeypatch, tmp_path, ceiling):
    # The first file fits under a hundred bytes and the second does not; under one byte
    # neither does. Part of a tree and none of it are the same fact about the copy.
    repo = make_repo(tmp_path, {"a.py": "x = 1\n", "b.py": "y = 2\n" * 1000}, name="big")
    real = snapshot.write_snapshot
    copies = []

    def _small(repo, out, **kwargs):
        copies.append(real(repo, out, max_total_bytes=ceiling))
        return copies[-1]

    monkeypatch.setattr(snapshot, "write_snapshot", _small)
    _no_turn(monkeypatch, repo, tmp_path / "o", "copy_incomplete")
    assert copies[0]["truncated"] is True and copies[0]["files_written"] == (ceiling == 100)


def test_a_budget_with_no_room_left_still_takes_the_turn_with_a_second(
    ready, monkeypatch, tmp_path, py_repo
):
    # No lane is skipped for want of time: it is started with
    # what is left, at least a second, and comes back timed out if that was not enough.
    class _Spent(orchestrator.Deadline):
        def remaining(self):
            return 0.0

    monkeypatch.setattr(orchestrator, "Deadline", _Spent)
    seen, _ = _taken(monkeypatch, py_repo, tmp_path / "o")
    assert seen["timeout"] == 1


def test_the_brief_is_written_without_regard_to_the_budget(ready, monkeypatch, tmp_path, py_repo):
    real = brief.write_brief
    seen = {}

    def _watched(repo, out, **kwargs):
        seen.update(kwargs=kwargs, ceiling=run.env.git_ceiling_seconds())
        return real(repo, out, **kwargs)

    monkeypatch.setattr(brief, "write_brief", _watched)
    _review(py_repo, tmp_path / "o", budget_seconds=600)
    assert seen == {"kwargs": {}, "ceiling": None}


@pytest.mark.parametrize(
    ("options", "census_seconds", "mine_seconds"),
    [
        ({}, 600, 600),
        ({"census_timeout": 30}, 30, 600),
        ({"mine_timeout": 45}, 600, 45),
        ({"census_timeout": 10_000, "mine_timeout": 20}, 600, 20),
    ],
)
def test_each_lane_gets_its_own_ceiling_or_what_the_run_has_left(
    ready, monkeypatch, tmp_path, py_repo, options, census_seconds, mine_seconds
):
    given = {}

    def _watch(lane, answer):
        def _ask(provider, model, prompt, repo_dir, read_dirs, timeout):
            given.setdefault(lane, timeout)
            return _turn(answer)

        return _ask

    class _Fixed(orchestrator.Deadline):
        def remaining(self):
            return 600.5

    monkeypatch.setattr(orchestrator, "Deadline", _Fixed)
    monkeypatch.setattr(census, "ask", _watch("census", _assessment()))
    monkeypatch.setattr(mining, "ask", _watch("mining", []))
    _review(py_repo, tmp_path / "o", **options)
    assert round(given["census"]) == census_seconds and given["mining"] == mine_seconds


def test_the_command_line_hands_each_lane_its_ceiling(monkeypatch, tmp_path, py_repo):
    seen = {}

    def _review_all(repos, out, **options):
        seen.update(options)
        raise run.Refused("stop here")

    monkeypatch.setattr(run, "review_all", _review_all)
    assert cli.main([str(py_repo), "--out", str(tmp_path / "o")]) == 2
    assert seen["census_timeout"] == seen["mine_timeout"] == 14400
    assert seen["budget_seconds"] == 9000
    cli.main([str(py_repo), "--census-timeout", "70", "--mine-timeout", "80"])
    assert (seen["census_timeout"], seen["mine_timeout"]) == (70, 80)


# --- a stop condition met part of the way through ----------------------------------------------


@pytest.mark.parametrize("stop", [registry.ProviderUnavailable, registry.ProviderNotIsolated])
def test_a_stop_on_a_later_repository_keeps_what_was_completed(
    ready, monkeypatch, tmp_path, capsys, stop
):
    repos = [
        make_repo(tmp_path, {"a.py": f"x = {n}\n"}, name=name)
        for n, name in enumerate(("first", "second", "third"))
    ]
    seen = []

    def _ask(provider, model, prompt, repo_dir, read_dirs, timeout):
        seen.append(repo_dir)
        if len(seen) == 2:
            raise stop("the command went away")
        return _turn(_assessment(complex_logic=[GOOD]))

    monkeypatch.setattr(census, "ask", _ask)
    out = tmp_path / "o"
    assert cli.main([*map(str, repos), "--out", str(out)]) == 2
    assert len(seen) == 2 and not any(path.exists() for path in seen)
    assert sorted(_zip_names(out)) == [f"first/{name}" for name in FOUR]
    assert not (out / "second").exists() and not (out / "third").exists()
    captured = capsys.readouterr()
    assert "the command went away" in captured.err and "Traceback" not in captured.err
    assert "1 of 3" in captured.err and "--resume" in captured.err
    assert "nothing was written" not in captured.err.lower()
    assert (out / "first" / "measurement.json").is_file()
    assert f"Results: {out}" in captured.out


def test_a_stop_on_the_first_turn_leaves_no_result_only_what_resuming_needs(
    ready, monkeypatch, tmp_path
):
    def _ask(*args, **kwargs):
        raise registry.ProviderUnavailable("the command went away")

    monkeypatch.setattr(census, "ask", _ask)
    repo = make_repo(tmp_path, {"a.py": "x = 1\n"}, name="first")
    out = tmp_path / "out" / "o"
    assert cli.main([str(repo), "--out", str(out)]) == 2
    assert sorted(p.name for p in out.parent.iterdir()) == ["o"]
    assert sorted(p.name for p in out.iterdir()) == sorted(SUPPORT_FILES)


@pytest.mark.parametrize(
    "failed_lane,failure,reason",
    [
        ("census", "timeout", "lanes_timed_out"),
        ("census", "unparseable", "lanes_unavailable"),
        # only the census running out of time is a timeout
        ("mining", "timeout", "lanes_unavailable"),
        ("mining", "unparseable", "lanes_unavailable"),
    ],
)
def test_partial_status_is_consistent_in_every_output(
    ready, monkeypatch, tmp_path, py_repo, capsys, failed_lane, failure, reason
):
    import csv

    lane = census if failed_lane == "census" else mining
    monkeypatch.setattr(lane, "ask", _answers(None, failure))
    out = tmp_path / "o"
    assert cli.main([str(py_repo), "--out", str(out)]) == 1
    root = out / py_repo.name
    row = json.loads((root / "codebase_repos.json").read_text())
    with (root / "codebase_repos.csv").open() as stream:
        csv_row = next(csv.DictReader(stream))
    assert row["status"] == csv_row["status"] == "partial"
    assert row["skip_reason"] == csv_row["skip_reason"] == reason
    # on screen, the plain-words summary names it; the machine reason stays in the files
    said = capsys.readouterr().err
    assert py_repo.name in said[said.index("Summary") :]
    material = json.loads((root / "measurement.json").read_text())["material"]
    mined = json.loads((root / "codebase_repo_mining.json").read_text())
    if failed_lane == "census":
        assert material["n_complex_logic"] is None and mined["total_candidates"] == 0
    else:
        assert material["n_complex_logic"] == 1 and mined["total_candidates"] is None
    assert material["task_type_counts"]["total_candidates"] == mined["total_candidates"]


def test_lanes_overlap_share_one_snapshot_and_cleanup_after_both(
    ready, monkeypatch, tmp_path, py_repo
):
    import threading

    rendezvous = threading.Barrier(2)
    paths = []
    copies = []
    real = snapshot.write_snapshot

    def copy(repo, out):
        copies.append(out)
        return real(repo, out)

    def ask(answer):
        def turn(provider, model, prompt, repo, dirs, timeout):
            paths.append((repo, dirs[0], timeout))
            rendezvous.wait(timeout=5)
            assert repo.exists() and dirs[0].exists()
            return _turn(answer)

        return turn

    monkeypatch.setattr(snapshot, "write_snapshot", copy)
    monkeypatch.setattr(census, "ask", ask(_assessment()))
    monkeypatch.setattr(mining, "ask", ask([]))
    result = _review(py_repo, tmp_path / "o")
    assert result["status"] == "measured" and result["error"] is None
    assert len(copies) == 1 and len(paths) == 2
    assert paths[0][:2] == paths[1][:2]
    assert all(0 < seconds <= 600 for _, _, seconds in paths)
    assert all(not repo.exists() and not history.exists() for repo, history, _ in paths)


def test_mining_evidence_is_never_written_and_rejected_descriptions_still_count(
    ready, monkeypatch, tmp_path, py_repo
):
    from tests.test_review_mining import task

    raw = [task(), task(title="PrivateTitle", summary=NAMED)]
    monkeypatch.setattr(mining, "ask", _answers(raw))
    out = tmp_path / "o"
    result = _review(py_repo, out)
    assert result["mining"]["total_candidates"] == 2
    assert len(result["mining"]["mined_task_summaries"]) == 1
    with zipfile.ZipFile(out.parent / run.ZIP_NAME) as archive:
        packed = "".join(archive.read(name).decode() for name in archive.namelist())
    assert "PrivateTitle" not in packed and "src/leases.py" not in packed and NAMED not in packed
    assert result["mining"]["mined_task_summaries"][0] in packed


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--mine-n", "0"),
        ("--mine-n", "-2"),
        ("--budget-seconds", "0"),
        ("--census-timeout", "0"),
        ("--mine-timeout", "-1"),
    ],
)
def test_nonpositive_limits_stop_before_provider_probe(monkeypatch, tmp_path, flag, value):
    monkeypatch.setattr(run, "resolve", lambda *a: pytest.fail("provider probed"))
    assert cli.main([str(tmp_path), flag, value]) == 2


def test_any_object_is_scored_and_what_it_says_is_never_written(
    ready, monkeypatch, tmp_path, py_repo
):
    # An object with none of the fields asked for scores as an
    # answer that found nothing, and a lone object from the miner is one task.
    raw = {"unexpected": "PrivateResponse in src/private.py"}
    monkeypatch.setattr(census, "ask", _answers(raw))
    monkeypatch.setattr(mining, "ask", _answers(raw))
    out = tmp_path / "o"
    result = _review(py_repo, out)
    assert result["error"] is None and result["status"] == "measured"
    assert result["material"]["scored"] is True and result["material"]["n_complex_logic"] == 0
    assert result["mining"]["total_candidates"] == result["mining"]["n_agentic"] == 1
    assert result["mining"]["mined_task_summaries"] == []
    with zipfile.ZipFile(out.parent / run.ZIP_NAME) as archive:
        assert all(b"PrivateResponse" not in archive.read(name) for name in archive.namelist())


def test_review_archive_excludes_unrelated_files_in_a_reused_output_folder(
    ready, tmp_path, py_repo
):
    out = tmp_path / "o"
    previous = out / py_repo.name
    previous.mkdir(parents=True)
    (previous / "private-notes.txt").write_text("PrivateOutputNeverShare")
    (previous / "old-result.json").write_text('{"old": "PrivateOutputNeverShare"}')
    _review(py_repo, out)
    with zipfile.ZipFile(out.parent / run.ZIP_NAME) as archive:
        assert len(archive.namelist()) == 4
        assert all(
            b"PrivateOutputNeverShare" not in archive.read(name) for name in archive.namelist()
        )
    assert (previous / "private-notes.txt").read_text() == "PrivateOutputNeverShare"


# --- an earlier run's local files in a reused output folder ---------------------------------


#: The first line of the local index an earlier hazina-review wrote, as the run recognises it.
OLD_INDEX_HEADER = run.OLD_INDEX_FIRST_LINE


def _old_review_folder(folder, detail="PrivateFindingNeverShare"):
    """A folder as an earlier hazina-review left it: step 1's files and the raw findings."""
    folder.mkdir(parents=True, exist_ok=True)
    for name in FOUR:
        (folder / name).write_text("stale")
    (folder / "DETAIL.local.md").write_text(detail)
    return folder


def _old_index(out, *folders):
    """The local index as an earlier hazina-review (or the bundled scanner) wrote it."""
    out.mkdir(parents=True, exist_ok=True)
    rows = [f"{folder.name}\trepo-1\t/home/me/{folder.name}\tmeasured" for folder in folders]
    index = out / "INDEX.local.txt"
    index.write_text("\n".join((OLD_INDEX_HEADER, *scan_cli.INDEX_HEADER[1:], *rows)) + "\n")
    return index


def _case_sensitive(folder) -> bool:
    probe = folder / "CaseProbe"
    probe.write_text("")
    try:
        return not (folder / "caseprobe").exists()
    finally:
        probe.unlink()


def test_an_earlier_runs_local_files_are_removed_and_nothing_else_is_touched(
    ready, tmp_path, py_repo, capsys
):
    out = tmp_path / "o"
    mine = _old_review_folder(out / py_repo.name)
    other = _old_review_folder(out / "other")
    (other / "deep").mkdir()
    index = _old_index(out, mine, other)
    assert index.read_text().splitlines()[0] == OLD_INDEX_HEADER
    old = [index, mine / "DETAIL.local.md", other / "DETAIL.local.md"]
    kept = {
        out / "notes.txt": "keep",
        out / "DETAIL.local.md": "keep",
        mine / "INDEX.local.txt": "keep",
        other / "keep.md": "keep",
        other / "deep" / "DETAIL.local.md": "keep",
    }
    # A name differing only in case is another file only where the file system says so; on
    # macOS's default one it is the old file itself, and is rightly removed with it.
    if _case_sensitive(tmp_path):
        kept[mine / "detail.local.md"] = "keep"
    for path, text in kept.items():
        path.write_text(text)
    capsys.readouterr()
    assert cli.main([str(py_repo), "--out", str(out)]) == 0
    err = capsys.readouterr().err
    assert not any(path.exists() for path in old)
    assert all(path.read_text() == text for path, text in kept.items())
    assert f"removed old INDEX.local.txt from {out} " in err
    assert f"removed old DETAIL.local.md from {mine} (raw findings from an earlier run)" in err
    assert f"removed old DETAIL.local.md from {other} (raw findings from an earlier run)" in err
    assert err.count("removed old") == 3 and "left " not in err
    assert sorted(p.name for p in mine.iterdir()) == sorted(
        [*FOUR, "INDEX.local.txt", *(["detail.local.md"] if _case_sensitive(tmp_path) else [])]
    )


def test_a_rewritten_folder_ends_with_only_the_four_files(ready, tmp_path, py_repo):
    out = tmp_path / "o"
    mine = _old_review_folder(out / py_repo.name)
    _old_index(out, mine)
    _review(py_repo, out)
    assert sorted(p.name for p in mine.iterdir()) == FOUR
    assert sorted(p.name for p in out.iterdir()) == sorted([py_repo.name, *SUPPORT_FILES])
    assert sorted(_zip_names(out)) == [f"{py_repo.name}/{name}" for name in FOUR]


def test_a_detail_file_in_a_folder_this_tool_never_wrote_is_left_and_said(
    ready, tmp_path, py_repo, capsys
):
    # A folder of the operator's own, beside the results: it holds a file by the old name,
    # and nothing else says an earlier run of this tool wrote it.
    out = tmp_path / "o"
    notes = out / "notes"
    notes.mkdir(parents=True)
    (notes / "DETAIL.local.md").write_text("MyOwnNotesNeverTouch")
    (notes / "measurement.json").write_text("{}")  # one of the two is not enough
    capsys.readouterr()
    assert cli.main([str(py_repo), "--out", str(out)]) == 0
    err = capsys.readouterr().err
    assert (notes / "DETAIL.local.md").read_text() == "MyOwnNotesNeverTouch"
    assert "removed old" not in err
    said = [line for line in err.splitlines() if str(notes / "DETAIL.local.md") in line]
    assert len(said) == 1
    assert said[0].startswith("left ") and "does not look like this tool's output" in said[0]


def test_a_detail_file_in_an_earlier_review_folder_is_removed(ready, tmp_path, py_repo, capsys):
    out = tmp_path / "o"
    earlier = _old_review_folder(out / "someone-else")
    capsys.readouterr()
    assert cli.main([str(py_repo), "--out", str(out)]) == 0
    err = capsys.readouterr().err
    assert not (earlier / "DETAIL.local.md").exists()
    assert sorted(p.name for p in earlier.iterdir()) == FOUR
    assert f"removed old DETAIL.local.md from {earlier} " in err


def test_the_scanners_own_index_is_left_and_said(ready, tmp_path, py_repo, capsys):
    # The bundled scanner's command line wrote the same file, with its rows naming its own
    # folders; a review into the same --out has no business removing it.
    out = tmp_path / "o"
    scanned = out / "scanned"
    scanned.mkdir(parents=True)
    for name in scan_cli.OUTPUT_FILES:
        (scanned / name).write_text("the scanner's")
    index = _old_index(out, scanned)
    before = index.read_text()
    capsys.readouterr()
    assert cli.main([str(py_repo), "--out", str(out)]) == 0
    err = capsys.readouterr().err
    assert index.read_text() == before
    assert "removed old" not in err
    said = [line for line in err.splitlines() if str(index) in line]
    assert len(said) == 1
    assert said[0].startswith("left ") and "does not look like this tool's output" in said[0]


@pytest.mark.parametrize(
    "text", ["my own index\n", "", OLD_INDEX_HEADER.split(" --")[0] + "\nx\ty\n"]
)
def test_an_index_with_another_first_line_is_left_and_said(ready, tmp_path, py_repo, capsys, text):
    out = tmp_path / "o"
    mine = _old_review_folder(out / py_repo.name)
    index = out / "INDEX.local.txt"
    index.write_text(text + f"{mine.name}\trepo-1\t/home/me\tmeasured\n")
    capsys.readouterr()
    assert cli.main([str(py_repo), "--out", str(out)]) == 0
    err = capsys.readouterr().err
    assert index.exists() and "does not look like this tool's output" in err


def test_an_earlier_reviews_index_is_removed(ready, tmp_path, py_repo, capsys):
    out = tmp_path / "o"
    index = _old_index(out, _old_review_folder(out / "earlier"))
    capsys.readouterr()
    assert cli.main([str(py_repo), "--out", str(out)]) == 0
    assert not index.exists()
    assert f"removed old INDEX.local.txt from {out} " in capsys.readouterr().err


@pytest.mark.parametrize("where", ["index", "detail", "other detail"])
def test_a_link_at_an_old_local_file_is_refused_and_nothing_is_removed(
    ready, tmp_path, py_repo, where
):
    out = tmp_path / "o"
    (out / py_repo.name).mkdir(parents=True)
    (out / "other").mkdir()
    target = tmp_path / "elsewhere.txt"
    target.write_text("PrivateNeverTouch")
    old = {
        "index": out / "INDEX.local.txt",
        "detail": out / py_repo.name / "DETAIL.local.md",
        "other detail": out / "other" / "DETAIL.local.md",
    }
    link = old.pop(where)
    for regular in old.values():
        regular.write_text("old")
    link.symlink_to(target)
    with pytest.raises(run.Refused, match="symbolic link"):
        _review(py_repo, out)
    assert link.is_symlink() and target.read_text() == "PrivateNeverTouch"
    assert all(regular.read_text() == "old" for regular in old.values())
    assert not (out / py_repo.name / "measurement.json").exists()
    assert not (out.parent / run.ZIP_NAME).exists()


def test_a_linked_folder_in_the_output_directory_is_never_entered(ready, tmp_path, py_repo):
    out = tmp_path / "o"
    out.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "DETAIL.local.md").write_text("PrivateNeverTouch")
    (out / "linked").symlink_to(elsewhere, target_is_directory=True)
    _review(py_repo, out)
    assert (elsewhere / "DETAIL.local.md").read_text() == "PrivateNeverTouch"
    assert (out / "linked").is_symlink()


def test_the_zip_has_its_own_name_and_leaves_the_scanners_zip_alone(ready, tmp_path, py_repo):
    # The two tools often run side by side; neither may overwrite the other's archive.
    out = tmp_path / "o"
    other = tmp_path / scan_cli.ZIP_NAME
    other.write_bytes(b"the scanner's archive")
    _review(py_repo, out)
    assert run.ZIP_NAME == "hazina-review-out.zip" != scan_cli.ZIP_NAME
    assert (out.parent / run.ZIP_NAME).is_file()
    assert other.read_bytes() == b"the scanner's archive"
