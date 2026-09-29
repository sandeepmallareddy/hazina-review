import json
import re

import pytest

from hazina_review.lanes import census
from hazina_review.providers.ask import Turn
from hazina_review.providers.registry import PROVIDERS
from hazina_review.sentences import redact
from hazina_scan.schema import MAX_SENTENCE_WORDS

GOOD = "a fare engine applying tiered surge multipliers with rounding rules per currency"
NAMES_A_FILE = "rewrite src/auth/session.py to rotate tokens"


@pytest.mark.parametrize(
    ("total", "band"),
    [
        (0.0, 0),
        (0.49, 0),
        (0.5, 1),
        (2.9, 1),
        (3.0, 2),
        (6.9, 2),
        (7.0, 3),
        (13.9, 3),
        (14.0, 4),
        (27.9, 4),
        (28.0, 5),
        (54.9, 5),
        (55.0, 6),
        (900.0, 6),
    ],
)
def test_band_thresholds(total, band):
    assert census.depth_band(total) == band


def test_there_are_nine_categories_and_each_has_a_weight():
    assert len(census.CATEGORIES) == 9
    assert set(census.WEIGHTS) == set(census.CATEGORIES)


def test_weighted_total_uses_each_category_weight():
    counts = {"complex_logic": 2, "substantial_features": 10, "performance_work": 4}
    assert census.weighted_total(counts, 0) == pytest.approx(2 * 1.0 + 10 * 0.7 + 4 * 0.5)


def test_regression_tests_add_a_bonus_capped_at_the_number_of_repairs():
    counts = {"defect_repairs": 2}
    assert census.weighted_total(counts, 9) == pytest.approx(2 * 0.7 + 0.4 * 2)


def test_count_is_the_length_of_the_surviving_list():
    out = census.score({"complex_logic": [GOOD, NAMES_A_FILE]})
    assert out["n_complex_logic"] == 1
    assert out["minable_ideas"]["complex_logic"] == [GOOD]


def test_a_sentence_naming_a_file_is_dropped_whole_not_kept_masked():
    out = census.score({"defect_repairs": [NAMES_A_FILE, "fix config.yaml defaults for retries"]})
    assert out["minable_ideas"]["defect_repairs"] == []
    assert out["n_defect_repairs"] == 0 and out["n_minable_ideas"] == 0


def test_entries_that_are_not_sentences_are_ignored():
    out = census.score({"complex_logic": [None, 7, "", "   ", ["nested"], {"a": 1}, GOOD, 3.5]})
    assert out["minable_ideas"]["complex_logic"] == [GOOD]
    assert out["n_complex_logic"] == 1


def test_a_category_is_walked_as_expected():
    # missing, empty or null: nothing. A string: its characters, each a one-letter sentence.
    # A mapping: its keys. Anything that cannot be walked holds nothing.
    out = census.score(
        {
            "complex_logic": "ab c",
            "work_units": 12,
            "hardening_work": None,
            "defect_repairs": {GOOD: 1},
        }
    )
    assert out["minable_ideas"]["complex_logic"] == ["a", "b", "c"]
    assert out["minable_ideas"]["defect_repairs"] == [GOOD]
    assert out["n_work_units"] == out["n_hardening_work"] == out["n_performance_work"] == 0
    assert out["census_hit_cap"] is False
    assert census.score({"complex_logic": "x" * census.SANITY_CAP})["census_hit_cap"] is True


def test_the_bonus_in_a_score_never_exceeds_the_surviving_repairs():
    out = census.score(
        {"defect_repairs": [GOOD, NAMES_A_FILE], "defect_repairs_with_regression_test": 2}
    )
    assert out["defect_repairs_with_regression_test"] == 1
    assert out["minable_ideas_total"] == pytest.approx(0.7 + 0.4)


def test_self_contained_is_clamped():
    assert census.score({"self_contained": 11})["self_contained"] == 4
    assert census.score({"self_contained": -3})["self_contained"] == 0
    assert census.score({"self_contained": "junk"})["self_contained"] == 0


