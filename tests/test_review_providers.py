import subprocess

import pytest

from hazina_review.providers import registry

# The export guard refuses a provider's name in any file the scan export carries, and that
# includes this one, so the name is read from the registry rather than spelled here.
FIRST = registry.PROVIDERS[0]
SECOND = registry.PROVIDERS[1]
_SANDBOX_READS = registry.sandbox_reads
_HELP_OF = registry.help_of
REQUIRED = ("--safe-mode", "--restricted", "--strict-mcp-config", "--no-session-persistence")
EVERY_FLAG = "\n".join(f"  {flag}   what it does" for flag in REQUIRED)


@pytest.fixture(autouse=True)
def _fresh_help_cache(request, monkeypatch):
    # What the registry remembers, it remembers for the process. Forgotten on both sides of
    # every test so nothing one test learned about an executable can answer for it in another.
    _HELP_OF.cache_clear()
    _SANDBOX_READS.cache_clear()
    # Unless a test is about it, the provider's own sandbox works.
    if "sandbox_check" not in request.keywords:
        monkeypatch.setattr(registry, "sandbox_reads", lambda executable, check: True)
    yield
    _HELP_OF.cache_clear()
    _SANDBOX_READS.cache_clear()


def _fake_probe(monkeypatch, text=EVERY_FLAG, error=None):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if error is not None:
            raise error
        return subprocess.CompletedProcess(argv, 0, stdout=text, stderr="")

    monkeypatch.setattr(registry.subprocess, "run", fake_run)
    return calls


def test_every_provider_names_the_model_a_run_uses_when_none_is_asked_for():
    from hazina_review import emit

    assert set(registry.DEFAULT_MODELS) == set(registry.PROVIDERS)
    assert registry.DEFAULT_MODELS[SECOND] == "gpt-6-sol"
    for provider, model in registry.DEFAULT_MODELS.items():
        # A pinned id and not an alias: an alias is a different model from one month to the
        # next, and two runs under different models are not the same measurement.
        assert emit.MODEL_ID.apply(model, "model") == model, provider
        assert any(ch.isdigit() for ch in model), provider


def test_second_provider_probes_its_exec_help_and_requires_its_switches(monkeypatch):
    known = registry._known(SECOND)
    calls = _fake_probe(monkeypatch, "\n".join(known.required))
    assert registry.isolation_flags(SECOND, f"/usr/bin/{SECOND}") == list(known.isolation)
    assert calls[0][0] == [f"/usr/bin/{SECOND}", "exec", "--help"]
    _HELP_OF.cache_clear()
    _fake_probe(monkeypatch, "--sandbox --ephemeral")
    with pytest.raises(registry.ProviderNotIsolated):
        registry.isolation_flags(SECOND, f"/usr/bin/{SECOND}")


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError):
        registry.resolve("wizard")


def test_missing_cli_raises_provider_unavailable(monkeypatch):
    monkeypatch.setattr(registry.shutil, "which", lambda _: None)
    with pytest.raises(registry.ProviderUnavailable):
        registry.resolve(FIRST)


def test_cli_without_the_required_flags_stops_the_run(monkeypatch):
    monkeypatch.setattr(registry, "_help_text", lambda *a: "--version\n--help\n")
    with pytest.raises(registry.ProviderNotIsolated) as exc:
        registry.isolation_flags(FIRST, f"/usr/bin/{FIRST}")
    assert "upgrade" in str(exc.value).lower()


def test_every_flag_is_required_and_every_flag_is_returned(monkeypatch):
    monkeypatch.setattr(registry, "_help_text", lambda *a: EVERY_FLAG)
    assert registry.isolation_flags(FIRST, f"/usr/bin/{FIRST}") == list(REQUIRED)


@pytest.mark.parametrize("absent", REQUIRED)
def test_a_cli_lacking_any_one_flag_stops_the_run_and_is_told_which(monkeypatch, absent):
    offered = "\n".join(flag for flag in REQUIRED if flag != absent)
    monkeypatch.setattr(registry, "_help_text", lambda *a: offered)
    with pytest.raises(registry.ProviderNotIsolated) as exc:
        registry.isolation_flags(FIRST, f"/usr/bin/{FIRST}")
    said = str(exc.value)
    assert f"does not offer {absent}." in said and "upgrade" in said.lower()


def test_what_a_help_text_lacks_is_answered_without_starting_anything(monkeypatch):
    monkeypatch.setattr(registry.subprocess, "run", lambda *a, **k: pytest.fail("started"))
    (known,) = (k for k in registry._KNOWN if k.name == FIRST)
    assert known.lacking(EVERY_FLAG) == ()
    assert known.lacking("--restricted-mode --safe-mode") == REQUIRED[1:]


@pytest.mark.parametrize(
    "lookalike", ["--safe-mode-off", "--no-safe-mode", "--unsafe-mode", "x--safe-mode"]
)
def test_a_longer_flag_that_merely_contains_the_name_does_not_count(monkeypatch, lookalike):
    offered = EVERY_FLAG.replace("--safe-mode", lookalike)
    monkeypatch.setattr(registry, "_help_text", lambda *a: offered)
    with pytest.raises(registry.ProviderNotIsolated) as exc:
        registry.isolation_flags(FIRST, f"/usr/bin/{FIRST}")
    assert "does not offer --safe-mode." in str(exc.value)


def test_help_that_cannot_be_read_stops_the_run(monkeypatch):
    _fake_probe(monkeypatch, error=subprocess.TimeoutExpired(cmd=FIRST, timeout=30))
    with pytest.raises(registry.ProviderNotIsolated):
        registry.isolation_flags(FIRST, f"/usr/bin/{FIRST}")


