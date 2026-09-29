import json

import pytest

from hazina_review import emit
from hazina_review.lanes import census
from hazina_review.providers.ask import Turn
from hazina_review.providers.registry import PROVIDERS
from hazina_scan.schema import EmissionRefused

GOOD = "add cursor pagination to a list endpoint backed by a relational store"
NAMES_A_FILE = "rewrite src/auth/session.py to rotate tokens"
NAMES_A_SYMBOL = "the PaymentReconciler class retries on a stale ledger lock"

COUNTS = (
    *(f"n_{name}" for name in census.CATEGORIES),
    "n_minable_ideas",
    "minable_ideas_total",
    "defect_repairs_with_regression_test",
    "census_attempts",
)


def _scored(payload=None, **over):
    block = {
        **census.score(payload or {"complex_logic": [GOOD]}),
        "provider": PROVIDERS[0],
        "model": None,
        "scored": True,
        "ok": True,
    }
    return {**block, **over}


def _unmeasured(**over):
    block = {
        **census.unmeasured(),
        "provider": PROVIDERS[0],
        "model": None,
        "scored": False,
        "ok": False,
        "census_failure_kind": "timeout",
        "timed_out": True,
        "census_attempts": 1,
    }
    return {**block, **over}


def test_a_clean_scored_block_passes_unchanged():
    block = _scored({"complex_logic": [GOOD], "themes": ["pricing"], "self_contained": 3})
    out = emit.material_block(block)
    assert out == block and out is not block
    assert out["n_complex_logic"] == 1
    assert out["minable_ideas"]["complex_logic"] == [GOOD]


def test_a_sentence_naming_a_file_is_emptied_at_the_boundary_and_leaves_its_list():
    block = _scored()
    block["minable_ideas"]["complex_logic"] = [NAMES_A_FILE, GOOD]
    assert emit.material_block(block)["minable_ideas"]["complex_logic"] == [GOOD]


def test_a_sentence_naming_a_symbol_is_emptied_at_the_boundary_and_leaves_its_list():
    block = _scored()
    block["minable_ideas"]["hardening_work"] = [NAMES_A_SYMBOL]
    assert emit.material_block(block)["minable_ideas"]["hardening_work"] == []


def test_a_masked_theme_got_past_scoring_and_is_emptied_here():
    block = _scored({"themes": ["see src/auth/session.py", "pricing"]})
    assert block["themes"] == ["see [path]", "pricing"]
    assert emit.material_block(block)["themes"] == ["pricing"]


def test_a_masked_summary_got_past_scoring_and_is_emptied_here():
    block = _scored({"material_summary": "Most of the logic lives in app/services/engine.py."})
    assert "[path]" in block["material_summary"]
    assert emit.material_block(block)["material_summary"] == ""


def test_a_summary_that_names_a_path_outright_is_emptied():
    block = _scored(material_summary="Most of the logic lives in app/services/engine.py.")
    assert emit.material_block(block)["material_summary"] == ""


def test_a_summary_may_run_to_three_sentences_but_no_further():
    three = " ".join(["a fare engine applies tiered multipliers with rounding rules."] * 3)
    assert emit.material_block(_scored(material_summary=three))["material_summary"] == three
    assert emit.material_block(_scored(material_summary=three * 6))["material_summary"] == ""


def test_what_could_not_be_assessed_passes_when_it_names_nothing():
    said = "generated parsers were too large to read in full"
    out = emit.material_block(_scored(what_i_could_not_assess=said))
    assert out["what_i_could_not_assess"] == said


@pytest.mark.parametrize(
    "said",
    [
        "could not open src/auth/session.py",
        NAMES_A_SYMBOL,
        "the adapter written for Acme was not readable",
        "word " * 60,
    ],
)
def test_what_could_not_be_assessed_is_emptied_when_it_names_anything_or_runs_on(said):
    block = _scored({"complex_logic": [GOOD], "what_i_could_not_assess": said})
    assert emit.material_block(block)["what_i_could_not_assess"] == ""


def test_every_label_for_what_the_masking_removed_passes():
    labels = sorted(census.REMOVED_KINDS)
    assert (
        emit.material_block(_scored(redacted_from_examples=labels))["redacted_from_examples"]
        == labels
    )


@pytest.mark.parametrize("label", ["an identifier", "a URL", "other", "src/auth/session.py", ""])
def test_a_label_outside_the_vocabulary_is_written_as_other(label):
    out = emit.material_block(_scored(redacted_from_examples=["a filename", label]))
    assert out["redacted_from_examples"] == ["a filename", "other"]


def test_a_label_that_is_not_a_string_refuses_the_write():
    with pytest.raises(EmissionRefused):
        emit.material_block(_scored(redacted_from_examples=[7]))


def test_the_probe_is_a_vocabulary_of_one():
    assert emit.material_block(_scored())["probe"] == census.PROBE
    assert emit.material_block(_unmeasured(probe=census.PROBE))["probe"] == census.PROBE
    for probe in ("code_structure", "src/auth/session.py", ""):
        with pytest.raises(EmissionRefused):
            emit.material_block(_scored(probe=probe))


def test_a_category_outside_the_nine_refuses_the_write():
    block = _scored()
    block["minable_ideas"]["astrology"] = [GOOD]
    with pytest.raises(EmissionRefused):
        emit.material_block(block)


