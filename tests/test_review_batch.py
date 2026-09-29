"""A batch of repositories as the person running it sees it: what it says while it works, when
it stops, how it carries on, and the support file it leaves behind.

Nothing here starts a provider command. Both lanes' turns are replaced at their seams, and
each fake answers according to which repository it was handed, read from a marker file every
test repository carries.
"""

import io
import json
import os
import shlex
import shutil
import signal
import threading
import zipfile

import pytest

from hazina_review import cli, preflight, run, support
from hazina_review.lanes import census, mining
from hazina_review.providers import registry
from hazina_review.providers.ask import Turn
from tests.conftest import make_repo
from tests.test_review_run import (  # noqa: F401 -- fixtures
    FOUR,
    GOOD,
    _assessment,
    _builds_nothing_unless_asked,
    _provider_checks_pass,
    _turn,
    ready,
)

PROVIDER = registry.PROVIDERS[0]
NAMES = ("first", "second", "third", "fourth")
SUPPORT = "partners@hazinalabs.com"

#: Past the checks and the stop conditions before a run, as in the run tests.
pytestmark = pytest.mark.usefixtures("ready")

RATE_LIMITED = Turn("exited", 1, "", "API Error: 429 usage limit reached for this account")
SIGNED_OUT = Turn("exited", 1, "", "Invalid API key. Please run /login")
NO_MODEL = Turn("exited", 1, "", "model not found: the requested model does not exist")


def _marker(repo_dir) -> str:
    return (repo_dir / "marker.py").read_text().split("'")[1]


def _repos(tmp_path, names=NAMES):
    return [make_repo(tmp_path, {"marker.py": f"name = '{name}'\n"}, name=name) for name in names]


class _Fake:
    """Both lanes, answering by repository. `failing` maps a repository's name to what its
    turns come back as; every other repository gets a usable answer. `asked` records each
    turn's repository and lane."""

    def __init__(self, monkeypatch, failing=None, *, delay=0.0):
        self.failing = dict(failing or {})
        self.asked = []
        self.delay = delay
        self._lock = threading.Lock()
        monkeypatch.setattr(census, "ask", self._lane("census", _assessment(complex_logic=[GOOD])))
        monkeypatch.setattr(mining, "ask", self._lane("mining", [{"summary": GOOD}] * 3))
        # A throttled census is tried again after a short wait; the wait is not the test.
        monkeypatch.setattr(census.time, "sleep", lambda seconds: None)

    def _lane(self, lane, answer):
        def ask(provider, model, prompt, repo_dir, read_dirs, timeout):
            name = _marker(repo_dir)
            with self._lock:
                self.asked.append((name, lane))
            if self.delay:
                # not `time.sleep`, which is stood in for below
                threading.Event().wait(self.delay)
            failure = self.failing.get(name)
            if failure is KeyboardInterrupt:
                raise KeyboardInterrupt
            if failure is not None:
                return failure
            return _turn(answer)

        return ask

    def repos_asked(self):
        return list(dict.fromkeys(name for name, _ in self.asked))


def _zip_folders(out):
    with zipfile.ZipFile(out.parent / run.ZIP_NAME) as archive:
        return sorted({name.split("/")[0] for name in archive.namelist()})


def _progress(out):
    return json.loads((out / support.PROGRESS_NAME).read_text())


def _states(out):
    return {repo["folder"]: repo["state"] for repo in _progress(out)["repos"]}


def _resume(out):
    return f"hazina-review --resume {shlex.quote(str(out))}"


def _support_line(out):
    return f"If you need help, email {out / support.LOG_NAME} to {SUPPORT}."


# --- a problem that would hit every repository stops the batch ----------------------------


