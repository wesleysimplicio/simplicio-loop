from __future__ import annotations

from pathlib import Path

from simplicio_loop.conformance import canonicalize, load_corpus, run_corpus
from simplicio_loop.engine_boundary import select_engine


def provider(payload):
    try:
        receipt = select_engine(payload.get("mode", "auto"), rust_probe=payload.get("rust_probe"), attempt_id="fixture")
        return {"selected_engine": receipt.selected_engine, "reason_code": receipt.reason_code}
    except Exception as exc:
        return {"error": str(exc)}


def test_canonical_comparator_ignores_observational_fields_only() -> None:
    assert canonicalize({"state": "done", "pid": 1}) == {"state": "done"}
    assert canonicalize({"state": "done"}) != canonicalize({"state": "blocked"})


def test_published_engine_selection_corpus_passes() -> None:
    corpus = load_corpus(Path(__file__).parents[1] / "contracts" / "loop-conformance" / "corpus-v1.json")
    receipt = run_corpus(corpus, provider)
    assert receipt["passed"] is True
    assert receipt["case_count"] == 3
    assert receipt["receipt_hash"].startswith("sha256:")