def test_an_unknown_category_is_ignored():
    out = census.score({"astrology": [GOOD]})
    assert "n_astrology" not in out and out["n_minable_ideas"] == 0


def test_a_category_at_the_sanity_cap_is_reported():
    out = census.score({"complex_logic": [GOOD] * census.SANITY_CAP})
    assert out["census_hit_cap"] is True


def test_a_category_under_the_sanity_cap_is_not_reported():
    out = census.score({"complex_logic": [GOOD] * (census.SANITY_CAP - 1)})
    assert out["census_hit_cap"] is False
    assert out["n_complex_logic"] == census.SANITY_CAP - 1


def test_a_long_category_keeps_exactly_the_cap_and_reports_it():
    out = census.score({"work_units": [GOOD] * 450})
    assert out["n_work_units"] == 400 == census.SANITY_CAP
    assert len(out["minable_ideas"]["work_units"]) == 400
    assert out["census_hit_cap"] is True


def test_the_cap_is_on_what_is_kept_not_on_what_is_examined():
    out = census.score({"work_units": [NAMES_A_FILE] * 100 + [GOOD] * 350})
    assert out["n_work_units"] == 350
    assert out["census_hit_cap"] is True


def test_the_cap_is_reported_from_the_list_as_received():
    out = census.score({"work_units": [NAMES_A_FILE] * 400})
    assert out["n_work_units"] == 0
    assert out["census_hit_cap"] is True


def test_at_most_five_themes_each_masked_and_cut_short():
    out = census.score({"themes": ["pricing", "", "x" * 90, "see src/auth/session.py", 5, 6, 7]})
    assert out["themes"][0] == "pricing"
    assert out["themes"][1] == "x" * 70
    assert "session" not in " ".join(out["themes"])
    # the blank one is gone, and the sixth and seventh were never looked at
    assert len(out["themes"]) == 4 and out["themes"][3] == "5"


def test_the_summary_is_masked():
    out = census.score({"material_summary": "most of the depth sits in src/auth/session.py"})
    assert "session" not in out["material_summary"]
    assert census.score({})["material_summary"] == ""


def test_an_empty_answer_scores_zero_everywhere():
    out = census.score({})
    assert out["logic_depth"] == 0 and out["minable_ideas_total"] == 0.0
    assert all(out[f"n_{name}"] == 0 for name in census.CATEGORIES)
    assert out["census_attempts"] == 1 and out["timed_out"] is False
    assert census.score({}, attempts=3)["census_attempts"] == 3


def test_the_band_is_read_from_the_weighted_total():
    out = census.score({"complex_logic": [GOOD] * 3, "performance_work": [GOOD] * 2})
    assert out["minable_ideas_total"] == pytest.approx(4.0)
    assert out["logic_depth"] == 2


def test_unmeasured_is_null_everywhere_and_never_zero():
    blank = census.unmeasured()
    assert blank["logic_depth"] is None and blank["n_complex_logic"] is None
    assert all(v is None for v in blank.values())


def test_scoring_is_deterministic():
    payload = {"complex_logic": [GOOD], "self_contained": 3, "themes": ["pricing"]}
    assert census.score(payload) == census.score(payload)


def _answered(answer):
    """A turn that exited cleanly with `answer` as the model's words."""
    return Turn("exited", 0, json.dumps({"result": json.dumps(answer)}), "")


def _turns(*turns):
    """A stand-in for the provider seam that ends each turn as the next of `turns` says."""
    queue = list(turns)
    seen = []

    def fake(*args, **kwargs):
        seen.append(args)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    fake.seen = seen
    return fake


def _answers(answer, note=None):
    if note == "timeout":
        return _turns(Turn("timeout"))
    return _turns(_answered(answer))


def _collect(tmp_path, fake, monkeypatch, *, timeout=5, **options):
    monkeypatch.setattr(census, "ask", fake)
    waits = []
    block, answer = census.collect(
        tmp_path,
        tmp_path,
        provider=PROVIDERS[0],
        model=None,
        timeout=timeout,
        sleep=waits.append,
        jitter=lambda: 0.5,
        **options,
    )
    return block, answer, waits