def test_a_usage_limit_on_the_second_of_four_stops_the_batch(monkeypatch, tmp_path, capsys):
    fake = _Fake(monkeypatch, {"second": RATE_LIMITED})
    repos = _repos(tmp_path)
    out = tmp_path / "out dir"
    assert cli.main([*map(str, repos), "--out", str(out)]) == 2
    assert fake.repos_asked() == ["first", "second"]
    # Both of the second repository's lanes ran to the end; nothing was started after them.
    assert {lane for name, lane in fake.asked if name == "second"} == {"census", "mining"}
    # Today's rule for the zip, with one addition: the four files of every repository whose
    # files were written, except one stopped by a problem that hits every repository, which
    # is redone on resume.
    assert _zip_folders(out) == ["first"]
    assert _states(out) == {
        "first": "done",
        "second": "incomplete",
        "third": "not started",
        "fourth": "not started",
    }
    err = capsys.readouterr().err
    assert "usage limit" in err and "rate_limited" not in err
    assert "the zip holds the 1 finished repository" in err
    assert _resume(out) in err and "'" in _resume(out)  # quoted: the path has a space
    assert err.rstrip().endswith(_support_line(out))
    assert "Traceback" not in err
    summary = err[err.index("Summary") :]
    assert "Summary: 1 of 4 repositories done, 1 incomplete, 2 not started." in summary
    # only what needs attention is named; the rest is a count
    second = next(line for line in summary.splitlines() if "second" in line)
    assert "usage limit" in second
    assert not any(name in summary for name in ("first", "third", "fourth"))


def test_a_resumed_run_finishes_only_what_was_left(monkeypatch, tmp_path, capsys):
    fake = _Fake(monkeypatch, {"second": RATE_LIMITED})
    repos = _repos(tmp_path)
    out = tmp_path / "o"
    assert cli.main([*map(str, repos), "--out", str(out)]) == 2
    first = (out / "first" / "measurement.json").read_bytes()
    fake.failing.clear()
    fake.asked.clear()
    capsys.readouterr()
    assert cli.main(["--resume", str(out)]) == 0
    assert fake.repos_asked() == ["second", "third", "fourth"]
    assert _zip_folders(out) == sorted(NAMES)
    assert set(_states(out).values()) == {"done"}
    assert (out / "first" / "measurement.json").read_bytes() == first
    err = capsys.readouterr().err
    assert "[2/4] second: starting" in err and "[1/4]" not in err
    log = (out / support.LOG_NAME).read_text()
    assert log.count("hazina-review run started") == 2 and "resumed" in log


def test_a_resumed_run_uses_the_recorded_options(monkeypatch, tmp_path):
    fake = _Fake(monkeypatch, {"second": RATE_LIMITED})
    seen = []
    original = run.review_all

    def _watch(repos, out_dir, **options):
        seen.append(options)
        return original(repos, out_dir, **options)

    monkeypatch.setattr(run, "review_all", _watch)
    out = tmp_path / "o"
    args = [*map(str, _repos(tmp_path, NAMES[:2])), "--out", str(out)]
    assert cli.main([*args, "--model", "m1.5", "--mine-n", "7", "--census-timeout", "70"]) == 2
    fake.failing.clear()
    assert cli.main(["--resume", str(out)]) == 0
    for key in ("provider", "model", "mine_n", "census_timeout", "mine_timeout", "budget_seconds"):
        assert seen[1][key] == seen[0][key]
    assert seen[1]["model"] == "m1.5" and seen[1]["mine_n"] == 7


@pytest.mark.parametrize(
    "extra",
    [["--model", "m2"], ["--provider", registry.PROVIDERS[0]], ["--mine-n", "5"], ["repo"]],
)
def test_resume_refuses_options_that_would_mix_one_batch(tmp_path, capsys, extra):
    out = tmp_path / "o"
    assert cli.main(["--resume", str(out), *extra]) == 2
    err = capsys.readouterr().err
    assert "--resume" in err and "Traceback" not in err
    assert not out.exists()


def test_resume_says_plainly_when_there_is_nothing_to_resume(tmp_path, capsys):
    assert cli.main(["--resume", str(tmp_path / "nowhere")]) == 2
    err = capsys.readouterr().err
    assert "nothing to resume" in err.lower() and "Traceback" not in err


