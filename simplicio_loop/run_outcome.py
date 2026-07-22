"""Public, fail-closed outcome contract for ``simplicio-loop run``."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "simplicio.run-outcome/v1"
EXIT_COMPLETE = 0
EXIT_BLOCKED = 20
EXIT_CANCELLED = 21
EXIT_PARTIAL = 22
EXIT_INVALID_RECEIPT = 23
EXIT_INFRASTRUCTURE_FAILURE = 24
MAX_RECEIPT_AGE_SECONDS = 86400


def _source_binding(manifest: Mapping[str, Any]) -> dict[str, str]:
    return {
        "kind": str(manifest.get("source_kind") or "local"),
        "identity": str(manifest.get("issue_ref") or manifest.get("source")
                        or manifest.get("task_path") or ""),
        "digest": str(manifest.get("source_snapshot_hash") or manifest.get("task_contract_hash")
                      or manifest.get("collection_hash") or manifest.get("diff_hash") or ""),
    }


def _receipt_age_seconds(receipt: Mapping[str, Any], now: datetime) -> float | None:
    raw = str(receipt.get("generated_at") or "")
    try:
        generated = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return (now - generated).total_seconds()
    except (TypeError, ValueError):
        return None


def resolve_run_outcome(status: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Resolve the only public terminal decision; extensions may consume, never author it."""
    manifest = status.get("manifest") if isinstance(status.get("manifest"), Mapping) else {}
    state = status.get("state") if isinstance(status.get("state"), Mapping) else {}
    run_id = str(manifest.get("run_id") or "")
    phase = str(state.get("phase") or "unknown").lower()
    source = _source_binding(manifest)
    path = Path(str(status.get("run_dir") or "")) / "completion-receipt.json"
    digest = None
    receipt: Mapping[str, Any] | None = None
    receipt_error = "completion_receipt_missing"
    try:
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        loaded = json.loads(raw)
        if isinstance(loaded, Mapping):
            receipt = loaded
            receipt_error = "completion_receipt_invalid"
    except (OSError, ValueError, TypeError):
        pass

    authorized = False
    oracle_verdict = str((receipt or {}).get("verdict") or "UNAVAILABLE")
    if receipt is not None:
        age = _receipt_age_seconds(receipt, now or datetime.now(timezone.utc))
        same_source = all(
            not source[key] or str(receipt.get("source_binding", {}).get(key) or "") == source[key]
            for key in ("kind", "identity", "digest")
        ) if isinstance(receipt.get("source_binding"), Mapping) else not any(source.values())
        if str(receipt.get("run_id") or "") != run_id:
            receipt_error = "completion_receipt_cross_run"
        elif age is None or age < -5 or age > MAX_RECEIPT_AGE_SECONDS:
            receipt_error = "completion_receipt_stale"
        elif not same_source:
            receipt_error = "completion_receipt_source_mismatch"
        elif not (receipt.get("ready") is True and oracle_verdict == "COMPLETE" and receipt.get("tag") == "MEASURED"):
            receipt_error = "oracle_not_authorized"
        else:
            authorized = True
            receipt_error = "completion_verified"

    if phase == "done" and authorized:
        outcome, code = "COMPLETE", EXIT_COMPLETE
    elif phase == "cancelled":
        outcome, code = "CANCELLED", EXIT_CANCELLED
    elif phase in {"partial", "awaiting_decision", "delivering", "verified"}:
        outcome, code = "PARTIAL", EXIT_PARTIAL
    elif phase == "blocked":
        outcome, code = "BLOCKED", EXIT_BLOCKED
    elif phase == "done" or receipt is not None:
        outcome, code = "INVALID_RECEIPT", EXIT_INVALID_RECEIPT
    else:
        outcome, code = "INFRASTRUCTURE_FAILURE", EXIT_INFRASTRUCTURE_FAILURE

    return {
        "schema": SCHEMA, "run_id": run_id, "phase": phase, "outcome": outcome,
        "exit_code": code, "source": source, "oracle": {"verdict": oracle_verdict, "authorized": authorized},
        "completion_receipt": {"path": str(path), "sha256": digest, "validation": receipt_error},
    }


def persist_run_outcome(status: Mapping[str, Any]) -> dict[str, Any]:
    outcome = resolve_run_outcome(status)
    raw_run_dir = str(status.get("run_dir") or "")
    run_dir = Path(raw_run_dir) if raw_run_dir else None
    if run_dir is not None and run_dir.is_dir():
        target = run_dir / "run-outcome.json"
        target.write_text(json.dumps(outcome, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return outcome
