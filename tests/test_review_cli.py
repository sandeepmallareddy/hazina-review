import pytest

from hazina_review import __version__, cli, preflight, run
from hazina_review.providers.registry import DEFAULT_MODELS, PROVIDERS


@pytest.fixture(autouse=True)
def _checks_pass(monkeypatch):
    """These tests are about what the command line hands the run. The checks before a run have
    tests of their own, and here they pass without starting anything."""
    passing = [preflight.Result(preflight.GIT, preflight.OK, "Fine.")]
    monkeypatch.setattr(preflight, "run_checks", lambda *a, **k: passing)


def test_version_is_reported(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_missing_repo_argument_is_an_error():
    with pytest.raises(SystemExit):
        cli.main([])


def _asked_for(monkeypatch, tmp_path, *extra):
    seen = {}

    def _review_all(repos, out_dir, **options):
        seen.update(options)
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
    assert cli.main([str(tmp_path), "--out", str(tmp_path / "o"), *extra]) == 0
    return seen


def test_a_run_with_no_model_named_uses_the_pinned_one(monkeypatch, tmp_path):
    seen = _asked_for(monkeypatch, tmp_path)
    assert seen["model"] == DEFAULT_MODELS[PROVIDERS[0]] and seen["model"] is not None


def test_a_model_named_on_the_command_line_is_the_one_used(monkeypatch, tmp_path):
    assert _asked_for(monkeypatch, tmp_path, "--model", "m1.5")["model"] == "m1.5"


def test_second_provider_requires_an_explicit_model(tmp_path, capsys):
    assert cli.main([str(tmp_path), "--provider", PROVIDERS[1]]) == 2
    assert "requires --model" in capsys.readouterr().err


def test_second_provider_passes_the_named_model(monkeypatch, tmp_path):
    chosen = _asked_for(monkeypatch, tmp_path, "--provider", PROVIDERS[1], "--model", "m1.5")
    assert chosen["provider"] == PROVIDERS[1] and chosen["model"] == "m1.5"


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
@pytest.mark.parametrize("check_only", [False, True])
def test_a_limit_out_of_range_stops_before_any_check_is_made(
    monkeypatch, tmp_path, capsys, flag, value, check_only
):
    # The checks end in a billed session, so a command that cannot run must never reach them.
    def _never(*a, **k):
        pytest.fail("a check was started for a command that cannot run")

    monkeypatch.setattr(preflight, "run_checks", _never)
    monkeypatch.setattr(preflight, "check_model", _never)
    monkeypatch.setattr(run, "review_all", _never)
    extra = ["--check"] if check_only else []
    assert cli.main([str(tmp_path), "--out", str(tmp_path / "o"), flag, value, *extra]) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("hazina-review: ") and "must be positive" in captured.err
    assert captured.out == ""
    assert not (tmp_path / "o").exists()


def test_the_default_output_directory_is_not_the_scanners():
    # Run one after the other with no flags, the two tools would otherwise share a folder,
    # and a review removes an index file the scanner still writes there.
    assert cli.build_parser().parse_args(["repo"]).out == "hazina-review-out"
