"""The checks a run makes before it touches a repository, each with a fake in place of the
provider command: nothing here starts a real one, and nothing here is billed."""

import io
import json
import subprocess
from pathlib import Path

import pytest

from hazina_review import cli, preflight
from hazina_review.lanes import census
from hazina_review.providers import ask as ask_mod
from hazina_review.providers import registry
from tests.conftest import make_repo
from tests.test_review_run import _builds_nothing_unless_asked  # noqa: F401 -- fixture

# The export guard refuses a provider's name in any file the export carries, so the names are
# read from the registry rather than spelled here.
FIRST = registry.PROVIDERS[0]
SECOND = registry.PROVIDERS[1]
EMAIL = "someone.private@example.com"
ORG = "Very Private Org Ltd"


@pytest.fixture(autouse=True)
def _fresh_caches():
    registry.help_of.cache_clear()
    registry.sandbox_reads.cache_clear()
    yield
    registry.help_of.cache_clear()
    registry.sandbox_reads.cache_clear()


def _completed(argv, code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr=stderr)


_REAL_RUN = subprocess.run


def _status_run(monkeypatch, answer):
    """The registry's subprocess seam, answering the sign-in status command with `answer`.
    The seam is the shared module's, so git, which the other checks start, is passed through."""
    calls = []

    def fake_run(argv, **kwargs):
        if Path(argv[0]).name.startswith("git"):
            return _REAL_RUN(argv, **kwargs)
        kwargs["cwd_was_empty"] = not any(Path(kwargs["cwd"]).iterdir())
        calls.append((argv, kwargs))
        if isinstance(answer, BaseException):
            raise answer
        return answer(argv)

    monkeypatch.setattr(registry.subprocess, "run", fake_run)
    return calls


def _clear_keys(monkeypatch):
    for provider in registry.PROVIDERS:
        for name in registry.api_key_names(provider):
            monkeypatch.delenv(name, raising=False)


# --- 1. installed ----------------------------------------------------------------------------


def test_an_installed_command_passes(monkeypatch):
    monkeypatch.setattr(registry.shutil, "which", lambda name: f"/usr/bin/{name}")
    result, executable = preflight.check_installed(FIRST)
    assert result.state == preflight.OK and executable == f"/usr/bin/{FIRST}"


def test_a_missing_command_fails_and_says_to_install_it(monkeypatch):
    monkeypatch.setattr(registry.shutil, "which", lambda name: None)
    result, executable = preflight.check_installed(SECOND)
    assert result.state == preflight.FAILED and executable is None
    assert SECOND in result.says and "install" in result.fix.lower()


# --- 2. flags --------------------------------------------------------------------------------


def test_a_version_offering_every_flag_passes(monkeypatch):
    known = registry._known(FIRST)
    monkeypatch.setattr(registry, "help_of", lambda *a: "\n".join(known.isolation))
    assert preflight.check_flags(FIRST, "/usr/bin/x").state == preflight.OK


def test_a_version_lacking_a_flag_fails_and_names_it(monkeypatch):
    known = registry._known(FIRST)
    monkeypatch.setattr(registry, "help_of", lambda *a: "\n".join(known.isolation[1:]))
    result = preflight.check_flags(FIRST, "/usr/bin/x")
    assert result.state == preflight.FAILED
    assert known.isolation[0] in result.says and "upgrade" in result.fix.lower()


# --- 3. signed in ----------------------------------------------------------------------------


def test_a_signed_in_status_passes_and_nothing_but_the_answer_is_kept(monkeypatch):
    _clear_keys(monkeypatch)
    status = json.dumps({"loggedIn": True, "email": EMAIL, "orgName": ORG, "orgId": "org-1"})
    calls = _status_run(monkeypatch, lambda argv: _completed(argv, 0, status))
    result = preflight.check_signed_in(FIRST, "/usr/bin/x")
    assert result.state == preflight.OK
    assert EMAIL not in repr(result) and ORG not in repr(result)
    argv, kwargs = calls[0]
    assert argv == ["/usr/bin/x", *registry._known(FIRST).status]
    assert kwargs["shell"] is False and kwargs["timeout"] <= 30
    assert "HOME" in kwargs["env"] and kwargs["cwd_was_empty"]