def test_a_failed_turn_yields_nulls_and_names_the_failure(monkeypatch, tmp_path):
    block, answer, _ = _collect(tmp_path, _answers(None, "timeout"), monkeypatch)
    assert answer is None
    assert block["scored"] is False and block["ok"] is False
    assert block["census_failure_kind"] == "timeout" and block["timed_out"] is True
    assert block["logic_depth"] is None and block["n_complex_logic"] is None
    assert all(block[name] is None for name in census.unmeasured())


def test_a_good_turn_is_scored_and_records_what_was_requested(monkeypatch, tmp_path):
    raw = {**{name: [] for name in census.CATEGORIES}, "self_contained": 2, "complex_logic": [GOOD]}
    # the second command writes its answer to a file, with no envelope round it
    monkeypatch.setattr(census, "ask", _turns(Turn("exited", 0, json.dumps(raw))))
    block, answer = census.collect(
        tmp_path, tmp_path, provider=PROVIDERS[-1], model="m1", timeout=5
    )
    assert block["scored"] is True and block["ok"] is True
    assert block["n_complex_logic"] == 1 and block["census_attempts"] == 1
    assert block["provider"] == PROVIDERS[-1] and block["model"] == "m1"
    assert "census_failure_kind" not in block


def test_the_raw_answer_is_handed_back_beside_the_block_never_inside_it(monkeypatch, tmp_path):
    raw = {
        **{name: [] for name in census.CATEGORIES},
        "self_contained": 2,
        "complex_logic": [GOOD, NAMES_A_FILE],
    }
    block, answer, _ = _collect(tmp_path, _answers(raw), monkeypatch)
    assert answer == raw
    assert "session" not in repr(block)


_ENVELOPE = '{{"type": "result", "is_error": true, "result": "{}"}}'


@pytest.mark.parametrize(
    ("turn", "kind", "again", "status"),
    [
        (Turn("timeout"), "timeout", False, None),
        (Turn("missing", error=FileNotFoundError(2, "No such file")), "cli_missing", False, None),
        (Turn("unstartable", error=PermissionError(13, "denied")), "crashed", False, None),
        (Turn("exited", 0, "not json at all"), "no_json", False, 0),
        (Turn("exited", 0, json.dumps({"result": "[]"})), "no_json", False, 0),
        (Turn("exited", 1, "", "API Error: 429 Too Many Requests"), "rate_limited", True, 1),
        (Turn("exited", 1, "", "API Error: 529 Overloaded"), "rate_limited", True, 1),
        (Turn("exited", 1, _ENVELOPE.format("Invalid API key")), "auth", False, 1),
        (Turn("exited", 1, "", "OAuth token has expired"), "auth", False, 1),
        (Turn("exited", 1, "", "Credit balance is too low"), "auth", False, 1),
        (Turn("exited", 1, "", "model not found: x-nonesuch"), "model_unavailable", False, 1),
        (Turn("exited", 1, "", "API Error: 404 not_found_error"), "model_unavailable", False, 1),
        # an expired credential mentioned beside a limit is a credential problem
        (Turn("exited", 1, "", "401 unauthorized: rate limit headers"), "auth", False, 1),
        (Turn("exited", 1, "", "Error: socket hang up"), "crashed", True, 1),
        (Turn("exited", 1, "", "API Error: 503 Service Unavailable"), "crashed", True, 1),
        (Turn("exited", 1, "", "Segmentation fault (core dumped)"), "crashed", False, 1),
        (Turn("exited", 1, "", "FATAL ERROR: heap limit"), "crashed", False, 1),
        (Turn("exited", 1), "unknown", False, 1),
        (Turn("exited", 128), "unknown", False, 128),
        (Turn("exited", -9), "crashed", False, -9),
        (Turn("exited", 137), "crashed", False, 137),
        (Turn("exited", 166), "unknown", False, 166),
        (Turn("exited", 3221225477), "crashed", False, 3221225477),
        # a refusal on a clean exit is read from its envelope, and only when nothing parsed
        (Turn("exited", 0, _ENVELOPE.format("usage limit reached")), "rate_limited", True, 0),
        (Turn("exited", 0, _ENVELOPE.format("something odd")), "no_json", False, 0),
    ],
)
def test_every_failure_is_named_from_a_closed_vocabulary(
    monkeypatch, tmp_path, turn, kind, again, status
):
    block, _, _ = _collect(tmp_path, _turns(turn), monkeypatch, timeout=5)
    assert block["census_failure_kind"] == kind
    assert kind in census.FAILURE_KINDS
    assert block["census_retryable"] is again
    assert block["error"].startswith(f"census failed ({kind}): {census.HUMAN[kind]}")
    assert (f"{PROVIDERS[0]} exited {status}" in block["error"]) is (status is not None)
    assert block["logic_depth_unavailable_reason"].startswith(
        f"material census unavailable: {census.HUMAN[kind]}"
    )
    assert block["timed_out"] is (kind == "timeout")
    assert block["scored"] is False and block["logic_depth"] is None


