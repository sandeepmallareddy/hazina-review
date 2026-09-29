"""The second CLI's real subprocess path, with a local fake command and no paid turn."""

import os

import pytest

from hazina_review import emit, run
from hazina_review.lanes import census
from hazina_review.providers import registry
from hazina_scan import orchestrator


@pytest.fixture(autouse=True)
def _a_working_sandbox(monkeypatch):
    # The fake command below cannot run a sandbox, and this machine's real one may not either;
    # whether the sandbox works is tested on its own in test_review_providers.
    monkeypatch.setattr(registry, "sandbox_reads", lambda executable, check: True)


def test_both_lanes_use_the_second_provider_and_validate_its_answers(
    monkeypatch, tmp_path, py_repo
):
    provider = registry.PROVIDERS[1]
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    executable = shim_dir / provider
    help_text = "\n".join(registry._known(provider).required)
    census_answer = {**{name: [] for name in census.CATEGORIES}, "self_contained": 2}
    mining_answer = [
        {
            "title": "local evidence",
            "task_type": "bug_repair",
            "summary": (
                "coordinate competing workers so an expired lease cannot publish stale results"
            ),
            "files": ["worker.py"],
        }
    ]
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"if '--help' in sys.argv:\n    print({help_text!r})\n"
        "else:\n"
        "    prompt = sys.stdin.read()\n"
        f"    answer = {census_answer!r} if 'complex_logic' in prompt else {mining_answer!r}\n"
        "    target = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
        "    target.write_text(json.dumps(answer))\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim_dir) + os.pathsep + os.environ["PATH"])
    registry.help_of.cache_clear()
    try:
        lanes = run._model_lanes(
            py_repo,
            orchestrator.LaneClock(label=""),
            orchestrator.Deadline(600),
            provider=provider,
            model="m1",
        )
    finally:
        registry.help_of.cache_clear()
    block, _ = lanes["census"]
    mined, _ = lanes["mining"]
    assert block["scored"] is True and mined["total_candidates"] == 1, (
        block.get("census_failure_kind"),
        mined.get("mine_unavailable_reason"),
    )
    emit.material_block(block)
    emit.mining_block(mined)