def test_a_signed_out_status_fails_and_says_how_to_sign_in(monkeypatch):
    _clear_keys(monkeypatch)
    status = json.dumps({"loggedIn": False, "email": EMAIL})
    _status_run(monkeypatch, lambda argv: _completed(argv, 1, status))
    result = preflight.check_signed_in(FIRST, "/usr/bin/x")
    assert result.state == preflight.FAILED
    assert registry.login_command(FIRST) in result.fix
    assert EMAIL not in repr(result)


def test_the_second_command_is_signed_in_when_its_status_exits_cleanly(monkeypatch):
    _clear_keys(monkeypatch)
    _status_run(monkeypatch, lambda argv: _completed(argv, 0, f"Logged in as {EMAIL}"))
    result = preflight.check_signed_in(SECOND, "/usr/bin/y")
    assert result.state == preflight.OK and EMAIL not in repr(result)


def test_the_second_command_signed_out_fails(monkeypatch):
    _clear_keys(monkeypatch)
    _status_run(monkeypatch, lambda argv: _completed(argv, 1, "Not logged in"))
    result = preflight.check_signed_in(SECOND, "/usr/bin/y")
    assert result.state == preflight.FAILED and registry.login_command(SECOND) in result.fix


@pytest.mark.parametrize("provider", [FIRST, SECOND])
@pytest.mark.parametrize(
    "answer",
    [
        lambda argv: _completed(argv, 2, "error: unknown command 'status'"),
        subprocess.TimeoutExpired(cmd="x", timeout=20),
        FileNotFoundError("gone"),
    ],
)
def test_a_status_that_cannot_be_asked_is_a_warning_not_a_failure(monkeypatch, provider, answer):
    _clear_keys(monkeypatch)
    _status_run(monkeypatch, answer)
    result = preflight.check_signed_in(provider, "/usr/bin/z")
    assert result.state == preflight.WARNING and "could not verify" in result.says.lower()


@pytest.mark.parametrize("provider", [FIRST, SECOND])
def test_an_api_key_in_the_environment_counts_as_signed_in(monkeypatch, provider):
    _clear_keys(monkeypatch)
    monkeypatch.setenv(registry.api_key_names(provider)[0], "sk-planted-secret")
    _status_run(monkeypatch, lambda argv: pytest.fail("the status command was started"))
    result = preflight.check_signed_in(provider, "/usr/bin/z")
    assert result.state == preflight.OK and "sk-planted-secret" not in repr(result)


def test_the_status_command_is_handed_the_sign_in_variables_and_no_others(monkeypatch):
    _clear_keys(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "planted")
    monkeypatch.setenv(registry.AUTH_VARS[FIRST][-1], "/sign-in-config")
    calls = _status_run(monkeypatch, lambda argv: _completed(argv, 0, '{"loggedIn": true}'))
    preflight.check_signed_in(FIRST, "/usr/bin/x")
    env = calls[0][1]["env"]
    assert "planted" not in env.values() and "GITHUB_TOKEN" not in env
    assert env[registry.AUTH_VARS[FIRST][-1]] == "/sign-in-config" and "HOME" in env


# --- 4. sandbox ------------------------------------------------------------------------------


def test_a_command_with_no_sandbox_of_its_own_is_not_applicable():
    result = preflight.check_sandbox(FIRST, "/usr/bin/x")
    assert result.state == preflight.SKIPPED and "not applicable" in result.says.lower()


def test_a_sandbox_that_reads_passes(monkeypatch):
    monkeypatch.setattr(registry.sys, "platform", "linux")
    monkeypatch.setattr(registry, "sandbox_reads", lambda *a: True)
    assert preflight.check_sandbox(SECOND, "/usr/bin/y").state == preflight.OK


def test_a_sandbox_that_cannot_read_fails_with_the_apparmor_fix(monkeypatch, tmp_path):
    # The AppArmor switch and the bubblewrap on PATH are this test's own, not the machine's.
    _restricted(monkeypatch, tmp_path)
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/bin/bwrap")
    result = preflight.check_sandbox(SECOND, "/usr/bin/y")
    assert result.state == preflight.FAILED
    assert "AppArmor" in result.fix and f"{SECOND} sandbox -- ls" in result.fix


def test_a_sandbox_on_a_platform_it_cannot_be_asked_on_is_skipped(monkeypatch):
    monkeypatch.setattr(registry.sys, "platform", "win32")
    monkeypatch.setattr(registry, "sandbox_reads", lambda *a: pytest.fail("asked"))
    assert preflight.check_sandbox(SECOND, "/usr/bin/y").state == preflight.SKIPPED