def test_resume_runs_the_checks_again(monkeypatch, tmp_path, capsys):
    fake = _Fake(monkeypatch, {"second": RATE_LIMITED})
    out = tmp_path / "o"
    assert cli.main([*map(str, _repos(tmp_path, NAMES[:2])), "--out", str(out)]) == 2
    fake.failing.clear()
    asked = []

    def _checks(provider, model, repos, where, **kwargs):
        asked.append((provider, model, sorted(p.name for p in repos), where))
        return [preflight.Result("git", preflight.FAILED, "Not today.", "Fix it.")]

    monkeypatch.setattr(preflight, "run_checks", _checks)
    assert cli.main(["--resume", str(out)]) == 2
    assert asked == [(PROVIDER, registry.DEFAULT_MODELS[PROVIDER], ["second"], out.resolve())]
    assert _states(out)["second"] == "incomplete"


def test_a_repository_that_is_gone_on_resume_is_skipped_and_said(monkeypatch, tmp_path, capsys):
    fake = _Fake(monkeypatch, {"second": RATE_LIMITED})
    repos = _repos(tmp_path, NAMES[:3])
    out = tmp_path / "o"
    assert cli.main([*map(str, repos), "--out", str(out)]) == 2
    shutil.rmtree(repos[2])
    fake.failing.clear()
    capsys.readouterr()
    assert cli.main(["--resume", str(out)]) == 1
    err = capsys.readouterr().err
    assert "third" in err and "no longer there" in err
    assert _states(out) == {"first": "done", "second": "done", "third": "not started"}
    assert _zip_folders(out) == ["first", "second"]


@pytest.mark.parametrize(
    "turn,said",
    [
        (SIGNED_OUT, lambda: registry.login_command(PROVIDER)),
        (NO_MODEL, lambda: "--model"),
    ],
)
def test_a_sign_in_or_model_problem_stops_the_batch_with_its_own_next_step(
    monkeypatch, tmp_path, capsys, turn, said
):
    fake = _Fake(monkeypatch, {"first": turn})
    out = tmp_path / "o"
    assert cli.main([*map(str, _repos(tmp_path, NAMES[:3])), "--out", str(out)]) == 2
    assert fake.repos_asked() == ["first"]
    err = capsys.readouterr().err
    assert said() in err and _resume(out) in err
    assert err.rstrip().endswith(_support_line(out))
    assert "0 of 3" in err
    assert not (out.parent / run.ZIP_NAME).exists()


@pytest.mark.parametrize("stop", [registry.ProviderUnavailable, registry.ProviderNotIsolated])
def test_a_provider_command_that_goes_away_stops_the_batch_and_can_be_resumed(
    monkeypatch, tmp_path, capsys, stop
):
    fake = _Fake(monkeypatch)
    real = census.ask

    def _ask(provider, model, prompt, repo_dir, read_dirs, timeout):
        if _marker(repo_dir) == "second":
            raise stop("the command cannot be used on this machine")
        return real(provider, model, prompt, repo_dir, read_dirs, timeout)

    monkeypatch.setattr(census, "ask", _ask)
    out = tmp_path / "o"
    assert cli.main([*map(str, _repos(tmp_path, NAMES[:3])), "--out", str(out)]) == 2
    assert _states(out) == {"first": "done", "second": "incomplete", "third": "not started"}
    err = capsys.readouterr().err
    assert _resume(out) in err and err.rstrip().endswith(_support_line(out))
    assert "Upgrade" in err or "Install" in err
    monkeypatch.setattr(census, "ask", real)
    assert cli.main(["--resume", str(out)]) == 0
    assert _zip_folders(out) == sorted(NAMES[:3])
    assert fake.repos_asked().count("first") == 1


# --- a problem with one repository does not stop the batch --------------------------------


def test_a_timeout_on_one_repository_does_not_stop_the_batch(monkeypatch, tmp_path, capsys):
    fake = _Fake(monkeypatch, {"second": Turn("timeout")})
    out = tmp_path / "o"
    assert cli.main([*map(str, _repos(tmp_path, NAMES[:3])), "--out", str(out)]) == 1
    assert fake.repos_asked() == ["first", "second", "third"]
    # Today's rule: a repository whose files were written is packed, complete or not.
    assert _zip_folders(out) == sorted(NAMES[:3])
    assert _states(out) == {"first": "done", "second": "incomplete", "third": "done"}
    err = capsys.readouterr().err
    summary = err[err.index("Summary") :]
    assert "Summary: 2 of 3 repositories done, 1 incomplete." in summary
    assert any("second" in line and "ran out of time" in line for line in summary.splitlines())
    assert _resume(out) in summary and err.rstrip().endswith(_support_line(out))
    line = next(line for line in summary.splitlines() if "second" in line)
    assert "timeout" not in line


