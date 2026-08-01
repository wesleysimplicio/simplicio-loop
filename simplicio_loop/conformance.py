"""Canonical semantic comparator and hash-addressed conformance receipt."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
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


def run_corpus(corpus: Mapping[str, Any], provider: Callable[[Mapping[str, Any]], Mapping[str, Any]]) -> dict[str, Any]:
    cases = corpus.get("cases") if isinstance(corpus.get("cases"), Sequence) else []
    results = []
    for case in cases:
        case = dict(case)
        expected = case.get("expected") if isinstance(case.get("expected"), Mapping) else {}
        try:
            actual = dict(provider(case.get("input") if isinstance(case.get("input"), Mapping) else {}))
        except Exception as exc:
            actual = {"error": str(exc)}
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
    if payload.get("schema") != SCHEMA:
        raise ValueError("unsupported conformance corpus schema")
    return payload


__all__ = ["SCHEMA", "canonicalize", "compare", "load_corpus", "run_corpus"]