# --- 5. git and the repositories ---------------------------------------------------------------


def test_git_installed_passes():
    assert preflight.check_git().state == preflight.OK


def test_git_missing_fails(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)
    result = preflight.check_git()
    assert result.state == preflight.FAILED and "git" in result.fix.lower()


def test_a_repository_with_a_commit_passes(py_repo):
    assert preflight.check_repo(py_repo).state == preflight.OK


def test_a_folder_that_is_not_a_repository_fails(tmp_path):
    result = preflight.check_repo(tmp_path)
    assert result.state == preflight.FAILED and "not a git repository" in result.says


def test_a_path_that_does_not_exist_fails(tmp_path):
    result = preflight.check_repo(tmp_path / "gone")
    assert result.state == preflight.FAILED and "not a folder" in result.says


def test_a_repository_with_no_commit_is_a_warning(tmp_path):
    # Not a stop: a run measures it and writes its review as not completed.
    repo = tmp_path / "empty"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    result = preflight.check_repo(repo)
    assert result.state == preflight.WARNING and "commit" in result.fix.lower()


# --- 6. the output directory -----------------------------------------------------------------


def test_a_new_output_directory_under_a_writable_folder_passes(tmp_path, py_repo):
    result = preflight.check_output(tmp_path / "out" / "o", [py_repo])
    assert result.state == preflight.OK
    assert not (tmp_path / "out").exists()


def test_an_output_directory_inside_a_repository_fails(py_repo):
    result = preflight.check_output(py_repo / "o", [py_repo])
    assert result.state == preflight.FAILED and "inside" in result.says


def test_an_output_directory_that_is_a_link_fails(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    result = preflight.check_output(tmp_path / "link", [])
    assert result.state == preflight.FAILED and "link" in result.says


def test_an_output_path_that_is_a_file_fails(tmp_path):
    (tmp_path / "f").write_text("x")
    result = preflight.check_output(tmp_path / "f", [])
    assert result.state == preflight.FAILED


def test_an_output_directory_that_cannot_be_written_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight.os, "access", lambda *a, **k: False)
    result = preflight.check_output(tmp_path / "o", [])
    assert result.state == preflight.FAILED and "--out" in result.fix


def test_a_link_waiting_at_a_repository_folder_fails(tmp_path, py_repo):
    out = tmp_path / "o"
    out.mkdir()
    (out / py_repo.name).symlink_to(tmp_path)
    result = preflight.check_output(out, [py_repo])
    assert result.state == preflight.FAILED and "link" in result.says


# --- 7. the model ----------------------------------------------------------------------------


def _fake_provider(monkeypatch, reply):
    """The provider behind `ask`, with only the process replaced. `reply(token, argv)` returns
    `(exit status, stdout, stderr)` or raises; the argv and environment are ask's own."""
    seen = []

    def fake_run(argv, *, domain, cwd, env, timeout, input_text=None):
        seen.append({"argv": argv, "cwd": cwd, "env": env, "timeout": timeout})
        token = (Path(cwd) / preflight.PROBE_NAME).read_text(encoding="utf-8").strip()
        code, out, err = reply(token, argv)
        if "--output-last-message" in argv:
            Path(argv[argv.index("--output-last-message") + 1]).write_text(out)
            out = ""
        return _completed(argv, code, out, err)

    monkeypatch.setattr(ask_mod, "resolve", lambda provider: f"/usr/bin/{provider}")
    monkeypatch.setattr(ask_mod, "isolation_flags", lambda provider, exe: ["--isolated"])
    monkeypatch.setattr(ask_mod, "run", fake_run)
    return seen


def _envelope(text, error=False):
    return json.dumps({"result": text, "is_error": error})


def test_a_model_that_reads_the_file_back_passes(monkeypatch):
    seen = _fake_provider(monkeypatch, lambda token, argv: (0, _envelope(token), ""))
    result = preflight.check_model(FIRST, "m-1")
    assert result.state == preflight.OK, result
    call = seen[0]
    assert "--isolated" in call["argv"] and "m-1" in call["argv"]
    assert str(call["cwd"]) in call["argv"]  # its read tools are aimed at the probe folder
    assert call["timeout"] == preflight.MODEL_SECONDS
    assert not Path(call["cwd"]).exists()  # the probe folder is gone afterwards