def test_an_undeclared_key_refuses_the_write():
    with pytest.raises(EmissionRefused):
        emit.material_block(_scored(favourite_colour="blue"))


@pytest.mark.parametrize(
    ("field", "value"),
    [("logic_depth", 9), ("logic_depth", -1), ("self_contained", 5), ("self_contained", -1)],
)
def test_a_number_is_not_held_to_a_range(field, value):
    # a number is checked for being a number, not for its range
    assert emit.material_block(_scored(**{field: value}))[field] == value


@pytest.mark.parametrize(("field", "top"), [("logic_depth", 6), ("self_contained", 4)])
def test_a_band_may_sit_at_either_end_of_its_range(field, top):
    assert emit.material_block(_scored(**{field: 0}))[field] == 0
    assert emit.material_block(_scored(**{field: top}))[field] == top


@pytest.mark.parametrize("field", COUNTS)
def test_a_count_that_is_not_a_finite_number_refuses_the_write(field):
    for value in ("3", float("nan"), [1]):
        with pytest.raises(EmissionRefused):
            emit.material_block(_scored(**{field: value}))


def test_a_flag_written_into_a_count_refuses_the_write():
    with pytest.raises(EmissionRefused):
        emit.material_block(_scored(n_complex_logic=True))


def test_a_flag_that_is_not_a_boolean_refuses_the_write():
    with pytest.raises(EmissionRefused):
        emit.material_block(_scored(scored="yes"))


def test_an_unmeasured_block_carries_nulls_through_and_never_a_zero():
    block = _unmeasured()
    out = emit.material_block(block)
    assert out == block
    for name in census.unmeasured():
        assert out[name] is None


def test_a_provider_outside_the_registry_refuses_the_write():
    for provider in PROVIDERS:
        assert emit.material_block(_scored(provider=provider))["provider"] == provider
    with pytest.raises(EmissionRefused):
        emit.material_block(_scored(provider="src/auth/session.py"))


@pytest.mark.parametrize("kind", census.FAILURE_KINDS)
def test_every_failure_kind_the_lane_can_name_passes(kind):
    assert emit.material_block(_unmeasured(census_failure_kind=kind))["census_failure_kind"] == kind


@pytest.mark.parametrize("kind", ["exit_1", "the command fell over in src/auth", ""])
def test_a_failure_in_free_text_refuses_the_write(kind):
    with pytest.raises(EmissionRefused):
        emit.material_block(_unmeasured(census_failure_kind=kind))


@pytest.mark.parametrize(
    "model", ["m1", "large-4.5", "family.large-v2:0", "vendor/large-4", "src/auth/session.py"]
)
def test_a_model_id_passes(model):
    assert emit.material_block(_scored(model=model))["model"] == model


@pytest.mark.parametrize(
    "model",
    [
        "/home/someone/checkout",
        "..\\checkout\\session",
        "the large one please",
        "m" * 200,
    ],
)
def test_a_model_that_is_a_path_or_a_sentence_refuses_the_write(model):
    with pytest.raises(EmissionRefused):
        emit.material_block(_scored(model=model))


@pytest.mark.parametrize(
    "turn",
    [
        Turn("exited", 0, json.dumps({"result": json.dumps({"complex_logic": [GOOD]})})),
        Turn("exited", 2, "", "something went wrong"),
    ],
)
def test_whatever_the_lane_collects_is_declared(monkeypatch, tmp_path, turn):
    monkeypatch.setattr(census, "ask", lambda *args, **kwargs: turn)
    block, _ = census.collect(tmp_path, tmp_path, provider=PROVIDERS[0], model="m1", timeout=5)
    assert emit.material_block(block).keys() == block.keys()


def test_a_failed_lane_says_what_happened_in_its_own_words():
    block = census.unscored(PROVIDERS[0], "m1", "unknown", returncode=2, seconds=60)
    out = emit.material_block(block)
    assert out["census_failure_kind"] == "unknown" and out["census_retryable"] is False
    # the masking reads the plural in brackets as a call and the acronym as a constant
    assert out["error"] == (
        f"census failed (unknown): the [identifier] failed for a reason it did not explain; "
        f"{PROVIDERS[0]} exited 2; 1 [function]; criteria unscored"
    )
    assert out["logic_depth_unavailable_reason"] == (
        "material census unavailable: the [identifier] failed for a reason it did not explain"
    )
    assert out["census_error_detail"] == ""


def test_every_string_is_masked_again_after_the_boundary():
    # A capital and a digit open a sentence the rules let through; the masking then takes the
    # word for a constant.
    block = _scored({"complex_logic": ["A1 routing retries each failed hop"]})
    assert block["minable_ideas"]["complex_logic"] == ["A1 routing retries each failed hop"]
    out = emit.material_block(block)
    assert out["minable_ideas"]["complex_logic"] == ["[identifier] routing retries each failed hop"]


def test_the_timing_note_is_a_tool_note():
    note = (
        "lane wall clock in seconds: census 12, mining 30, total 31; independent lanes ran at "
        "the same time, so the total is below their sum"
    )
    assert emit.material_block(_scored(census_platform_note=note))["census_platform_note"] == note