def test_an_envelope_with_no_answer_in_it_is_itself_the_answer(monkeypatch, tmp_path):
    """With nothing under the answer key, the whole of what was
    printed is searched, and the envelope is an object."""
    turn = Turn("exited", 0, json.dumps({"error": {"message": "quota"}}))
    block, answer, _ = _collect(tmp_path, _turns(turn), monkeypatch)
    assert block["scored"] is True and block["n_minable_ideas"] == 0
    assert answer == {"error": {"message": "quota"}}


def test_the_rules_read_only_the_last_words_printed():
    early = "rate limit" + " " * census.CLASSIFY_CHARS
    assert census.classify(1, "", early) == ("unknown", False)
    assert census.classify(1, early, "") == ("unknown", False)
    assert census.classify(1, "rate limit", "x" * 10_000) == ("rate_limited", True)


def test_the_evidence_is_the_masked_tail_of_both_streams(monkeypatch, tmp_path):
    turn = Turn("exited", 2, "out " * 300 + "src/auth/session.py", "the end of the errors")
    block, _, _ = _collect(tmp_path, _turns(turn), monkeypatch)
    detail = block["census_error_detail"]
    assert detail.startswith("standard error tail: the end of the errors | standard output tail:")
    assert "session" not in detail and "[path]" in detail
    assert len(detail) <= census.DETAIL_LIMIT
    assert census.tails("", "") == "" and census.tails(None, None) == ""


def test_the_evidence_is_never_longer_than_the_limit():
    assert len(census.tails("a " * 2000, "b " * 2000)) <= census.DETAIL_LIMIT


def test_a_command_that_would_not_start_says_what_the_system_said(monkeypatch, tmp_path):
    turn = Turn("missing", error=FileNotFoundError(2, "No such file or directory", "/x/y"))
    block, _, _ = _collect(tmp_path, _turns(turn), monkeypatch)
    assert block["census_error_detail"] == "[identifier]: No such file or directory"


def test_a_throttled_turn_is_tried_again_and_the_answer_counts(monkeypatch, tmp_path):
    raw = {"complex_logic": [GOOD]}
    limited = Turn("exited", 1, "", "429 Too Many Requests")
    fake = _turns(limited, limited, _answered(raw))
    block, answer, waits = _collect(tmp_path, fake, monkeypatch, timeout=14400)
    assert block["scored"] is True and block["census_attempts"] == 3
    assert answer == raw and len(fake.seen) == 3
    # five then ten seconds, each spread by the jitter; here the jitter is its middle
    assert waits == [5.0, 10.0]


