import pytest

from hazina_scan import redact


def test_scrub_replaces_paths_and_identifiers():
    assert "[path]" in redact.scrub("see src/main.py for details")
    assert "[email]" in redact.scrub("mail dev@example.com")
    assert "[identifier]" in redact.scrub("set the DB_URL first")


def test_leaks_reports_what_survives():
    assert redact.leaks("a plain sentence") is None
    assert redact.leaks("dev@example.com") == "an email address"


def test_redact_tree_drops_identity_keys_and_scrubs_strings():
    out = redact.redact_tree({"repo_path": "/x", "note": "in lib/a.py", "loc": 3})
    assert out == {"note": "in [path]", "loc": 3}


def test_redact_tree_leaves_closed_vocabulary_values_alone():
    out = redact.redact_tree({"tree": {"detected_frameworks": ["Next.js", "GitHub Actions"]}})
    assert out["tree"]["detected_frameworks"] == ["Next.js", "GitHub Actions"]


def test_redact_tree_masks_a_lint_config_value_as_a_filename():
    # A lint config's file name is written masked as a filename.
    lint = {"ruff": "pyproject.toml", "ESLint": ".eslintrc.json", "Flake8": "setup.cfg"}
    out = redact.redact_tree({"tree": {"linters_and_formatters": lint}})
    assert out["tree"]["linters_and_formatters"] == {
        "ruff": "[file]",
        "ESLint": ".[file]",
        "Flake8": "setup.cfg",
    }
    redact.audit_no_leak(out)


def test_audit_raises_on_a_surviving_path():
    with pytest.raises(redact.LeakDetected):
        redact.audit_no_leak({"tree": {"readme_loc": 3, "bounds": "see src/x.py"}})


def test_audit_exempts_provenance():
    redact.audit_no_leak({"real_repo_name": "acme/demo", "repo_digest": "a" * 64})


@pytest.mark.parametrize(
    "token",
    [
        "AcmeAuthProvider_Legacy",
        "InternalBillingService_v3",
        "CompanyName_Prod",
    ],
)
def test_scrub_and_leaks_catch_pascalcase_with_underscore_suffix(token):
    assert redact.scrub(token) == "[identifier]"
    assert redact.leaks(token) == "an identifier"


def test_scrub_and_leaks_catch_two_segment_dotted_identifiers():
    assert redact.scrub("auth.check") == "[identifier]"
    assert redact.leaks("auth.check") == "an identifier"


def test_sentence_punctuation_with_a_space_after_the_period_is_left_alone():
    text = "it works. Then it stops"
    assert redact.scrub(text) == text
    assert redact.leaks(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "e.g. this is normal",
        "i.e. that one too",
        "the U.S. government requires this",
    ],
)
def test_single_letter_abbreviations_are_left_alone(text):
    assert redact.scrub(text) == text
    assert redact.leaks(text) is None


@pytest.mark.parametrize("token", ["auth.check", "db.models"])
def test_two_segment_dotted_identifiers_still_scrub(token):
    assert redact.scrub(token) == "[identifier]"
    assert redact.leaks(token) == "an identifier"