def test_every_repository_done_exits_zero_and_says_so(monkeypatch, tmp_path, capsys):
    _Fake(monkeypatch)
    out = tmp_path / "o"
    assert cli.main([*map(str, _repos(tmp_path, NAMES[:2])), "--out", str(out)]) == 0
    err = capsys.readouterr().err
    assert "Summary: all 2 repositories done." in err
    assert SUPPORT not in err and "--resume" not in err


def test_resuming_a_finished_run_says_so_and_checks_nothing(monkeypatch, tmp_path, capsys):
    fake = _Fake(monkeypatch)
    out = tmp_path / "o"
    assert cli.main([*map(str, _repos(tmp_path, NAMES[:2])), "--out", str(out)]) == 0
    fake.asked.clear()
    capsys.readouterr()

    def _must_not_run(*args, **kwargs):
        raise AssertionError("a check ran for a run with nothing left to do")

    monkeypatch.setattr(preflight, "run_checks", _must_not_run)
    monkeypatch.setattr(preflight, "check_model", _must_not_run)
    monkeypatch.setattr(run, "review_all", _must_not_run)
    assert cli.main(["--resume", str(out)]) == 0
    said = capsys.readouterr()
    assert "Every repository in this run is already done." in said.out
    assert f"Results: {out.resolve()}" in said.out
    assert f"Zip to send: {out.resolve().parent / run.ZIP_NAME}" in said.out
    assert "Traceback" not in said.err and fake.asked == []


# --- Ctrl-C ------------------------------------------------------------------------------------


def test_ctrl_c_mid_batch_saves_what_is_done(monkeypatch, tmp_path, capsys):
    fake = _Fake(monkeypatch, {"second": KeyboardInterrupt})
    out = tmp_path / "o"
    assert cli.main([*map(str, _repos(tmp_path, NAMES[:3])), "--out", str(out)]) == 2
    assert "third" not in fake.repos_asked()
    assert _zip_folders(out) == ["first"]
    assert _states(out) == {"first": "done", "second": "incomplete", "third": "not started"}
    err = capsys.readouterr().err
    assert "Ctrl-C" in err and _resume(out) in err and "Traceback" not in err
    fake.failing.clear()
    assert cli.main(["--resume", str(out)]) == 0
    assert _zip_folders(out) == sorted(NAMES[:3])


# --- what it says while it works -------------------------------------------------------------


def test_each_repository_and_lane_is_announced(monkeypatch, tmp_path, capsys):
    _Fake(monkeypatch)
    out = tmp_path / "o"
    assert cli.main([*map(str, _repos(tmp_path, NAMES[:2])), "--out", str(out)]) == 0
    err = capsys.readouterr().err
    assert "[1/2] first: starting" in err and "[2/2] second: starting" in err
    assert "[1/2] first: census done in " in err
    assert "[2/2] second: task mining done in " in err and "(3 tasks)" in err
    # A run shorter than the interval never says it is still working.
    assert "still working" not in err


def test_a_heartbeat_names_the_lanes_still_running(monkeypatch, tmp_path, capsys):
    _Fake(monkeypatch, delay=0.4)
    out = tmp_path / "o"
    run.review_all(
        _repos(tmp_path, NAMES[:1]),
        out,
        provider=PROVIDER,
        model=None,
        budget_seconds=600,
        heartbeat_seconds=0.1,
    )
    err = capsys.readouterr().err
    assert "[1/1] first: still working (census " in err and "task mining " in err


def test_a_failed_lane_is_one_plain_line(monkeypatch, tmp_path, capsys):
    _Fake(monkeypatch, {"first": Turn("timeout")})
    out = tmp_path / "o"
    assert cli.main([*map(str, _repos(tmp_path, NAMES[:1])), "--out", str(out)]) == 1
    err = capsys.readouterr().err
    assert "[1/1] first: census did not complete" in err and "ran out of time" in err
    assert "[1/1] first: task mining did not complete" in err