def test_the_second_provider_reading_the_file_back_passes(monkeypatch):
    seen = _fake_provider(monkeypatch, lambda token, argv: (0, token + "\n", ""))
    assert preflight.check_model(SECOND, "m-2").state == preflight.OK
    assert "m-2" in seen[0]["argv"]


def test_a_model_that_answers_something_else_fails(monkeypatch):
    _fake_provider(monkeypatch, lambda token, argv: (0, _envelope("I cannot open files."), ""))
    result = preflight.check_model(FIRST, "m-1")
    assert result.state == preflight.FAILED and "read" in result.says


@pytest.mark.parametrize(
    ("printed", "kind", "fix"),
    [
        ("Error: 429 rate limit exceeded", "rate_limited", "wait"),
        ("You've hit your usage limit", "rate_limited", "plan"),
        ("Invalid API key. Please run /login", "auth", "LOGIN"),
        ("model not found: m-1", "model_unavailable", "--model"),
        ("socket hang up", "crashed", "network"),
    ],
)
def test_a_refused_session_is_named_and_given_its_fix(monkeypatch, printed, kind, fix):
    _fake_provider(monkeypatch, lambda token, argv: (1, "", printed))
    result = preflight.check_model(FIRST, "m-1")
    assert result.state == preflight.FAILED and kind in result.says
    expected = registry.login_command(FIRST) if fix == "LOGIN" else fix
    assert expected.lower() in result.fix.lower()
    assert printed not in repr(result)  # what the command printed is never repeated


def test_a_refusal_inside_a_clean_exit_is_still_named(monkeypatch):
    _fake_provider(monkeypatch, lambda token, argv: (0, _envelope("usage limit reached", 1), ""))
    assert "rate_limited" in preflight.check_model(FIRST, "m-1").says


def test_a_session_that_runs_out_of_time_fails_as_a_timeout(monkeypatch):
    def reply(token, argv):
        raise subprocess.TimeoutExpired(argv, 1)

    _fake_provider(monkeypatch, reply)
    result = preflight.check_model(FIRST, "m-1")
    assert result.state == preflight.FAILED and "timeout" in result.says


def test_a_provider_with_no_pinned_model_needs_one_named(monkeypatch):
    # Every provider has a pinned model today; the guard stays for one added without it.
    monkeypatch.setitem(registry.DEFAULT_MODELS, SECOND, None)
    _fake_provider(monkeypatch, lambda token, argv: pytest.fail("a session was started"))
    result = preflight.check_model(SECOND, None)
    assert result.state == preflight.FAILED and "--model" in result.fix


# --- the whole list --------------------------------------------------------------------------


@pytest.fixture
def everything_passes(monkeypatch):
    """Every provider-side seam answering as a working, signed-in, isolated command would."""
    _clear_keys(monkeypatch)
    monkeypatch.setattr(registry.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        registry, "help_of", lambda exe, probe=(): "\n".join(registry._known(FIRST).isolation)
    )
    _status_run(monkeypatch, lambda argv: _completed(argv, 0, json.dumps({"loggedIn": True})))
    return _fake_provider(monkeypatch, lambda token, argv: (0, _envelope(token), ""))


def test_every_check_passes_in_order(everything_passes, py_repo, tmp_path):
    results = preflight.run_checks(FIRST, None, [py_repo], tmp_path / "o")
    assert [r.name for r in results] == [
        preflight.INSTALLED,
        preflight.FLAGS,
        preflight.SIGNED_IN,
        preflight.SANDBOX,
        preflight.GIT,
        f"{preflight.REPO} {py_repo.name}",
        preflight.OUTPUT,
        preflight.MODEL,
    ]
    assert preflight.passed(results)
    assert len(everything_passes) == 1


def test_skipping_the_model_check_skips_only_that_check(everything_passes, py_repo, tmp_path):
    results = preflight.run_checks(FIRST, None, [py_repo], tmp_path / "o", model_check=False)
    assert [r.state for r in results[:-1]] == [
        preflight.OK,
        preflight.OK,
        preflight.OK,
        preflight.SKIPPED,
        preflight.OK,
        preflight.OK,
        preflight.OK,
    ]
    assert results[-1].name == preflight.MODEL and results[-1].state == preflight.SKIPPED
    assert "limit" in results[-1].says
    assert not everything_passes and preflight.passed(results)


