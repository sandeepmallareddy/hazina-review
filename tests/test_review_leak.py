"""The promise the review rests on: nothing path-shaped or name-shaped leaves the machine.

Every block here is built the way the lane builds one, scored and then passed through the
emission boundary, and what is inspected is the text that would be written.
"""

import json
import re

import pytest

from hazina_review import emit
from hazina_review.lanes import census
from hazina_review.providers.registry import PROVIDERS

_PATHISH = re.compile(r"[\w.-]+/[\w./-]+|\b\w+\.(?:py|js|ts|go|rs|java|rb|cs|php|kt|swift)\b")

CLEAN = "add cursor pagination to a list endpoint backed by a relational store"

#: One hostile line per way a model can name what it read, with the fragment that would
#: identify the client if it got out.
LEAKS = {
    "posix path": ("rewrite src/auth/session.py to use rotating tokens", "session"),
    "class name": ("the PaymentReconciler class retries on a stale ledger lock", "Reconciler"),
    "client name": ("port the Acme billing adapter to the new queue", "Acme"),
    "windows path": (
        r"move C:\Users\dev\billing\ledger_sync.cs behind the new queue",
        "ledger_sync",
    ),
    "relative windows path": (
        r"the retry loop in services\billing\invoice_writer needs a ceiling",
        "invoice_writer",
    ),
    "dotted module path": (
        "split app.services.fare_engine into a pricing core and an adapter",
        "fare_engine",
    ),
    "function call": (
        "make compute_surge_multiplier() return a bounded value",
        "compute_surge",
    ),
    "email address": (
        "ask priya.raman@acmecorp.example about the ledger rounding rule",
        "priya",
    ),
    "url": (
        "follow the retry policy described at https://wiki.acmecorp.example/billing/retries",
        "acmecorp",
    ),
    "short commit id": ("redo the rounding fix from commit 9f3c2ab with a test", "9f3c2ab"),
    "commit id": ("redo the rounding fix from commit 9f3c2ab7d1e4 with a test", "9f3c2ab"),
    "full commit id": (
        "revert 4b825dc642cb6eb9a060e54bf8d69288fbee4904 and reapply it behind a flag",
        "4b825dc",
    ),
}

HOSTILE = {
    "complex_logic": [*(line for line, _ in LEAKS.values()), CLEAN],
    "themes": ["lib/pricing/engine.rb", "pricing"],
    "material_summary": "Most of the logic lives in app/services/fare_engine.py.",
    "what_i_could_not_assess": "could not open vendor/parsers/grammar_tables.py",
}


def _emitted(payload) -> str:
    block = {
        **census.score(payload),
        "provider": PROVIDERS[0],
        "model": None,
        "scored": True,
        "ok": True,
    }
    return json.dumps(emit.material_block(block))


def test_nothing_path_shaped_survives_into_the_emitted_block():
    text = _emitted(HOSTILE)
    assert not _PATHISH.search(text), text


def test_the_one_clean_sentence_is_the_one_that_survives():
    out = census.score(HOSTILE)
    assert out["n_complex_logic"] == 1
    assert out["minable_ideas"]["complex_logic"] == [CLEAN]
    assert CLEAN in _emitted(HOSTILE)


@pytest.mark.parametrize("kind", LEAKS)
def test_no_identifying_fragment_leaves_by_any_free_text_field(kind):
    """The line is put everywhere the model writes freely: a category, the themes, the summary
    and what it says it could not assess. Its fragment may come out of none of them."""
    line, fragment = LEAKS[kind]
    text = _emitted(
        {
            "complex_logic": [line, CLEAN],
            "themes": [line],
            "material_summary": line,
            "what_i_could_not_assess": line,
        }
    )
    assert fragment not in text, text
    assert CLEAN in text


@pytest.mark.parametrize("kind", LEAKS)
def test_what_could_not_be_assessed_is_the_one_field_scoring_leaves_unmasked(kind):
    """So the boundary is all that stands between it and the document, and is tested alone."""
    line, fragment = LEAKS[kind]
    assert fragment in census.score({"what_i_could_not_assess": line})["what_i_could_not_assess"]
    assert fragment not in _emitted({"what_i_could_not_assess": line})