def test_three_throttled_turns_end_the_lane_and_say_why(monkeypatch, tmp_path):
    fake = _turns(Turn("exited", 1, "", "429 Too Many Requests"))
    block, _, waits = _collect(tmp_path, fake, monkeypatch, timeout=14400)
    assert len(fake.seen) == 3 and block["census_attempts"] == 3
    assert block["census_failure_kind"] == "rate_limited" and block["census_retryable"] is True
    assert block["census_error_detail"] == (
        "standard error tail: 429 Too Many Requests | no further attempt: the ceiling of 3 "
        "attempts was reached"
    )
    assert "3 attempt(s)" in block["error"] and waits == [5.0, 10.0]


def test_a_retry_is_not_started_without_room_for_it(monkeypatch, tmp_path):
    clock = [0.0]
    fake = _turns(Turn("exited", 1, "", "overloaded"))

    def ask(*args, **kwargs):
        clock[0] += 50
        return fake(*args, **kwargs)

    block, _, waits = _collect(tmp_path, ask, monkeypatch, timeout=60, now=lambda: clock[0])
    assert len(fake.seen) == 1 and waits == []
    assert block["census_error_detail"].endswith(
        "no further attempt: only 5 of the 60 second budget would remain after backing off, "
        "below the 15 seconds a census needs to be worth starting"
    )


def test_a_failure_not_worth_repeating_is_tried_once(monkeypatch, tmp_path):
    fake = _turns(Turn("exited", 1, "", "Invalid API key"))
    block, _, waits = _collect(tmp_path, fake, monkeypatch, timeout=14400)
    assert len(fake.seen) == 1 and waits == []
    assert block["census_error_detail"] == "standard error tail: Invalid API key"


def test_each_turn_is_given_what_is_left_of_the_lane(monkeypatch, tmp_path):
    clock = [0.0]
    given = []
    fake = _turns(Turn("exited", 1, "", "rate limit"), _answered({}))

    def ask(provider, model, prompt, repo, read_dirs, timeout):
        given.append(timeout)
        clock[0] += 100
        return fake()

    block, _, _ = _collect(tmp_path, ask, monkeypatch, timeout=1000, now=lambda: clock[0])
    assert given == [1000.0, 900.0] and block["census_attempts"] == 2


def test_the_waits_are_capped_one_by_one_and_all_together():
    assert census.wait_before(1, 0, lambda: 0.0) == 2.5
    assert census.wait_before(3, 0, lambda: 1.0) == 30.0
    assert census.wait_before(3, 40, lambda: 1.0) == 5.0
    assert census.wait_before(3, 45, lambda: 1.0) == 0.0
    assert census.worth_starting(14400) == 120 and census.worth_starting(60) == 15


def test_the_turn_is_asked_about_the_repository_with_the_history_beside_it(monkeypatch, tmp_path):
    seen = {}

    def fake(provider, model, prompt, repo, read_dirs, timeout):
        seen.update(locals())
        return Turn("timeout")

    repo, history = tmp_path / "repo", tmp_path / "history"
    monkeypatch.setattr(census, "ask", fake)
    census.collect(repo, history, provider=PROVIDERS[0], model="m1", timeout=7, now=lambda: 0.0)
    assert seen["repo"] == repo and seen["read_dirs"] == [history]
    assert seen["provider"] == PROVIDERS[0] and seen["model"] == "m1" and seen["timeout"] == 7
    assert str(history) in seen["prompt"] and "{history}" not in seen["prompt"]
    assert f"{history}/HISTORY-OVERVIEW.md" in seen["prompt"]
    assert f"{history}/commit-diffs/" in seen["prompt"]
    assert seen["prompt"] == census.prompt(history)


def test_the_prompt_forbids_names_and_asks_for_every_category():
    text = census.prompt()
    for name in census.CATEGORIES:
        assert name in text
    lowered = " ".join(text.lower().split())
    assert "one plain-english sentence each" in lowered
    assert "no file names" in lowered and "no commit hashes" in lowered


def _flat_prompt() -> str:
    return " ".join(census.prompt().lower().split())


