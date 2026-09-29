import json
import subprocess
from pathlib import Path

import pytest

from hazina_review.providers import ask as ask_mod
from hazina_review.providers import registry
from hazina_scan import env as env_mod

# The export guard refuses a provider's name in any file the scan export carries, and that
# includes this one, so the name is read from the registry rather than spelled here.
FIRST = registry.PROVIDERS[0]
SECOND = registry.PROVIDERS[1]


@pytest.fixture(autouse=True)
def _fresh_help_cache():
    # Process-wide state in the registry; see the same fixture in test_review_providers.
    registry.help_of.cache_clear()
    yield
    registry.help_of.cache_clear()


def _done(stdout: str, code: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr=stderr)


def _wire(monkeypatch, result):
    monkeypatch.setattr(ask_mod, "resolve", lambda p: f"/usr/bin/{p}")
    monkeypatch.setattr(ask_mod, "isolation_flags", lambda p, e: ["--safe-mode"])
    if isinstance(result, Exception):

        def _raise(*a, **k):
            raise result

        monkeypatch.setattr(ask_mod, "run", _raise)
    else:
        monkeypatch.setattr(ask_mod, "run", lambda *a, **k: result)


def _capture(monkeypatch, result):
    """Wire `ask` up as `_wire` does, and keep what it hands to `run`."""
    _wire(monkeypatch, result)
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen.update(kwargs)
        return result

    monkeypatch.setattr(ask_mod, "run", fake_run)
    return seen


def test_a_finished_turn_keeps_its_exit_and_both_streams(monkeypatch, tmp_path):
    _wire(monkeypatch, _done("printed", code=3, stderr="complained"))
    turn = ask_mod.ask(FIRST, None, "p", tmp_path, [], timeout=5)
    assert turn == ask_mod.Turn("exited", 3, "printed", "complained")


def test_a_timeout_keeps_what_was_printed_before_it(monkeypatch, tmp_path):
    _wire(
        monkeypatch,
        subprocess.TimeoutExpired(cmd=FIRST, timeout=5, output=b"partial", stderr="waiting"),
    )
    turn = ask_mod.ask(FIRST, None, "p", tmp_path, [], timeout=5)
    assert turn == ask_mod.Turn("timeout", None, "partial", "waiting")


def test_no_shell_tool_is_ever_granted():
    argv = ask_mod.build_argv(f"/usr/bin/{FIRST}", None, "p", [Path("/r")], [])
    granted = argv[argv.index("--allowedTools") + 1]
    assert granted == "Read Grep Glob"
    assert "bash" not in " ".join(argv).lower()


# --- the two failures that stop the run ---------------------------------------------------


@pytest.mark.parametrize("stop", [registry.ProviderUnavailable, registry.ProviderNotIsolated])
@pytest.mark.parametrize("provider", registry.PROVIDERS)
def test_a_command_that_is_missing_or_cannot_be_isolated_stops_the_run(
    monkeypatch, tmp_path, provider, stop
):
    started = []
    _wire(monkeypatch, _done("{}"))
    monkeypatch.setattr(ask_mod, "run", lambda *a, **k: started.append(a))

    def _stop(*a):
        raise stop("no")

    target = "resolve" if stop is registry.ProviderUnavailable else "isolation_flags"
    monkeypatch.setattr(ask_mod, target, _stop)
    with pytest.raises(stop):
        ask_mod.ask(provider, None, "p", tmp_path, [], timeout=5)
    assert started == []


