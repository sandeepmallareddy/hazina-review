"""The build check inside a review: the same flags as the bundled scanner, the same check, run
after both model lanes on the checkout, with its own share of the run's time.

The build itself is replaced at `orchestrator.build_lane` throughout. A real build, compared
with the scanner's own, is in `tests/parity/test_review_build.py`.
"""

import json
import threading

import pytest

from hazina_review import cli, preflight, run, support
from hazina_review.lanes import census, mining
from hazina_review.providers import registry
from hazina_scan import cli as scan_cli
from hazina_scan import orchestrator
from hazina_scan.build import probe
from tests.conftest import make_repo
from tests.test_review_batch import RATE_LIMITED, _Fake, _repos
from tests.test_review_run import (  # noqa: F401 -- fixtures
    GOOD,
    _assessment,
    _builds_nothing_unless_asked,
    _provider_checks_pass,
    _turn,
    ready,
)

PROVIDER = registry.PROVIDERS[0]

#: Past the checks and the stop conditions before a run, as in the run tests.
pytestmark = pytest.mark.usefixtures("ready")

#: What the build check returns for a project that installed, built and ran its tests.
BUILT = {
    "probe": "build",
    "ok": True,
    "build_attempted": True,
    "install_ok": True,
    "build_ok": True,
    "tests_discovered": True,
    "build_and_tests_ran": True,
    "timed_out": False,
}


class _Builds:
    """`orchestrator.build_lane`, answering `block` and recording each call and when it came
    relative to the model lanes' turns."""

    def __init__(self, monkeypatch, block=None, *, wait=0.0):
        self.block, self.wait = dict(BUILT if block is None else block), wait
        self.calls, self.events = [], []
        monkeypatch.setattr(orchestrator, "build_lane", self._lane)
        for lane, module, answer in (
            ("census", census, _assessment(complex_logic=[GOOD])),
            ("mining", mining, []),
        ):
            monkeypatch.setattr(module, "ask", self._ask(lane, answer))

    def _ask(self, lane, answer):
        def ask(provider, model, prompt, repo_dir, read_dirs, timeout):
            self.events.append(lane)
            return _turn(answer)

        return ask

    def _lane(
        self,
        repo,
        level,
        probe_budget,
        timeout_build,
        max_projects,
        full_attempt,
        deadline,
        say=None,
    ):
        self.events.append("build")
        self.calls.append(
            {
                "repo": repo,
                "level": level,
                "probe_budget": probe_budget,
                "timeout_build": timeout_build,
                "max_projects": max_projects,
                "full_attempt": full_attempt,
            }
        )
        if self.wait:
            threading.Event().wait(self.wait)
        return {**self.block, "build_level": level, "build_level_requested": level}


class _Fixed(orchestrator.Deadline):
    """A deadline on which no time passes, so ceilings can be compared exactly."""

    def remaining(self):
        return float(self.total)


# --- the flags ---------------------------------------------------------------------------------


#: Each build option's name in this tool, and its destination in the scanner's parser.
SCAN_DEST = {
    "build_budget_seconds": "build_budget",
    "full_attempt_seconds": "full_attempt_seconds",
    "timeout_build": "timeout_build",
    "max_build_projects": "max_build_projects",
}


def test_the_build_flags_have_hazina_scans_defaults():
    scan_args = scan_cli.build_parser().parse_args(["somewhere"])
    assert cli.DEFAULT_BUILD_LEVEL == scan_args.build == "full"
    for name, dest in SCAN_DEST.items():
        assert cli.DEFAULTS[name] == getattr(scan_args, dest), name
    review_parser = cli.build_parser()
    scan_parser = scan_cli.build_parser()
    choices = {
        parser.prog: next(a.choices for a in parser._actions if "--build" in a.option_strings)
        for parser in (review_parser, scan_parser)
    }
    assert tuple(choices[review_parser.prog]) == tuple(choices[scan_parser.prog])


def _handed(monkeypatch, tmp_path, repo, *extra, default=None):
    """What the command line hands the checks and the run."""
    if default is not None:
        monkeypatch.setitem(cli.DEFAULTS, "build_level", default)
    seen = {}
    real = preflight.run_checks

    def _checks(*args, **kwargs):
        seen["checks"] = kwargs
        return real(*args, **kwargs)

    def _review_all(repos, out, **options):
        seen.update(options)
        raise run.Refused("stop here")

    monkeypatch.setattr(preflight, "run_checks", _checks)
    monkeypatch.setattr(run, "review_all", _review_all)
    assert cli.main([str(repo), "--out", str(tmp_path / "o"), *extra]) == 2
    return seen