def test_a_missing_command_skips_what_depends_on_it_and_never_starts_a_session(
    everything_passes, monkeypatch, tmp_path
):
    monkeypatch.setattr(registry.shutil, "which", lambda name: None)
    results = preflight.run_checks(FIRST, None, [], tmp_path / "o")
    states = {r.name: r.state for r in results}
    assert states[preflight.INSTALLED] == preflight.FAILED
    assert states[preflight.FLAGS] == states[preflight.SIGNED_IN] == preflight.SKIPPED
    assert states[preflight.MODEL] == preflight.SKIPPED
    assert not everything_passes and not preflight.passed(results)


def test_any_earlier_failure_saves_the_paid_check(everything_passes, tmp_path):
    results = preflight.run_checks(FIRST, None, [tmp_path], tmp_path / "o")
    assert results[-1].state == preflight.SKIPPED and not everything_passes


def test_a_warning_does_not_fail_the_list(everything_passes, monkeypatch, tmp_path):
    _status_run(monkeypatch, lambda argv: _completed(argv, 2, "unknown command"))
    results = preflight.run_checks(FIRST, None, [], tmp_path / "o")
    assert any(r.state == preflight.WARNING for r in results) and preflight.passed(results)


def test_the_checklist_is_one_line_each_with_its_fix():
    results = [
        preflight.Result("A", preflight.OK, "Fine."),
        preflight.Result("B", preflight.FAILED, "Broken.", "Mend it."),
        preflight.Result("C", preflight.SKIPPED, "Not asked."),
        preflight.Result("D", preflight.WARNING, "Unsure.", "Look."),
    ]
    import io

    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    preflight.say(results, stream)
    lines = stream.buffer.getvalue().decode("utf-8").splitlines()
    assert lines[0].startswith("✓ A") and lines[1].startswith("✗ B")
    assert "Mend it." in lines[2] and lines[3].startswith("– C") and lines[4].startswith("! D")


def test_a_terminal_that_cannot_print_the_marks_gets_plain_ones():
    import io

    stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
    preflight.say([preflight.Result("A", preflight.FAILED, "Broken.", "Mend it.")], stream)
    assert stream.buffer.getvalue().decode("ascii").startswith("x A: Broken.")


# --- the command line ------------------------------------------------------------------------


def test_check_exits_zero_when_everything_passes(everything_passes, py_repo, tmp_path, capsys):
    assert cli.main(["--check", str(py_repo), "--out", str(tmp_path / "o")]) == 0
    said = capsys.readouterr().out
    assert "✓" in said and preflight.MODEL in said
    assert not (tmp_path / "o").exists()