def test_a_command_that_will_not_start_stops_the_run(monkeypatch):
    _fake_probe(monkeypatch, error=FileNotFoundError("gone"))
    with pytest.raises(registry.ProviderNotIsolated):
        registry.isolation_flags(FIRST, f"/usr/bin/{FIRST}")


def test_help_is_read_once_per_executable(monkeypatch):
    calls = _fake_probe(monkeypatch)
    registry.isolation_flags(FIRST, f"/usr/bin/{FIRST}")
    registry.isolation_flags(FIRST, f"/usr/bin/{FIRST}")
    assert len(calls) == 1
    registry.isolation_flags(FIRST, f"/opt/other/{FIRST}")
    assert len(calls) == 2


def test_the_probe_carries_no_credential_and_no_shell(monkeypatch, tmp_path):
    for name in (registry.AUTH_VARS[FIRST][0], "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN"):
        monkeypatch.setenv(name, "planted")
    monkeypatch.chdir(tmp_path)
    calls = _fake_probe(monkeypatch)
    registry.isolation_flags(FIRST, f"/usr/bin/{FIRST}")
    argv, kwargs = calls[0]
    assert argv == [f"/usr/bin/{FIRST}", "--help"]
    assert kwargs["shell"] is False
    assert "planted" not in kwargs["env"].values()
    # Isolation is not proven yet, so the probe does not start inside the measured tree.
    assert kwargs["cwd"] is not None and kwargs["cwd"] != str(tmp_path)


def test_auth_variables_are_named_for_every_provider_and_none_is_denied(monkeypatch):
    from hazina_scan.env import MODEL, build_env

    assert set(registry.AUTH_VARS) == set(registry.PROVIDERS)
    assert all(registry.AUTH_VARS[provider] for provider in registry.PROVIDERS)
    for names in registry.AUTH_VARS.values():
        for name in names:
            monkeypatch.setenv(name, "x")
        build_env(passthrough=names, domain=MODEL)


def test_the_second_command_is_not_required_to_offer_a_flag_it_is_never_given():
    known = next(p for p in registry._KNOWN if p.name == registry.PROVIDERS[1])
    assert "--output-schema" not in known.required


# --- the second command's own sandbox has to be able to read a file -----------------------


def _fake_sandbox(monkeypatch, *, reads: bool, code: int = 0):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if not reads:
            return subprocess.CompletedProcess(
                argv, code, stdout="bwrap: loopback: Failed", stderr=""
            )
        # a working sandbox prints the file it was asked to read
        target = argv[-1]
        from pathlib import Path

        return subprocess.CompletedProcess(
            argv, 0, stdout=(Path(kwargs["cwd"]) / target).read_text(), stderr=""
        )

    monkeypatch.setattr(registry.subprocess, "run", fake_run)
    return calls


@pytest.mark.sandbox_check
def test_a_sandbox_that_cannot_read_stops_the_run_before_any_turn(monkeypatch):
    known = registry._known(SECOND)
    monkeypatch.setattr(registry, "help_of", lambda *a: "\n".join(known.required))
    monkeypatch.setattr(registry.sys, "platform", "linux")
    _fake_sandbox(monkeypatch, reads=False, code=1)
    with pytest.raises(registry.ProviderSandboxBroken) as stop:
        registry.isolation_flags(SECOND, f"/usr/bin/{SECOND}")
    assert isinstance(stop.value, registry.ProviderNotIsolated)
    assert "could not open" in str(stop.value)


@pytest.mark.sandbox_check
def test_a_sandbox_that_exits_cleanly_but_prints_nothing_still_stops_the_run(monkeypatch):
    known = registry._known(SECOND)
    monkeypatch.setattr(registry, "help_of", lambda *a: "\n".join(known.required))
    monkeypatch.setattr(registry.sys, "platform", "darwin")
    _fake_sandbox(monkeypatch, reads=False, code=0)
    with pytest.raises(registry.ProviderSandboxBroken):
        registry.isolation_flags(SECOND, f"/usr/bin/{SECOND}")


@pytest.mark.sandbox_check
def test_a_working_sandbox_is_checked_once_with_no_credential(monkeypatch):
    known = registry._known(SECOND)
    monkeypatch.setattr(registry, "help_of", lambda *a: "\n".join(known.required))
    monkeypatch.setattr(registry.sys, "platform", "linux")
    monkeypatch.setenv("OPENAI_API_KEY", "should-not-be-passed")
    calls = _fake_sandbox(monkeypatch, reads=True)
    for _ in range(3):
        assert registry.isolation_flags(SECOND, f"/usr/bin/{SECOND}") == list(known.isolation)
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[:3] == [f"/usr/bin/{SECOND}", "sandbox", "--"]
    assert "OPENAI_API_KEY" not in kwargs["env"]


@pytest.mark.sandbox_check
def test_the_first_command_and_other_platforms_are_not_checked(monkeypatch):
    calls = _fake_sandbox(monkeypatch, reads=False, code=1)
    monkeypatch.setattr(registry, "help_of", lambda *a: EVERY_FLAG)
    registry.isolation_flags(FIRST, f"/usr/bin/{FIRST}")
    known = registry._known(SECOND)
    monkeypatch.setattr(registry, "help_of", lambda *a: "\n".join(known.required))
    monkeypatch.setattr(registry.sys, "platform", "win32")
    registry.isolation_flags(SECOND, f"/usr/bin/{SECOND}")
    assert calls == []