def test_a_run_builds_at_full_unless_told_otherwise(monkeypatch, tmp_path, py_repo):
    seen = _handed(monkeypatch, tmp_path, py_repo, default=cli.DEFAULT_BUILD_LEVEL)
    assert seen["build_level"] == "full" and seen["checks"]["build_level"] == "full"
    assert seen["build_budget_seconds"] == orchestrator.DEFAULT_BUILD_BUDGET_SECONDS
    assert seen["full_attempt_seconds"] == orchestrator.DEFAULT_FULL_ATTEMPT_SECONDS
    assert seen["timeout_build"] == orchestrator.DEFAULT_TIMEOUT_BUILD
    assert seen["max_build_projects"] == orchestrator.DEFAULT_MAX_BUILD_PROJECTS


@pytest.mark.parametrize(
    ("extra", "level"),
    [
        (["--build", "discover"], "discover"),
        (["--build", "none"], "none"),
        (["--no-build"], "none"),
        (["--build", "full", "--no-build"], "none"),
    ],
)
def test_the_build_level_is_the_one_asked_for(monkeypatch, tmp_path, py_repo, extra, level):
    seen = _handed(monkeypatch, tmp_path, py_repo, *extra, default=cli.DEFAULT_BUILD_LEVEL)
    assert seen["build_level"] == level and seen["checks"]["build_level"] == level


def test_the_build_sizes_are_handed_to_the_run(monkeypatch, tmp_path, py_repo):
    seen = _handed(
        monkeypatch,
        tmp_path,
        py_repo,
        *("--build-budget-seconds", "600", "--full-attempt-seconds", "300"),
        *("--timeout-build", "120", "--max-build-projects", "2"),
    )
    assert (
        seen["build_budget_seconds"],
        seen["full_attempt_seconds"],
        seen["timeout_build"],
        seen["max_build_projects"],
    ) == (600, 300, 120, 2)


@pytest.mark.parametrize(
    "flag",
    ["--build-budget-seconds", "--full-attempt-seconds", "--timeout-build", "--max-build-projects"],
)
@pytest.mark.parametrize("value", ["0", "-5"])
def test_a_build_size_that_cannot_bound_a_build_is_refused_before_any_check(
    monkeypatch, tmp_path, py_repo, capsys, flag, value
):
    monkeypatch.setattr(preflight, "run_checks", lambda *a, **k: pytest.fail("a check ran"))
    monkeypatch.setattr(run, "review_all", lambda *a, **k: pytest.fail("the run began"))
    assert cli.main([str(py_repo), "--out", str(tmp_path / "o"), flag, value]) == 2
    err = capsys.readouterr().err
    assert "positive" in err and "Traceback" not in err
    assert not (tmp_path / "o").exists()


def test_the_run_refuses_an_unknown_build_level_itself(tmp_path, py_repo):
    with pytest.raises(run.Refused):
        run.review(
            py_repo,
            tmp_path / "o",
            provider=PROVIDER,
            model=None,
            budget_seconds=60,
            build_level="everything",
        )


# --- the check inside a run -------------------------------------------------------------------


def test_the_build_check_fills_the_build_block_and_the_rows_build_columns(
    monkeypatch, tmp_path, py_repo
):
    builds = _Builds(monkeypatch)
    out = tmp_path / "o"
    result = run.review(
        py_repo, out, provider=PROVIDER, model=None, budget_seconds=600, build_level="full"
    )
    assert result["error"] is None and result["status"] == "measured"
    measurement = json.loads((out / "repo" / "measurement.json").read_text())
    block = measurement["ext_signals"]["build"]
    assert block["build_ok"] is True and block["build_level"] == "full"
    for name in ("codebase_repos.json",):
        row = json.loads((out / "repo" / name).read_text())
        row = row[0] if isinstance(row, list) else row
        assert row["build_ok"] is True and row["testable_at_head"] is True
    assert builds.calls[0]["repo"] == py_repo.resolve()


def test_the_build_check_runs_after_both_model_lanes_and_alone(monkeypatch, tmp_path, py_repo):
    builds = _Builds(monkeypatch)
    spans = {}
    real = run._QuietClock

    class _Keeping(real):
        def lane(self, name):
            spans[name] = self
            return super().lane(name)

    monkeypatch.setattr(run, "_QuietClock", _Keeping)
    run.review(
        py_repo,
        tmp_path / "o",
        provider=PROVIDER,
        model=None,
        budget_seconds=600,
        build_level="discover",
    )
    assert builds.events[-1] == "build" and sorted(builds.events[:-1]) == ["census", "mining"]
    clock = spans["build"]
    assert clock.overlaps("build") == []
    census_end = clock.spans()["census"][1]
    assert clock.spans()["build"][0] >= census_end


