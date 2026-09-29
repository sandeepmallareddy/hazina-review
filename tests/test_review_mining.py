import json

import pytest

from hazina_review import emit, sentences
from hazina_review.lanes import mining
from hazina_review.providers.ask import Turn
from hazina_review.providers.registry import PROVIDERS
from hazina_scan.schema import EmissionRefused

GOOD = "coordinate competing workers so an expired lease cannot publish stale results"


def task(**changes):
    return {
        "title": "worker lease",
        "task_type": "bug_repair",
        "summary": GOOD,
        "files": ["src/leases.py"],
        **changes,
    }


def test_counts_follow_types_while_named_descriptions_are_omitted():
    result = mining.score(
        [
            task(),
            task(task_type="net_new"),
            task(task_type="repo_evolution"),
            task(task_type="unknown", summary="change src/leases.py"),
        ]
    )
    assert result["total_candidates"] == 4
    assert all(result[f"n_{name}"] == 1 for name in mining.TASK_TYPES)
    assert len(result["mined_task_summaries"]) == 3
    assert "leases" not in json.dumps(result)


def test_tasks_past_the_ceiling_still_count_and_the_ceiling_is_reported():
    # as in a full run: the ceiling is asked for, not cut to
    result = mining.score([task()] * 4, n=2)
    assert result["total_candidates"] == result["n_bug_repair"] == 4
    assert len(result["mined_task_summaries"]) == 4
    assert result["mine_unavailable_reason"].startswith(
        "the agentic census reached its ceiling of 2 ideas, so this count is a floor"
    )


def test_an_empty_answer_is_a_measured_zero_and_says_so():
    result = mining.score([])
    assert result["total_candidates"] == 0 and result["scored"] is False
    assert "these zeros are measured, not a failed lane" in result["mine_unavailable_reason"]


@pytest.mark.parametrize(
    ("answer", "total"),
    [
        ({}, 1),
        (task(), 1),
        ([None, "broken", 7, []], 0),
        ([{}], 1),
        ([task(title="")], 1),
        ([task(summary=None)], 1),
        ([task(), "broken"], 1),
        ([task(files="a.py")], 1),
    ],
)
def test_any_object_is_a_task_and_nothing_else_is(answer, total):
    """No field is required. A lone object is one task; an entry that is not an object is
    not a task."""
    assert mining.score(answer)["total_candidates"] == total


def test_a_task_with_no_type_and_no_sentence_counts_as_agentic_with_nothing_sent():
    result = mining.score([{}])
    assert result["n_agentic"] == 1 and result["mined_task_summaries"] == []


def test_the_sentence_falls_back_to_why_it_is_interesting():
    result = mining.score([{"why_interesting": f"  {GOOD}  ", "strategy": "net_new"}])
    assert result["mined_task_summaries"] == [f"net_new: {GOOD}"]


def _answered(answer):
    return lambda *a, **k: Turn("exited", 0, json.dumps({"result": json.dumps(answer)}), "")


P = PROVIDERS[0]


@pytest.mark.parametrize(
    ("turn", "said"),
    [
        (Turn("timeout"), f"{P} agent timed out after 5s"),
        (Turn("missing", error=FileNotFoundError(2, "gone")), f"{P} CLI not found on PATH"),
        (
            Turn("unstartable", error=PermissionError(13, "Permission denied")),
            f"{P} agent could not run: [Errno 13] Permission denied",
        ),
        (
            Turn("exited", 0, json.dumps({"result": "no answer here"})),
            f"{P} agent completed, but could not parse JSON from the agent's output.",
        ),
        (Turn("exited", 2), f"{P} exited 2: "),
        (Turn("exited", 1, "", "  rate limit reached  "), f"{P} exited 1: rate limit reached"),
        (Turn("exited", 1, "", "x" * 400), f"{P} exited 1: {'x' * 300}"),
        (
            # a refusal on a clean exit is not read here: nothing parsed, and that is all
            Turn("exited", 0, json.dumps({"is_error": True, "result": "usage limit reached"})),
            f"{P} agent completed, but could not parse JSON from the agent's output.",
        ),
    ],
)
def test_failed_turns_have_null_counts_and_say_what_the_command_did(
    monkeypatch, tmp_path, turn, said
):
    assert mining.why_stopped(turn, P, 5) == said
    monkeypatch.setattr(mining, "ask", lambda *a, **k: turn)
    block, raw = mining.collect(tmp_path, tmp_path, provider=P, model="m", timeout=5)
    assert all(block[key] is None for key in mining.COUNT_KEYS)
    assert raw is None and block["scored"] is False
    assert block["mine_unavailable_reason"] == sentences.scrub(
        f"{said}; the lane did not finish, so every task-type count is reported unmeasured "
        "rather than as zero"
    )