def test_check_needs_no_repository(everything_passes, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--check"]) == 0


def test_check_exits_two_when_a_check_fails(everything_passes, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(registry.shutil, "which", lambda name: None)
    assert cli.main(["--check"]) == 2
    assert "✗" in capsys.readouterr().out


def test_check_with_skip_model_check_starts_no_session(everything_passes, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--check", "--skip-model-check"]) == 0
    assert not everything_passes


def test_a_run_without_a_repository_is_still_a_usage_error():
    with pytest.raises(SystemExit) as stopped:
        cli.main([])
    assert stopped.value.code == 2


def test_no_email_org_or_key_ever_reaches_the_output(everything_passes, monkeypatch, capsys):
    status = json.dumps({"loggedIn": True, "email": EMAIL, "orgName": ORG, "orgId": "org-xyz"})
    _status_run(monkeypatch, lambda argv: _completed(argv, 0, status))
    cli.main(["--check", "--skip-model-check"])
    _status_run(monkeypatch, lambda argv: _completed(argv, 1, status.replace("true", "false")))
    cli.main(["--check", "--skip-model-check"])
    monkeypatch.setenv(registry.api_key_names(FIRST)[0], "sk-ant-planted-value")
    cli.main(["--check", "--skip-model-check"])
    said = capsys.readouterr()
    for secret in (EMAIL, ORG, "org-xyz", "sk-ant-planted-value"):
        assert secret not in said.out and secret not in said.err


def test_a_failed_check_stops_a_run_before_anything_is_written(
    everything_passes, monkeypatch, tmp_path, capsys
):
    repo = make_repo(tmp_path, {"a.py": "x = 1\n"}, name="r")
    monkeypatch.setattr(registry, "help_of", lambda *a: "--help")
    monkeypatch.setattr(census, "ask", lambda *a, **k: pytest.fail("a turn was started"))
    from hazina_review import run

    monkeypatch.setattr(run, "review_all", lambda *a, **k: pytest.fail("the run began"))
    out = tmp_path / "out" / "o"
    assert cli.main([str(repo), "--out", str(out)]) == 2
    err = capsys.readouterr().err
    assert "✗" in err and "nothing was written" in err.lower()
    assert not (tmp_path / "out").exists()


def test_a_run_prints_the_checklist_and_then_runs(everything_passes, monkeypatch, tmp_path, capsys):
    repo = make_repo(tmp_path, {"a.py": "x = 1\n"}, name="r")
    from hazina_review import run

    began = []

    def _review_all(repos, out_dir, **options):
        began.append(options)
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

    monkeypatch.setattr(run, "review_all", _review_all)
    assert cli.main([str(repo), "--out", str(tmp_path / "o"), "--skip-model-check"]) == 0
    assert began and "✓" in capsys.readouterr().err
    assert not everything_passes


def _restricted(monkeypatch, tmp_path, value="1"):
    switch = tmp_path / "apparmor_restrict_unprivileged_userns"
    switch.write_text(value + "\n")
    monkeypatch.setattr(preflight, "APPARMOR_SWITCH", switch)
    monkeypatch.setattr(registry.sys, "platform", "linux")
    monkeypatch.setattr(registry, "sandbox_reads", lambda *a: False)


def _installed_with_its_own_bwrap(tmp_path):
    package = tmp_path / "lib" / "node_modules" / "@openai" / "pkg"
    bwrap = package / "node_modules" / "@openai" / "pkg-linux-x64" / "vendor" / "arch"
    bwrap = bwrap / f"{SECOND}-resources" / "bwrap"
    bwrap.parent.mkdir(parents=True)
    bwrap.write_text("")
    exe = package / "bin" / "entry.js"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    return exe, bwrap


def test_a_blocked_sandbox_names_the_systems_bubblewrap_when_there_is_one(monkeypatch, tmp_path):
    # Traced on Ubuntu 24.04: the command runs the system's bwrap even though it ships its
    # own, so that is the program the profile has to name.
    _restricted(monkeypatch, tmp_path)
    exe, bundled = _installed_with_its_own_bwrap(tmp_path)
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/bin/bwrap")
    result = preflight.check_sandbox(SECOND, str(exe))
    assert result.state == preflight.FAILED
    assert f"profile {SECOND}-bwrap /usr/bin/bwrap flags=(unconfined)" in result.fix
    assert str(bundled) not in result.fix


def test_a_blocked_sandbox_gets_one_command_to_paste_and_how_to_undo_it(monkeypatch, tmp_path):
    _restricted(monkeypatch, tmp_path)
    exe, bundled = _installed_with_its_own_bwrap(tmp_path)
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)
    result = preflight.check_sandbox(SECOND, str(exe))
    # with no system bubblewrap, the command's own copy is the one that runs
    assert f"profile {SECOND}-bwrap {bundled} flags=(unconfined)" in result.fix
    fix_line = next(line for line in result.fix.splitlines() if "apparmor_parser -r" in line)
    # one command, one password prompt: pasting two lines let the second be read as the
    # password for the first
    assert fix_line.strip().startswith("sudo sh -c ") and fix_line.count("sudo") == 1
    assert "userns," in fix_line and "<<" not in result.fix
    assert "apparmor_parser -R" in result.fix  # how to undo it
    assert f"{SECOND} sandbox -- ls" in result.fix
    assert f"--provider {FIRST}" in result.fix


def test_a_blocked_sandbox_without_the_apparmor_limit_gets_the_general_advice(
    monkeypatch, tmp_path
):
    _restricted(monkeypatch, tmp_path, value="0")
    result = preflight.check_sandbox(SECOND, "/usr/bin/y")
    assert result.state == preflight.FAILED
    assert "apparmor_parser" not in result.fix
    assert f"--provider {FIRST}" in result.fix


def test_a_fix_over_several_lines_is_indented_line_by_line():
    stream = io.StringIO()
    preflight.say(
        [preflight.Result("Sandbox", preflight.FAILED, "It failed.", "first line\nsecond line")],
        stream,
    )
    lines = stream.getvalue().splitlines()
    assert "    first line" in lines and "    second line" in lines