def test_the_prompt_sets_a_bar_and_says_how_few_clear_it():
    text = _flat_prompt()
    for part in ("interesting", "nuanced", "complex", "large", "long-horizon"):
        assert part in text, part
    assert "no cap and no round number" in text and "one-line fix" in text
    assert "a handful of these, not dozens" in text and "padding" in text
    assert "returns an empty list" in text and "disqualify" in text


def test_the_prompt_says_how_to_read_and_for_how_long():
    text = _flat_prompt()
    assert "no shell" in text and "inventory only" in text and "25-40 minutes" in text
    assert "densest source files" in text


def test_the_prompt_carries_every_mechanical_rule_and_what_breaking_one_costs():
    text = _flat_prompt()
    assert f"at most {MAX_SENTENCE_WORDS} words" in text
    for rule in (
        "lower case",
        "acronyms",
        "all-capital",
        "quotation marks",
        "backticks",
        "parentheses",
        "brackets",
        "braces",
        "slashes",
        "equals signs",
        "dotted.names",
        "underscore",
        "capital in the middle",
    ):
        assert rule in text, rule
    assert "discarded whole" in text and "out of the count" in text


def _examples(label: str) -> list[str]:
    found = re.findall(rf'^\s*{label}\s+"([^"]+)"', census.prompt(), flags=re.M | re.S)
    return [" ".join(example.split()) for example in found]


def test_the_prompts_own_examples_are_judged_the_way_it_says_they_will_be():
    good, bad = _examples("GOOD"), _examples("BAD")
    assert len(good) >= 3 and len(bad) >= 4
    for example in good:
        assert census._clean([example])[0] == [example], example
    for example in bad:
        assert census._clean([example])[0] == [], example


def test_the_prompt_asks_for_themes_and_a_scale_with_both_ends_named():
    text = _flat_prompt()
    assert "two to five short phrases" in text and "never name a product or a company" in text
    assert "4 = a standalone application" in text and "0 = one slice" in text


def test_the_prompt_is_not_a_short_one():
    # The instruction is most of what decides how much comes back. One this long cannot be
    # cut down to a paragraph by accident.
    assert len(census.prompt().split()) > 1500


def test_the_failure_sentences_are_the_expected_ones():
    block = census.unscored(PROVIDERS[0], "m1", "timeout", returncode=None, seconds=90.7)
    assert block["error"] == (
        "census failed (timeout): the census exceeded its time budget; timed out after 90s; "
        "1 attempt(s); criteria unscored"
    )
    assert block["logic_depth_unavailable_reason"] == (
        "material census unavailable: the census exceeded its time budget; the lane was capped "
        "at 90 seconds and every material measurement is reported unmeasured rather than "
        "scored low"
    )
    assert census.unscored(PROVIDERS[0], "m1", "crashed", returncode=-9)["error"] == (
        f"census failed (crashed): the CLI died before producing a result; {PROVIDERS[0]} "
        "exited -9; 1 attempt(s); criteria unscored"
    )


def test_a_lane_that_never_took_its_turn_is_null_everywhere_and_says_why():
    block = census.no_turn(PROVIDERS[0], "m1", "nothing_to_read")
    assert set(kind for kind, _ in census.NO_TURN.values()) <= set(census.FAILURE_KINDS)
    assert block["census_failure_kind"] == "unknown" and block["census_attempts"] == 0
    assert census.NO_TURN["nothing_to_read"][1] in block["error"]
    assert block["scored"] is False and block["ok"] is False and block["timed_out"] is False
    assert block["logic_depth"] is None and block["n_minable_ideas"] is None
    assert block["provider"] == PROVIDERS[0] and block["model"] == "m1"


def test_every_block_scored_or_not_names_the_probe_that_made_it():
    assert census.score({})["probe"] == census.PROBE == "material_census"
    assert census.unscored(PROVIDERS[0], None, "timeout")["probe"] == census.PROBE


