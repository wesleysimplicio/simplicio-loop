from __future__ import annotations

from pathlib import Path

import pytest

from simplicio_loop.conformance import canonicalize, load_corpus, run_corpus, validate_corpus
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


def test_corpus_validation_rejects_duplicate_or_malformed_cases() -> None:
    valid = {"schema": "simplicio.loop-conformance/v1", "cases": [
        {"id": "one", "input": {}, "expected": {}},
    ]}
    validate_corpus(valid)
    duplicate = {"schema": valid["schema"], "cases": valid["cases"] * 2}
    with pytest.raises(ValueError, match="duplicate"):
        validate_corpus(duplicate)
    with pytest.raises(ValueError, match="non-empty array"):
        validate_corpus({"schema": valid["schema"], "cases": []})


def test_runner_classifies_timeout_crash_and_oversized_provider_output() -> None:
    corpus = {"schema": "simplicio.loop-conformance/v1", "cases": [
        {"id": "timeout", "input": {}, "expected": {
            "error": "deadline", "reason_code": "provider_timeout"}},
        {"id": "crash", "input": {}, "expected": {
            "error": "boom", "reason_code": "provider_failed"}},
        {"id": "large", "input": {}, "expected": {
            "error": "provider output exceeded limit", "reason_code": "provider_output_oversized"}},
    ]}

    def provider(payload):
        if payload.get("case") == "timeout":
            raise TimeoutError("deadline")
        if payload.get("case") == "crash":
            raise RuntimeError("boom")
        return {"payload": "x" * 100}

    for item, name in zip(corpus["cases"], ("timeout", "crash", "large")):
        item["input"]["case"] = name
    receipt = run_corpus(corpus, provider, max_output_bytes=32)
    assert receipt["passed"] is True