def test_no_build_asked_for_runs_none(monkeypatch, tmp_path, py_repo):
    builds = _Builds(monkeypatch)
    out = tmp_path / "o"
    run.review(py_repo, out, provider=PROVIDER, model=None, budget_seconds=600, build_level="none")
    assert builds.calls == []
    measurement = json.loads((out / "repo" / "measurement.json").read_text())
    assert measurement["ext_signals"]["build"] is None


@pytest.mark.parametrize(
    ("budget", "level", "options", "census_seconds", "mine_seconds", "probe_budget"),
    [
        (9000, "full", {}, 7200, 7200, 1800),
        (9000, "none", {}, 9000, 9000, None),
        (1000, "discover", {}, 500, 500, 1000),
        (3000, "full", {"build_budget_seconds": 600, "mine_timeout": 100}, 2400, 100, 600),
        (600, "full", {"census_timeout": 30}, 30, 300, 600),
    ],
)
def test_the_build_check_keeps_its_share_of_the_run(
    monkeypatch,
    tmp_path,
    py_repo,
    budget,
    level,
    options,
    census_seconds,
    mine_seconds,
    probe_budget,
):
    builds = _Builds(monkeypatch)
    given = {}

    def _watch(lane, answer):
        def _ask(provider, model, prompt, repo_dir, read_dirs, timeout):
            given[lane] = timeout
            return _turn(answer)

        return _ask

    monkeypatch.setattr(orchestrator, "Deadline", _Fixed)
    monkeypatch.setattr(census, "ask", _watch("census", _assessment()))
    monkeypatch.setattr(mining, "ask", _watch("mining", []))
    run.review(
        py_repo,
        tmp_path / "o",
        provider=PROVIDER,
        model=None,
        budget_seconds=budget,
        build_level=level,
        **options,
    )
    assert (round(given["census"]), given["mining"]) == (census_seconds, mine_seconds)
    assert run.lane_ceilings(
        _Fixed(budget),
        build_level=level,
        build_budget=options.get("build_budget_seconds", orchestrator.DEFAULT_BUILD_BUDGET_SECONDS),
        census_timeout=options.get("census_timeout", run.DEFAULT_LANE_TIMEOUT),
        mine_timeout=options.get("mine_timeout", run.DEFAULT_LANE_TIMEOUT),
    ) == (census_seconds, mine_seconds)
    assert (builds.calls[0]["probe_budget"] if builds.calls else None) == probe_budget


# --- what the person running it reads ----------------------------------------------------------


@pytest.mark.parametrize(
    ("block", "said"),
    [
        (BUILT, "build check done in 0s (built, tests ran)"),
        ({**BUILT, "build_and_tests_ran": None}, "build check done in 0s (built, tests listed)"),
        (
            {**BUILT, "build_and_tests_ran": False, "tests_discovered": False},
            "build check done in 0s (built, no tests ran)",
        ),
        (
            {**BUILT, "build_ok": False, "build_and_tests_ran": False},
            "build check done in 0s: the repository did not build",
        ),
        (
            {**BUILT, "build_ok": None, "build_and_tests_ran": None, "timed_out": True},
            "build check did not complete after 0s: it ran out of time",
        ),
        (
            {**BUILT, "build_ok": None, "build_and_tests_ran": None},
            "build check did not complete after 0s: this machine could not build it",
        ),
    ],
)
def test_a_line_says_how_the_build_check_ended(monkeypatch, tmp_path, py_repo, capsys, block, said):
    _Builds(monkeypatch, block)
    out = tmp_path / "o"
    assert cli.main([str(py_repo), "--out", str(out), "--build", "full"]) in (0, 1)
    err = capsys.readouterr().err
    assert f"[1/1] repo: {said}" in err
    assert "[build] ran at level" not in err and "[budget]" not in err
    log = (out / support.LOG_NAME).read_text()
    assert "build check: full" in log and "repo: build check ended after 0s: " in log


def test_a_build_the_run_had_no_time_for_says_so(monkeypatch, tmp_path, py_repo, capsys):
    _Builds(monkeypatch)
    out = tmp_path / "o"
    # Less than the shortest command the check would start, so it never does.
    budget = str(probe.MIN_PHASE_SECONDS - 5)
    assert (
        cli.main([str(py_repo), "--out", str(out), "--budget-seconds", budget, "--build", "full"])
        == 1
    )
    err = capsys.readouterr().err
    assert "[1/1] repo: build check did not start: the run's time was used up" in err
    progress = json.loads((out / support.PROGRESS_NAME).read_text())
    assert progress["repos"][0]["state"] == support.INCOMPLETE
    assert progress["repos"][0]["kind"] == "build_timed_out"
    assert "the build check ran out of time" in err


def test_the_heartbeat_names_the_build_check_while_it_runs(monkeypatch, tmp_path, py_repo, capsys):
    _Builds(monkeypatch, wait=0.5)
    run.review_all(
        [py_repo],
        tmp_path / "o",
        provider=PROVIDER,
        model=None,
        budget_seconds=600,
        build_level="full",
        heartbeat_seconds=0.1,
    )
    err = capsys.readouterr().err
    assert "still working (build check" in err


