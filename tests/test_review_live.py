"""Both real model lanes, end to end. Paid calls require the explicit `--live` option."""

import os

import pytest

from hazina_review import emit, run
from hazina_review.providers.registry import PROVIDERS
from hazina_scan import orchestrator


@pytest.mark.live
def test_real_model_lanes_return_blocks_the_boundaries_accept(py_repo):
    # Through the run's own seam and nothing beside it: the scratch copy, the history brief,
    # the checks on both, and then the turn, aimed at the copy and never at the fixture's
    # working tree. The one test that spends money proves the path a client's run takes.
    lanes = run._model_lanes(
        py_repo,
        orchestrator.LaneClock(label=""),
        orchestrator.Deadline(600),
        provider=PROVIDERS[0],
        model=None,
    )
    block, _answer = lanes["census"]
    mined, _ = lanes["mining"]
    assert mined["total_candidates"] is not None, mined.get("mine_unavailable_reason")
    assert block["scored"] is True, block.get("census_failure_kind")
    assert 0 <= block["logic_depth"] <= 6
    emit.material_block(block)
    emit.mining_block(mined)


@pytest.mark.live
def test_real_second_provider_lanes_return_valid_blocks(py_repo):
    model = os.environ.get("HAZINA_REVIEW_LIVE_SECOND_MODEL")
    if not model:
        pytest.skip("set HAZINA_REVIEW_LIVE_SECOND_MODEL to choose a model for the paid test")
    lanes = run._model_lanes(
        py_repo,
        orchestrator.LaneClock(label=""),
        orchestrator.Deadline(900),
        provider=PROVIDERS[1],
        model=model,
        mine_n=5,
    )
    block, census_answer = lanes["census"]
    mined, mining_answer = lanes["mining"]
    census_shape = (
        (type(census_answer).__name__, sorted(census_answer))
        if isinstance(census_answer, dict)
        else type(census_answer).__name__
    )
    mining_shape = (type(mining_answer).__name__, len(mining_answer or []))
    assert block["scored"] is True, (block.get("census_failure_kind"), census_shape)
    assert mined["total_candidates"] is not None, (
        mined.get("mine_unavailable_reason"),
        mining_shape,
    )
    emit.material_block(block)
    emit.mining_block(mined)


def test_a_run_that_did_not_ask_for_live_skips_every_live_test(request):
    if request.config.getoption("--live"):
        pytest.skip("this run asked for the live tests")
    live = [item for item in request.session.items if "live" in item.keywords]
    assert all(item.get_closest_marker("skip") for item in live)