def test_what_the_command_printed_is_masked_before_it_is_kept(monkeypatch, tmp_path):
    turn = Turn("exited", 1, "", "cannot read src/auth/session.py for someone@example.com")
    monkeypatch.setattr(mining, "ask", lambda *a, **k: turn)
    block, _ = mining.collect(tmp_path, tmp_path, provider=P, model="m", timeout=5)
    said = block["mine_unavailable_reason"]
    assert "session" not in said and "example" not in said and "[path]" in said


def test_a_task_whose_list_field_cannot_be_walked_stops_the_lane(monkeypatch, tmp_path):
    answer = [task(), task(files=7)]
    monkeypatch.setattr(mining, "ask", _answered(answer))
    block, raw = mining.collect(tmp_path, tmp_path, provider=P, model="m", timeout=5)
    assert block["total_candidates"] is None and raw == answer


def test_empty_array_and_empty_object_are_both_measured(monkeypatch, tmp_path):
    for answer, total in (([], 0), ({}, 1)):
        monkeypatch.setattr(mining, "ask", _answered(answer))
        block, raw = mining.collect(tmp_path, tmp_path, provider=P, model="m", timeout=5)
        assert block["total_candidates"] == total
        assert raw == answer


def test_an_answer_that_is_not_a_string_makes_the_envelope_one_task(monkeypatch, tmp_path):
    stdout = json.dumps({"type": "result", "result": [task(), task()]})
    monkeypatch.setattr(mining, "ask", lambda *a, **k: Turn("exited", 0, stdout, ""))
    block, _ = mining.collect(tmp_path, tmp_path, provider=P, model="m", timeout=5)
    assert block["total_candidates"] == 1 and block["n_agentic"] == 1


def test_emit_checks_tags_and_sentences_independently():
    with pytest.raises(EmissionRefused):
        emit.mining_block({"mined_task_summaries": ["secret: an ordinary sentence"]})
    out = emit.mining_block({"mined_task_summaries": ["bug_repair: change src/leases.py"]})
    assert not any(out["mined_task_summaries"])
    with pytest.raises(EmissionRefused):
        emit.mining_block({"files": ["src/leases.py"]})
    # the masking runs before the boundary, and a sentence it changed is refused there
    assert mining.score([task(summary="V2 retries back off on a jittered schedule")])[
        "mined_task_summaries"
    ] == ["bug_repair: [identifier] retries back off on a jittered schedule"]
    out = emit.mining_block(
        mining.score([task(summary="V2 retries back off on a jittered schedule")])
    )
    assert out["mined_task_summaries"] == []


def test_prompt_uses_prepared_history_and_requested_ceiling(monkeypatch, tmp_path):
    seen = {}

    def ask(provider, model, prompt, repo, dirs, timeout):
        seen.update(prompt=prompt, dirs=dirs, timeout=timeout)
        return _answered([task()])()

    monkeypatch.setattr(mining, "ask", ask)
    block, raw = mining.collect(
        tmp_path, tmp_path / "history", provider=PROVIDERS[0], model="m", timeout=12, n=17
    )
    assert block["total_candidates"] == 1 and raw[0]["files"] == ["src/leases.py"]
    assert "up to 17" in seen["prompt"] and "{n}" not in seen["prompt"]
    assert str(tmp_path / "history" / "HISTORY-OVERVIEW.md") in seen["prompt"]
    assert seen["prompt"] == mining.prompt(17, tmp_path / "history")
    assert seen["timeout"] == 12


@pytest.mark.parametrize(
    "kind",
    [
        "posix path",
        "class name",
        "client name",
        "windows path",
        "relative windows path",
        "dotted module path",
        "function call",
        "email address",
        "url",
        "short commit id",
        "commit id",
        "full commit id",
    ],
)
def test_mining_never_emits_identifying_descriptions(kind):
    from tests.test_review_leak import LEAKS

    line, fragment = LEAKS[kind]
    result = emit.mining_block(mining.score([task(summary=line), task()]))
    assert result["total_candidates"] == 2
    assert result["mined_task_summaries"] == [f"bug_repair: {GOOD}"]
    assert fragment not in json.dumps(result)


def test_default_ceiling_matches_a_full_run():
    assert mining.DEFAULT_N == 150