def test_what_could_not_be_assessed_is_tidied_and_cut_but_not_judged_here():
    out = census.score({"what_i_could_not_assess": "  the vendored\n parsers   were skipped "})
    assert out["what_i_could_not_assess"] == "the vendored parsers were skipped"
    long = census.score({"what_i_could_not_assess": "word " * 100})["what_i_could_not_assess"]
    assert len(long) == census.COULD_NOT_ASSESS_CHARS == 300
    assert census.score({})["what_i_could_not_assess"] == ""
    assert census.score({"what_i_could_not_assess": None})["what_i_could_not_assess"] == ""


def test_the_masking_is_named_for_a_sentence_that_was_kept():
    out = census.score({"complex_logic": ["a `retry` loop with a ceiling on how long it waits"]})
    assert out["minable_ideas"]["complex_logic"] == [
        "a retry loop with a ceiling on how long it waits"
    ]
    assert out["redacted_from_examples"] == ["code punctuation"]


def test_a_sentence_that_was_dropped_names_nothing():
    out = census.score({"complex_logic": [NAMES_A_FILE, GOOD]})
    assert out["n_complex_logic"] == 1 and out["redacted_from_examples"] == []


def test_the_summary_names_what_was_masked_in_it_and_the_themes_do_not():
    assert census.score({"themes": ["lib/pricing/engine.rb"]})["redacted_from_examples"] == []
    out = census.score({"material_summary": "Most of it lives in app/services/fare_engine.py."})
    # the path takes the file name and the identifier inside it along with it
    assert out["redacted_from_examples"] == ["a filesystem path"]


def test_what_could_not_be_assessed_names_nothing_because_it_is_not_masked():
    out = census.score({"what_i_could_not_assess": "could not open src/auth/session.py"})
    assert out["redacted_from_examples"] == []


@pytest.mark.parametrize(
    ("summary", "label"),
    [
        ("Most of it is in session.py today.", "a filename"),
        ("The fix arrived in 9f3c2ab7d1e4 much later.", "an object hash"),
        ("Most of it is in fare_engine and the rest is small.", "a snake_case identifier"),
        ("Most of it is in fareEngine and the rest is small.", "a camelCase identifier"),
        ("The PaymentReconciler does most of it.", "a class name"),
        ("Acme", "a proper name"),
    ],
)
def test_each_kind_of_masking_is_named_in_the_closed_vocabulary(summary, label):
    assert census.score({"material_summary": summary})["redacted_from_examples"] == [label]


@pytest.mark.parametrize(
    "summary",
    [
        "Ask priya.raman@acmecorp.example about it.",
        "It is described at https://wiki.acmecorp.example today.",
        'It prints "hello world" a great deal.',
        "Split app.services.engine in two.",
        "The MAX layer bounds it.",
    ],
)
def test_shapes_the_masking_has_no_label_for_are_left_to_the_boundary(summary):
    from hazina_review import emit

    out = census.score({"material_summary": summary})
    assert out["redacted_from_examples"] == []
    assert emit.material_block({"material_summary": out["material_summary"]}) == {
        "material_summary": ""
    }


def test_the_labels_are_sorted_and_said_once():
    out = census.score(
        {
            "complex_logic": ["a `retry` loop with a ceiling", "a `backoff` loop with a floor"],
            "material_summary": "Most of it is in session.py and fare_engine today.",
        }
    )
    assert out["redacted_from_examples"] == [
        "a filename",
        "a snake_case identifier",
        "code punctuation",
    ]


def test_nothing_is_named_unless_the_masking_changed_the_text():
    for line in (GOOD, "A billing system with dense pricing rules. It uses Python and Django."):
        assert redact(line) == (line, [])
        assert census.score({"material_summary": line})["redacted_from_examples"] == []


def test_every_label_the_lane_can_name_is_in_the_vocabulary():
    assert len(census.REMOVED_KINDS) == len(set(census.REMOVED_KINDS)) == 12


def test_the_prompt_asks_for_the_keys_in_the_order_the_block_is_read_in():
    shape = census.prompt().split("Return ONLY", 1)[1]
    assert re.findall(r'"(\w+)":', shape) == [
        "themes",
        "self_contained",
        "complex_logic",
        "substantial_features",
        "defect_repairs",
        "defect_repairs_with_regression_test",
        "work_units",
        "net_new_capabilities",
        "repo_evolutions",
        "performance_work",
        "hardening_work",
        "integration_work",
        "material_summary",
        "what_i_could_not_assess",
    ]