def test_the_duration_is_said_the_way_people_say_it():
    assert support.duration(5) == "5s"
    assert support.duration(312) == "5m 12s"
    assert support.duration(423) == "7m 03s"
    assert support.duration(3723) == "1h 02m 03s"
    assert support.short(45) == "45s" and support.short(250) == "4m"


# --- the progress file -------------------------------------------------------------------------


def test_the_progress_file_is_replaced_whole_after_every_repository(monkeypatch, tmp_path):
    _Fake(monkeypatch, {"second": Turn("timeout")})
    replaced = []
    real = os.replace

    def _replace(source, target):
        if str(target).endswith(support.PROGRESS_NAME):
            assert os.path.dirname(source) == os.path.dirname(target)
            replaced.append(json.loads(open(source).read()))
        return real(source, target)

    monkeypatch.setattr(support.os, "replace", _replace)
    out = tmp_path / "o"
    repos = _repos(tmp_path, NAMES[:2])
    assert cli.main([*map(str, repos), "--out", str(out), "--model", "m1.5"]) == 1
    states = [[repo["state"] for repo in saved["repos"]] for saved in replaced]
    assert ["done", "not started"] in states and ["done", "incomplete"] in states
    final = _progress(out)
    assert final["options"]["model"] == "m1.5" and final["options"]["provider"] == PROVIDER
    assert final["tool_version"] == run.__version__
    assert [repo["folder"] for repo in final["repos"]] == ["first", "second"]
    assert [repo["path"] for repo in final["repos"]] == [str(r.resolve()) for r in repos]
    assert final["repos"][1]["kind"] == "timeout"
    assert not list(out.glob("*.tmp*"))


def test_the_progress_file_and_log_stay_out_of_the_zip_and_survive_cleanup(monkeypatch, tmp_path):
    _Fake(monkeypatch)
    out = tmp_path / "o"
    repo = _repos(tmp_path, NAMES[:1])
    assert cli.main([*map(str, repo), "--out", str(out)]) == 0
    assert cli.main([*map(str, repo), "--out", str(out)]) == 0
    assert (out / support.PROGRESS_NAME).is_file() and (out / support.LOG_NAME).is_file()
    with zipfile.ZipFile(out.parent / run.ZIP_NAME) as archive:
        assert sorted(archive.namelist()) == [f"first/{name}" for name in FOUR]
    assert (out / support.LOG_NAME).read_text().count("hazina-review run started") == 2


# --- the support log ---------------------------------------------------------------------------

EMAIL = "someone.private@example.com"
# Built here rather than written out, so they read as the shapes they are and not as literals.
TOKEN = "tok_" + "9f8e7d6c5b4a" * 3
KEY = "-".join(("sk", "live", "AbCdEfGhIjKlMnOp" * 2))


def test_the_log_says_what_happened_and_never_holds_a_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(registry.api_key_names(PROVIDER)[0], KEY)
    monkeypatch.setenv("SOME_SERVICE_TOKEN", TOKEN)
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", EMAIL)
    monkeypatch.setattr(support, "provider_version", lambda executable: f"9.9.9 {EMAIL} {TOKEN}")
    checked = [
        preflight.Result(preflight.SIGNED_IN, preflight.OK, f"Signed in as {EMAIL} key {KEY}."),
        preflight.Result(preflight.GIT, preflight.OK, "git version 2.43.0."),
    ]
    monkeypatch.setattr(preflight, "run_checks", lambda *a, **k: list(checked))
    _Fake(monkeypatch, {"second": Turn("exited", 1, "", f"429 usage limit for {EMAIL} {KEY}")})
    out = tmp_path / "o"
    assert cli.main([*map(str, _repos(tmp_path, NAMES[:3])), "--out", str(out)]) == 2
    log = (out / support.LOG_NAME).read_text()
    for secret in (EMAIL, TOKEN, KEY, str(tmp_path), GOOD):
        assert secret not in log
    assert run.__version__ in log and "9.9.9" in log and "python" in log.lower()
    assert "git version" in log and "Signed in" in log
    assert "first" in log and "second" in log and "rate_limited" in log
    assert "exit code 1" in log and "stopped" in log
    assert "~/o" in log
    for line in log.splitlines():
        assert not line or line.startswith(("20", "====")), line


