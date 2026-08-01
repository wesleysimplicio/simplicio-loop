"""Canonical semantic comparator and hash-addressed conformance receipt."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

SCHEMA = "simplicio.loop-conformance/v1"


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def canonicalize(value: Any) -> Any:
    """Normalize observational fields while preserving causal semantics."""
    if isinstance(value, Mapping):
        ignored = {"pid", "process_id", "timestamp", "started_at", "finished_at", "temp_path"}
        return {str(key): canonicalize(item) for key, item in sorted(value.items()) if str(key) not in ignored}
    if isinstance(value, (list, tuple)):
        return [canonicalize(item) for item in value]
    return value


def compare(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> dict[str, Any]:
    left = canonicalize(expected)
    right = canonicalize(actual)
    return {"passed": left == right, "expected": left, "actual": right,
            "expected_hash": _hash(left), "actual_hash": _hash(right)}


def run_corpus(corpus: Mapping[str, Any], provider: Callable[[Mapping[str, Any]], Mapping[str, Any]],
               *, max_output_bytes: int = 1_048_576) -> dict[str, Any]:
    """Run a validated corpus with bounded, classified provider failures."""
    validate_corpus(corpus)
    if max_output_bytes <= 0:
        raise ValueError("max_output_bytes must be positive")
    cases = corpus["cases"]
    results = []
    for case in cases:
        case = dict(case)
        expected = case["expected"]
        try:
            raw = provider(case["input"])
            if not isinstance(raw, Mapping):
                raise TypeError("provider output must be an object")
            actual = dict(raw)
            encoded = json.dumps(actual, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if len(encoded) > max_output_bytes:
                actual = {"error": "provider output exceeded limit", "reason_code": "provider_output_oversized"}
        except TimeoutError as exc:
            actual = {"error": str(exc), "reason_code": "provider_timeout"}
        except Exception as exc:
            actual = {"error": str(exc), "reason_code": "provider_failed"}
        result = compare(expected, actual)
        result.update({"id": str(case.get("id") or ""), "family": str(case.get("family") or "")})
        results.append(result)
    receipt = {"schema": SCHEMA, "corpus_schema": corpus.get("schema"),
               "case_count": len(results), "passed": all(row["passed"] for row in results),
               "results": results}
    receipt["receipt_hash"] = _hash(receipt)
    return receipt


def load_corpus(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_corpus(payload)
    return payload


def validate_corpus(corpus: Mapping[str, Any]) -> None:
    """Reject ambiguous corpus data before a provider can consume it."""
    if corpus.get("schema") != SCHEMA:
        raise ValueError("unsupported conformance corpus schema")
    cases = corpus.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("conformance corpus cases must be a non-empty array")
    seen: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, Mapping):
            raise ValueError(f"conformance case {index} must be an object")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"conformance case {index} requires a non-empty id")
        if case_id in seen:
            raise ValueError(f"duplicate conformance case id: {case_id}")
        seen.add(case_id)
        if not isinstance(case.get("input"), Mapping) or not isinstance(case.get("expected"), Mapping):
            raise ValueError(f"conformance case {case_id} requires input and expected objects")


__all__ = ["SCHEMA", "canonicalize", "compare", "load_corpus", "run_corpus", "validate_corpus"]