def test_an_old_command_stops_the_run_through_the_real_registry(monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(registry.shutil, "which", lambda p: f"/usr/bin/{p}")
    monkeypatch.setattr(registry, "_help_text", lambda *a: "--version --help")
    monkeypatch.setattr(ask_mod, "run", lambda *a, **k: started.append(a))
    with pytest.raises(registry.ProviderNotIsolated):
        ask_mod.ask(FIRST, None, "p", tmp_path, [], timeout=5)
    assert started == []


# --- every other failure is a note --------------------------------------------------------


@pytest.mark.parametrize(
    ("failure", "ended"),
    [
        (FileNotFoundError("gone"), "missing"),
        (PermissionError("no"), "unstartable"),
        (OSError(7, "Argument list too long"), "unstartable"),
        (ValueError("embedded null byte"), "unstartable"),
    ],
)
def test_a_command_that_will_not_start_is_recorded(monkeypatch, tmp_path, failure, ended):
    _wire(monkeypatch, failure)
    turn = ask_mod.ask(FIRST, None, "p", tmp_path, [], timeout=5)
    assert turn.ended == ended and turn.error is failure and turn.returncode is None


def test_a_command_killed_by_a_signal_is_recorded(monkeypatch, tmp_path):
    _wire(monkeypatch, _done("", code=-9))
    assert ask_mod.ask(FIRST, None, "p", tmp_path, [], timeout=5).returncode == -9


# --- the census reply, read as the platform reads it ---------------------------------------


def _envelope(result):
    return json.dumps({"type": "result", "result": result})


@pytest.mark.parametrize(
    ("stdout", "answer"),
    [
        (_envelope('{"self_contained": 3}'), {"self_contained": 3}),
        (_envelope({"self_contained": 3}), {"self_contained": 3}),
        (_envelope('```json\n{"a": 1}\n```'), {"a": 1}),
        (_envelope('here it is:\n```json\n{"a": 1}\n```\nthat is all'), {"a": 1}),
        (_envelope('```\n{"a": 1}\n```'), {"a": 1}),
        # a fence that does not decode is passed over for the widest span
        (_envelope('```json\n{"a": }\n``` then {"b": 2}'), None),
        (_envelope('```json\n{"a": }\n``` then {"b": 2'), None),
        (_envelope('```json\n{"a": }\n```'), None),
        (_envelope('prose {"a": {"b": 1}} more prose'), {"a": {"b": 1}}),
        (_envelope('[{"a": 1}]'), {"a": 1}),
        (_envelope("[1, 2]"), None),
        (_envelope("no answer"), None),
        (_envelope("{broken"), None),
        # no envelope at all: the whole of what was printed is searched
        ('noise {"a": 1} noise', {"a": 1}),
        ('{"a": 1}', {"a": 1}),
        # an envelope with no string or object answer is itself searched, and is an object
        (json.dumps({"result": 5}), {"result": 5}),
        (json.dumps({"is_error": True, "result": "limit {reached}"}), None),
        ("", None),
        ("[" * 100_000, None),
        (_envelope("[" * 100_000), None),
    ],
)
def test_the_census_reply_is_read_as_expected(stdout, answer):
    assert ask_mod.census_answer(stdout, FIRST) == answer


def test_a_file_answer_is_searched_as_it_stands():
    assert ask_mod.census_answer('{"result": "x"}', SECOND) == {"result": "x"}
    assert ask_mod.census_answer('said ```json\n{"a": 1}\n```', SECOND) == {"a": 1}


# --- the task list, read as the platform reads it ------------------------------------------


@pytest.mark.parametrize(
    ("stdout", "answer"),
    [
        (_envelope("[]"), []),
        (_envelope('[{"summary": "x"}]'), [{"summary": "x"}]),
        (_envelope("[1, 2]"), [1, 2]),
        (_envelope('```json\n[{"a": 1}]\n```'), [{"a": 1}]),
        (_envelope('list: [{"a": 1}, {"b": 2}] done'), [{"a": 1}, {"b": 2}]),
        (_envelope('one: {"a": 1}'), {"a": 1}),
        (_envelope('"just a string"'), "just a string"),
        (_envelope("42"), 42),
        (_envelope("no answer"), None),
        (_envelope('[{"title": "a"}, broken]'), {"title": "a"}),
        # an answer that is not a string: the envelope itself is what is read
        (_envelope({"a": 1}), {"type": "result", "result": {"a": 1}}),
        (_envelope(["x"]), {"type": "result", "result": ["x"]}),
        ('[{"a": 1}]', [{"a": 1}]),
        ("", None),
        ("[" * 100_000, None),
    ],
)
def test_the_task_list_is_read_as_expected(stdout, answer):
    assert ask_mod.mining_answer(stdout, FIRST) == answer


# --- what the child is given --------------------------------------------------------------

_AMBIENT_SECRETS = ("AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "DATABASE_URL", "SSH_AUTH_SOCK")


@pytest.mark.parametrize("provider", registry.PROVIDERS)
def test_the_child_gets_its_own_credential_and_nobody_elses(monkeypatch, tmp_path, provider):
    for name in _AMBIENT_SECRETS:
        monkeypatch.setenv(name, "ambient-secret")
    for names in registry.AUTH_VARS.values():
        for name in names:
            monkeypatch.setenv(name, "provider-credential")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("UNRELATED_SETTING", "ambient-secret")

    seen = _capture(monkeypatch, _done(json.dumps({"result": "{}"})))
    ask_mod.ask(provider, "m1", "p", tmp_path, [], timeout=5)
    child = seen["env"]

    assert seen["domain"] == env_mod.MODEL
    for name in _AMBIENT_SECRETS:
        assert name not in child
    assert "ambient-secret" not in child.values()
    # Its own sign-in arrives, by name, and the other provider's does not.
    for name in registry.AUTH_VARS[provider]:
        assert child[name] == "provider-credential"
    others = {n for p, names in registry.AUTH_VARS.items() if p != provider for n in names}
    assert others.isdisjoint(child)
    assert child["HOME"] == str(tmp_path)
    # The home directory is where its sign-in lives. A configuration root of the operator's
    # choosing is not, and does not cross.
    assert "XDG_CONFIG_HOME" not in child
    # And that is the whole of it: the harmless base, the home variables, its own names.
    allowed = {*env_mod._BASE, "TMPDIR", *registry.IDENTITY_VARS, *registry.AUTH_VARS[provider]}
    assert set(child) <= allowed


@pytest.mark.parametrize("provider", registry.PROVIDERS)
def test_every_turn_is_told_whose_sign_in_to_use(monkeypatch, tmp_path, provider):
    # On macOS a command that signed in with its own login keeps that sign-in in the Keychain
    # and finds it by the user's name. A turn not told the name cannot sign in on any Mac.
    monkeypatch.setenv("USER", "operator-login-name")
    monkeypatch.setenv("LOGNAME", "operator-login-name")
    monkeypatch.setenv("HOME", str(tmp_path))
    seen = _capture(monkeypatch, _done(json.dumps({"result": "{}"})))
    ask_mod.ask(provider, "m1", "p", tmp_path, [], timeout=5)
    assert seen["env"]["USER"] == "operator-login-name"
    assert seen["env"]["LOGNAME"] == "operator-login-name"
    assert seen["env"]["HOME"] == str(tmp_path)


def test_the_prompt_is_one_argument_never_a_shell_string(monkeypatch, tmp_path):
    prompt = "say `id`; $(touch pwned) && echo 'x' | cat > /dev/null\n--dangerous"
    seen = _capture(monkeypatch, _done(json.dumps({"result": "{}"})))
    ask_mod.ask(FIRST, None, prompt, tmp_path, [], timeout=5)
    assert isinstance(seen["argv"], list)
    assert seen["argv"].count(prompt) == 1
    assert seen["argv"][seen["argv"].index("-p") + 1] == prompt
    # Nothing is fed to it on stdin, and nothing asks for a shell.
    assert "input_text" not in seen and "shell" not in seen


def test_the_turn_runs_in_the_repository_with_the_isolation_flags(monkeypatch, tmp_path):
    seen = _capture(monkeypatch, _done(json.dumps({"result": "{}"})))
    extra = tmp_path / "history"
    ask_mod.ask(FIRST, "some-model", "p", tmp_path, [extra], timeout=7)
    argv = seen["argv"]
    assert seen["cwd"] == tmp_path and seen["timeout"] == 7
    assert "--safe-mode" in argv
    assert argv[argv.index("--model") + 1] == "some-model"
    dirs = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
    assert dirs == [str(tmp_path), str(extra)]


def test_second_provider_reads_its_final_file_and_deletes_it(monkeypatch, tmp_path):
    monkeypatch.setattr(ask_mod, "resolve", lambda p: f"/usr/bin/{p}")
    monkeypatch.setattr(ask_mod, "isolation_flags", lambda p, e: list(registry._known(p).isolation))
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        output = Path(argv[argv.index("--output-last-message") + 1])
        seen["output"] = output
        output.write_text('```json\n[{"task_type":"bug_repair"}]\n```')
        return _done("progress on stdout")

    monkeypatch.setattr(ask_mod, "run", fake_run)
    turn = ask_mod.ask(SECOND, "m1", "prompt", tmp_path, [tmp_path / "history"], 7)
    assert turn.stdout == '```json\n[{"task_type":"bug_repair"}]\n```' and turn.returncode == 0
    assert ask_mod.mining_answer(turn.stdout, SECOND) == [{"task_type": "bug_repair"}]
    assert seen["input_text"] == "prompt" and seen["cwd"] == tmp_path
    assert "prompt" not in seen["argv"] and "--add-dir" not in seen["argv"]
    assert seen["argv"][-1] == "-"
    assert seen["output"].exists() is False


def test_second_provider_is_sent_no_answer_schema(monkeypatch, tmp_path):
    monkeypatch.setattr(ask_mod, "resolve", lambda p: f"/usr/bin/{p}")
    monkeypatch.setattr(ask_mod, "isolation_flags", lambda p, e: list(registry._known(p).isolation))
    seen = _capture(monkeypatch, _done(""))
    ask_mod.ask(SECOND, "m1", "prompt", tmp_path, [], 7)
    assert "--output-schema" not in seen["argv"]


@pytest.mark.parametrize("provider", registry.PROVIDERS)
def test_no_argument_grants_a_shell_or_a_write(provider):
    flags = registry._known(provider).isolation
    argv = ask_mod.build_argv(f"/usr/bin/{provider}", "m", "p", [Path("/r")], list(flags))
    joined = " ".join(argv).lower()
    for word in ("bash", "shell", "write", "edit", "danger", "bypass", "full-auto", "yolo"):
        assert word not in joined


def test_shell_true_appears_nowhere_in_the_providers_package():
    package = Path(ask_mod.__file__).parent
    for source in package.glob("*.py"):
        assert "shell=True" not in source.read_text()