def test_the_log_names_the_provider_environment_and_never_a_value(monkeypatch, tmp_path):
    # Values nothing else in a run would print, so any of them in the log came from the
    # environment. None is shaped like a secret, so the scrubber would not catch them.
    seeded = {
        "HOME": str(tmp_path),
        "USER": "seeded-user-value",
        "LOGNAME": "seeded-logname-value",
        "XDG_CONFIG_HOME": "/seeded/xdg/value",
        registry.AUTH_VARS[PROVIDER][-1]: "/seeded/config/value",
        registry.api_key_names(PROVIDER)[0]: "seeded-key-value",
    }
    for name, value in seeded.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    for name in ("USERPROFILE", "CODEX_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(preflight, "run_checks", lambda *a, **k: [])
    _Fake(monkeypatch)
    out = tmp_path / "o"
    assert cli.main([str(_repos(tmp_path, NAMES[:1])[0]), "--out", str(out)]) == 0
    log = (out / support.LOG_NAME).read_text()
    for value in (*seeded.values(), "seeded"):
        assert value not in log
    said = next(line for line in log.splitlines() if "provider environment" in line)
    for name in ("HOME", "USER", "LOGNAME", "TMPDIR"):
        assert f"{name}: set, passed" in said
    assert "XDG_CONFIG_HOME: set, not passed" in said
    assert "USERPROFILE: not set" in said and "CODEX_HOME: not set" in said
    signs = next(line for line in log.splitlines() if "provider sign-in variables" in line)
    for name in registry.AUTH_VARS[PROVIDER]:
        assert name in signs
    assert f"{registry.api_key_names(PROVIDER)[0]}: set" in signs
    assert f"{registry.AUTH_VARS[PROVIDER][-1]}: set" in signs


def test_lines_said_from_both_lanes_at_once_never_run_together(monkeypatch):
    class _Slow:
        """A stream that lets another thread in during every write, as a busy one would."""

        def __init__(self):
            self.written = []

        def write(self, text):
            threading.Event().wait(0.001)
            self.written.append(text)

        def flush(self):
            pass

    stream = _Slow()
    monkeypatch.setattr(run.sys, "stderr", stream)
    said = [f"[1/1] repo: line {n}" for n in range(40)]
    threads = [threading.Thread(target=run._tell, args=("", line)) for line in said]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted("".join(stream.written).splitlines()) == sorted(said)


def test_a_long_batch_names_at_most_ten_incomplete_repositories(tmp_path):
    repos = [
        support.Repo(path=f"/r/{n}", folder=f"r{n:02d}", state=support.DONE) for n in range(30)
    ]
    for repo in repos[:15]:
        repo.state, repo.kind = support.INCOMPLETE, "timeout"
    lines, code = support.report(
        {
            "repos": repos,
            "out_dir": tmp_path,
            "provider": registry.PROVIDERS[0],
            "model": "m",
            "stop_kind": None,
        },
        io.StringIO(),
    )
    said = "\n".join(lines)
    assert code == 1
    assert "Summary: 15 of 30 repositories done, 15 incomplete." in said
    assert sum("ran out of time" in line for line in lines) == 10
    assert f"and 5 more, listed in {tmp_path / support.LOG_NAME}" in said


def test_a_stop_before_anything_finished_says_so_plainly(tmp_path):
    repos = [support.Repo(path="/r/a", folder="a", state=support.INCOMPLETE, kind="rate_limited")]
    repos.append(support.Repo(path="/r/b", folder="b"))
    lines, code = support.report(
        {
            "repos": repos,
            "out_dir": tmp_path,
            "provider": registry.PROVIDERS[0],
            "model": "m",
            "stop_kind": "rate_limited",
        },
        io.StringIO(),
    )
    said = "\n".join(lines)
    assert code == 2 and "Nothing had finished yet." in said and " are done" not in said


# --- a file that is not this run's own at the log's or the progress file's name ----------------

posix_only = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs and links")


@pytest.fixture
def no_hang():
    """A test that would block on a pipe fails after ten seconds instead of hanging the suite."""

    def _hung(*_):
        raise TimeoutError("blocked: something opened a pipe and waited on it")

    before = signal.signal(signal.SIGALRM, _hung)
    signal.alarm(10)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, before)