def test_the_log_keeps_the_build_level_and_each_outcome_and_no_command_output(
    monkeypatch, tmp_path, py_repo
):
    _Builds(monkeypatch, {**BUILT, "note": "pip said something private"})
    out = tmp_path / "o"
    assert cli.main([str(py_repo), "--out", str(out), "--build", "discover"]) == 0
    log = (out / support.LOG_NAME).read_text()
    assert "build check: discover" in log
    assert "repo: build check ended after " in log and ": built" in log
    assert "private" not in log


# --- carrying on ------------------------------------------------------------------------------


def test_a_resumed_run_keeps_its_build_level(monkeypatch, tmp_path):
    fake = _Fake(monkeypatch, {"second": RATE_LIMITED})
    builds = _Builds.__new__(_Builds)
    builds.block, builds.wait, builds.calls, builds.events = dict(BUILT), 0.0, [], []
    monkeypatch.setattr(orchestrator, "build_lane", builds._lane)
    out = tmp_path / "o"
    repos = [str(r) for r in _repos(tmp_path, ("first", "second"))]
    args = [*repos, "--out", str(out), "--build", "discover", "--timeout-build", "77"]
    assert cli.main(args) == 2
    recorded = json.loads((out / support.PROGRESS_NAME).read_text())["options"]
    assert recorded["build_level"] == "discover" and recorded["timeout_build"] == 77
    fake.failing.clear()
    builds.calls.clear()
    assert cli.main(["--resume", str(out)]) == 0
    assert [c["level"] for c in builds.calls] == ["discover"]
    assert builds.calls[0]["timeout_build"] == 77


@pytest.mark.parametrize(
    "extra", [["--build", "full"], ["--no-build"], ["--build-budget-seconds", "60"]]
)
def test_resume_refuses_a_different_build(tmp_path, capsys, extra):
    out = tmp_path / "o"
    assert cli.main(["--resume", str(out), *extra]) == 2
    err = capsys.readouterr().err
    assert "--resume" in err and "Traceback" not in err
    assert not out.exists()


def test_an_older_progress_file_resumes_without_a_build(tmp_path):
    out = tmp_path / "o"
    out.mkdir()
    progress = support.Progress(out, {name: None for name in support.RECORDED}, [])
    options = {
        "provider": PROVIDER,
        "model": "m",
        "budget_seconds": 60,
        "mine_n": 1,
        "census_timeout": 1,
        "mine_timeout": 1,
    }
    document = {
        "format": support.PROGRESS_FORMAT,
        "tool_version": progress.tool_version,
        "options": options,
        "stopped": None,
        "repos": [{"path": str(tmp_path), "folder": "x", "state": "incomplete"}],
    }
    (out / support.PROGRESS_NAME).write_text(json.dumps(document))
    loaded = support.Progress.load(out)
    assert loaded.options["build_level"] == "none"


# --- the checklist before a run ---------------------------------------------------------------


def test_the_checklist_warns_that_the_build_changes_the_checkout(tmp_path, py_repo):
    results = preflight.run_checks(
        PROVIDER, None, [py_repo], tmp_path / "o", model_check=False, build_level="full"
    )
    warned = [r for r in results if r.name == preflight.BUILD]
    assert [r.state for r in warned] == [preflight.WARNING]
    assert warned[0].says == (
        "The build check runs each repository's own install, build and test commands and "
        "changes the checkout. Run it on a throwaway copy."
    )
    assert preflight.passed(results)
    quiet = preflight.run_checks(
        PROVIDER, None, [py_repo], tmp_path / "o", model_check=False, build_level="none"
    )
    assert not [r for r in quiet if r.name == preflight.BUILD]


def test_the_checklist_warns_of_uncommitted_changes_only_when_building(tmp_path):
    clean = make_repo(tmp_path, {"a.py": "a = 1\n"}, name="clean")
    dirty = make_repo(tmp_path, {"b.py": "b = 1\n"}, name="dirty")
    (dirty / "b.py").write_text("b = 2\n")
    results = preflight.run_checks(
        PROVIDER, None, [clean, dirty], tmp_path / "o", model_check=False, build_level="discover"
    )
    warned = [r for r in results if "uncommitted" in r.says]
    assert len(warned) == 1 and warned[0].state == preflight.WARNING
    assert "dirty" in warned[0].name and "may overwrite" in warned[0].says
    assert preflight.passed(results)
    none = preflight.run_checks(
        PROVIDER, None, [dirty], tmp_path / "o", model_check=False, build_level="none"
    )
    assert not [r for r in none if "uncommitted" in r.says]