@pytest.mark.parametrize(
    "sentence",
    [
        'a loop that prints "hello world" until a ceiling is reached',
        "a loop that prints 'hello world' until a ceiling is reached",
        "a playlist rule for rock 'n' roll tagged items",
        "the user's 'draft' state is kept until publish",
        'a loop printing "x" inside a `retry` wrapper',
        "one two three four five six seven eight nine ten " * 3 + 'one two "three four" five',
    ],
)
def test_a_sentence_the_masking_took_words_out_of_is_dropped_whole(sentence):
    """What is left reads well enough, and it is not what the model wrote."""
    out = census.score({"complex_logic": [sentence, GOOD]})
    assert out["minable_ideas"]["complex_logic"] == [GOOD]
    assert out["redacted_from_examples"] == []


@pytest.mark.parametrize(
    "sentence",
    [
        "import pipeline that resumes after a partial failure",
        "import duties are computed per jurisdiction and summed per order",
        "from settings import defaults before each run",
        "class loading: a staged approach with a fallback",
        "def lookup: a cached approach",
    ],
)
def test_a_sentence_that_opens_the_way_a_line_of_code_does_is_dropped(sentence):
    assert census.score({"work_units": [sentence, GOOD]})["minable_ideas"]["work_units"] == [GOOD]


@pytest.mark.parametrize(
    "sentence",
    [
        "Import duties are computed per jurisdiction and summed per order",
        "imports of supplier rates are retried nightly",
        "import, export and audit share one queue",
        "a step to import rates from a supplier feed nightly",
        "from the queue a worker claims one job at a time",
        "class loading is staged with a fallback",
        "class of service rules: a tiered approach",
        "a `import` step that resumes after a partial failure",
    ],
)
def test_a_sentence_that_only_starts_with_such_a_word_is_kept(sentence):
    assert census.score({"work_units": [sentence]})["n_work_units"] == 1


def _apart(gap: int) -> str:
    """Two contractions with `gap` characters between their apostrophes."""
    between = "t " + "z" * (gap - 10) + " and isn"
    assert len(between) == gap
    return f"it doesn'{between}'t"


def test_two_straight_apostrophes_close_together_read_as_a_quotation_and_the_sentence_goes():
    for sentence in (
        "a merge of the users' saved filters with the admins' defaults at sign in",
        "a retry that doesn't give up when the queue isn't ready",
        _apart(80),
    ):
        assert census.score({"work_units": [sentence]})["n_work_units"] == 0, sentence
    for sentence in ("a retry that doesn't give up early", _apart(81), "an empty '' marker"):
        assert census.score({"work_units": [sentence]})["n_work_units"] == 1, sentence


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"error": "refused"},
        {"complex_logic": []},
        {**{name: [] for name in census.CATEGORIES}, "self_contained": "unknown"},
        {**{name: [] for name in census.CATEGORIES}, "self_contained": True},
    ],
)
def test_any_object_is_scored_whatever_it_holds(monkeypatch, tmp_path, raw):
    block, answer, _ = _collect(tmp_path, _answers(raw), monkeypatch)
    assert block["scored"] is True and block["n_complex_logic"] == 0
    assert answer == raw


def test_an_answer_that_is_not_an_object_is_no_answer(monkeypatch, tmp_path):
    block, answer, _ = _collect(tmp_path, _answers([]), monkeypatch)
    assert block["scored"] is False and block["census_failure_kind"] == "no_json"
    assert answer is None


def test_explicit_empty_assessment_is_measured(monkeypatch, tmp_path):
    raw = {**{name: [] for name in census.CATEGORIES}, "self_contained": 0}
    block, _, _ = _collect(tmp_path, _answers(raw), monkeypatch)
    assert block["scored"] is True and block["n_minable_ideas"] == 0