def _plant(kind, path, outside):
    """Put `kind` at `path`: a named pipe, a second name for `outside`, or a link to it."""
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "hard link":
        os.link(outside, path)
    else:
        path.symlink_to(outside)


def _checks_must_not_run(monkeypatch):
    def _checks(*args, **kwargs):
        raise AssertionError("the checks ran, and the last of them is billed")

    monkeypatch.setattr(preflight, "run_checks", _checks)


@posix_only
@pytest.mark.parametrize("name", [support.LOG_NAME, support.PROGRESS_NAME])
@pytest.mark.parametrize("kind", ["fifo", "hard link", "symlink"])
def test_an_unsafe_file_at_the_log_or_progress_name_stops_before_the_checks(
    monkeypatch, tmp_path, capsys, no_hang, name, kind
):
    _Fake(monkeypatch)
    _checks_must_not_run(monkeypatch)
    out = tmp_path / "o"
    out.mkdir()
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("PrivateNeverTouch")
    _plant(kind, out / name, outside)
    assert cli.main([*map(str, _repos(tmp_path, NAMES[:1])), "--out", str(out)]) == 2
    err = capsys.readouterr().err
    assert str(out / name) in err and "--out" in err and "Traceback" not in err
    assert outside.read_text() == "PrivateNeverTouch"
    assert not (out / "first").exists() and not (out.parent / run.ZIP_NAME).exists()
    assert os.path.lexists(out / name)


@posix_only
@pytest.mark.parametrize("kind", ["fifo", "hard link", "symlink"])
def test_the_log_refuses_an_unsafe_file_that_appears_after_it_was_checked(tmp_path, no_hang, kind):
    out = tmp_path / "o"
    out.mkdir()
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("PrivateNeverTouch")
    log = support.Log(out)
    _plant(kind, out / support.LOG_NAME, outside)
    with pytest.raises(ValueError, match="--out"):
        log.write("a line")
    assert outside.read_text() == "PrivateNeverTouch"


@posix_only
@pytest.mark.parametrize("kind", ["fifo", "hard link", "symlink"])
def test_the_progress_file_refuses_an_unsafe_file_at_its_name(tmp_path, no_hang, kind):
    out = tmp_path / "o"
    out.mkdir()
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("PrivateNeverTouch")
    progress = support.Progress(out, {}, [support.Repo("/nowhere", "first")])
    _plant(kind, out / support.PROGRESS_NAME, outside)
    with pytest.raises(ValueError, match="--out"):
        progress.save()
    assert outside.read_text() == "PrivateNeverTouch"
    assert os.path.lexists(out / support.PROGRESS_NAME)


@posix_only
@pytest.mark.parametrize("kind", ["fifo", "hard link", "symlink"])
def test_the_progress_files_temporary_name_is_never_written_through(tmp_path, no_hang, kind):
    out = tmp_path / "o"
    out.mkdir()
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("PrivateNeverTouch")
    progress = support.Progress(out, {}, [support.Repo("/nowhere", "first")])
    _plant(kind, out / f".{support.PROGRESS_NAME}.tmp-{os.getpid()}", outside)
    progress.save()
    assert outside.read_text() == "PrivateNeverTouch"
    assert json.loads((out / support.PROGRESS_NAME).read_text())["repos"][0]["folder"] == "first"
    assert not list(out.glob("*.tmp*"))


def test_one_repository_done_says_just_done(tmp_path):
    repos = [support.Repo(path="/r/a", folder="a", state=support.DONE)]
    lines, code = support.report(
        {
            "repos": repos,
            "out_dir": tmp_path,
            "provider": registry.PROVIDERS[0],
            "model": "m",
            "stop_kind": None,
        },
        io.StringIO(),
    )
    assert code == 0 and "Summary: done." in lines and "all 1" not in "\n".join(lines)
