"""Canonical ``simplicio.quality-matrix/v2`` reader and v1 migration.

The validator is deliberately dependency-free and closed: unknown keys are errors and
every terminal lane is enumerated.  Producers may use the packaged JSON Schema for
generation while the completion oracle and watcher share :func:`evaluate_v2`.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

SCHEMA = "simplicio.quality-matrix/v2"
STATUSES = frozenset({"PASS", "FAIL", "BLOCKED", "NOT_APPLICABLE"})
LANES = (
    "implementation", "static_quality", "unit_component", "integration_contract",
    "system_e2e", "regression_smoke_artifact", "negative_paths",
    "property_fuzz_mutation", "invariants", "concurrency_fault_repeatability",
    "security_supply_chain", "performance_load_stress_soak", "coverage",
    "compatibility_install_upgrade_migration", "observability_evidence_audit",
)
_TOP = {"schema", "identity", "lanes"}
_IDENTITY = {"run_id", "task_id", "attempt_id", "head_sha", "tree_hash", "diff_hash",
             "policy_hash", "config_hash", "produced_at"}
_LANE = {"status", "reason_code", "evidence", "metrics", "waiver"}
_EVIDENCE = {"uri", "sha256", "run_id", "attempt_id", "head_sha", "author", "auditor"}
_METRIC = {"name", "value", "unit", "sample_count", "source", "reason_code"}
_WAIVER = {"scope", "justification", "approver", "expires_at", "policy_hash"}


class QualityMatrixV2Error(ValueError):
    """A malformed or semantically unsafe v2 receipt."""


def schema_text() -> str:
    """Return the exact schema shipped in the installed wheel."""
    return (Path(__file__).parent / "_contracts/quality-matrix/v2/schema.json").read_text(encoding="utf-8")


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _closed(obj: Any, allowed: Iterable[str], path: str, errors: List[str]) -> bool:
    if not isinstance(obj, dict):
        errors.append(f"{path}: expected object")
        return False
    unknown = sorted(set(obj) - set(allowed))
    if unknown:
        errors.append(f"{path}: unknown fields: {', '.join(unknown)}")
    return True


def _nonempty(obj: Mapping[str, Any], names: Iterable[str], path: str, errors: List[str]) -> None:
    for name in names:
        if not isinstance(obj.get(name), str) or not obj[name].strip():
            errors.append(f"{path}.{name}: required non-empty string")


def validate_v2(receipt: Any, *, now: datetime | None = None) -> List[str]:
    """Return all structural and semantic errors; an empty list is conformant."""
    errors: List[str] = []
    if not _closed(receipt, _TOP, "$", errors):
        return errors
    if receipt.get("schema") != SCHEMA:
        errors.append("$.schema: expected simplicio.quality-matrix/v2")
    identity = receipt.get("identity")
    if _closed(identity, _IDENTITY, "$.identity", errors):
        _nonempty(identity, _IDENTITY, "$.identity", errors)
    lanes = receipt.get("lanes")
    if not isinstance(lanes, dict):
        errors.append("$.lanes: expected object")
        return errors
    missing, unknown = sorted(set(LANES) - set(lanes)), sorted(set(lanes) - set(LANES))
    if missing:
        errors.append("$.lanes: missing lanes: " + ", ".join(missing))
    if unknown:
        errors.append("$.lanes: unknown lanes: " + ", ".join(unknown))
    now = now or datetime.now(timezone.utc)
    for name in LANES:
        lane = lanes.get(name)
        path = f"$.lanes.{name}"
        if not _closed(lane, _LANE, path, errors):
            continue
        status = lane.get("status")
        if status not in STATUSES:
            errors.append(f"{path}.status: invalid terminal status")
        reason = lane.get("reason_code")
        if not isinstance(reason, str) or not reason:
            errors.append(f"{path}.reason_code: required")
        evidence = lane.get("evidence")
        if not isinstance(evidence, list):
            errors.append(f"{path}.evidence: expected array")
            evidence = []
        for i, ref in enumerate(evidence):
            ep = f"{path}.evidence[{i}]"
            if _closed(ref, _EVIDENCE, ep, errors):
                _nonempty(ref, _EVIDENCE, ep, errors)
                if ref.get("author") == ref.get("auditor"):
                    errors.append(f"{ep}: auditor must be independent")
                for key in ("run_id", "attempt_id", "head_sha"):
                    if isinstance(identity, dict) and ref.get(key) != identity.get(key):
                        errors.append(f"{ep}.{key}: stale binding")
                digest = ref.get("sha256", "")
                if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                    errors.append(f"{ep}.sha256: expected lowercase SHA-256")
        if status == "PASS" and not evidence:
            errors.append(f"{path}: PASS requires evidence")
        metrics = lane.get("metrics", [])
        if not isinstance(metrics, list):
            errors.append(f"{path}.metrics: expected array")
        else:
            for i, metric in enumerate(metrics):
                mp = f"{path}.metrics[{i}]"
                if not _closed(metric, _METRIC, mp, errors):
                    continue
                _nonempty(metric, ("name", "unit", "source"), mp, errors)
                value = metric.get("value")
                if value is None and not metric.get("reason_code"):
                    errors.append(f"{mp}: null value requires reason_code")
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                    errors.append(f"{mp}.value: expected number or null")
                if not isinstance(metric.get("sample_count"), int) or metric.get("sample_count", 0) < 0:
                    errors.append(f"{mp}.sample_count: expected non-negative integer")
        waiver = lane.get("waiver")
        if status == "NOT_APPLICABLE":
            if not _closed(waiver, _WAIVER, f"{path}.waiver", errors):
                continue
            _nonempty(waiver, _WAIVER, f"{path}.waiver", errors)
            if isinstance(identity, dict) and waiver.get("policy_hash") != identity.get("policy_hash"):
                errors.append(f"{path}.waiver.policy_hash: policy mismatch")
            if waiver.get("approver") in {identity.get("task_id") if isinstance(identity, dict) else None,
                                          identity.get("run_id") if isinstance(identity, dict) else None}:
                errors.append(f"{path}.waiver.approver: self-approval forbidden")
            try:
                expiry = datetime.fromisoformat(str(waiver.get("expires_at", "")).replace("Z", "+00:00"))
                if expiry <= now:
                    errors.append(f"{path}.waiver.expires_at: expired")
            except ValueError:
                errors.append(f"{path}.waiver.expires_at: invalid timestamp")
        elif waiver is not None:
            errors.append(f"{path}.waiver: allowed only for NOT_APPLICABLE")
        if status == "PASS" and reason.lower() in {"skipped", "skip", "xfail", "flaky"}:
            errors.append(f"{path}: {reason} cannot become PASS")
    return errors


def evaluate_v2(receipt: Any, **kwargs: Any) -> Dict[str, Any]:
    """Shared oracle/watcher terminal semantics: malformed or non-green always blocks."""
    errors = validate_v2(receipt, **kwargs)
    if errors:
        return {"schema": SCHEMA, "ready": False, "reason_code": "quality_matrix_v2_invalid", "errors": errors}
    blocked = [name for name in LANES if receipt["lanes"][name]["status"] in {"FAIL", "BLOCKED"}]
    return {"schema": SCHEMA, "ready": not blocked,
            "reason_code": "quality_matrix_verified" if not blocked else "quality_matrix_lanes_blocked",
            "blocked_lanes": blocked, "errors": []}


def migrate_v1(receipt: Mapping[str, Any]) -> Dict[str, Any]:
    """Project v1 deterministically; absent v1 guarantees remain BLOCKED, never PASS."""
    if receipt.get("schema") != "simplicio.quality-matrix/v1":
        raise QualityMatrixV2Error("expected simplicio.quality-matrix/v1")
    work = receipt.get("work_item") if isinstance(receipt.get("work_item"), dict) else {}
    identity = {key: str(value or "") for key, value in {
        "run_id": receipt.get("run_id"), "task_id": work.get("id"), "attempt_id": receipt.get("attempt_id"),
        "head_sha": work.get("head_sha"), "tree_hash": receipt.get("tree_hash"),
        "diff_hash": receipt.get("diff_hash"), "policy_hash": receipt.get("policy_hash"),
        "config_hash": receipt.get("config_hash"), "produced_at": receipt.get("produced_at"),
    }.items()}
    mapping = {"implementation": "implementation", "unit": "unit_component",
               "integration": "integration_contract", "system": "system_e2e",
               "regression": "regression_smoke_artifact", "benchmark": "performance_load_stress_soak"}
    lanes = {name: {"status": "BLOCKED", "reason_code": "not_represented_in_v1", "evidence": [], "metrics": []}
             for name in LANES}
    requirements = receipt.get("requirements") if isinstance(receipt.get("requirements"), dict) else {}
    for old, new in mapping.items():
        entry = requirements.get(old) if isinstance(requirements.get(old), dict) else {}
        old_status = str(entry.get("status", "")).lower()
        lanes[new] = {"status": "FAIL" if old_status == "fail" else "BLOCKED",
                      "reason_code": "v1_failure" if old_status == "fail" else "v1_pass_requires_v2_evidence",
                      "evidence": [], "metrics": []}
    measured = (receipt.get("coverage") or {}).get("measured") if isinstance(receipt.get("coverage"), dict) else None
    lanes["coverage"]["metrics"] = [{"name": "line", "value": measured, "unit": "percent",
                                        "sample_count": 1 if isinstance(measured, (int, float)) and not isinstance(measured, bool) else 0,
                                        "source": "v1.coverage.measured", "reason_code": None if measured is not None else "unavailable_in_v1"}]
    return {"schema": SCHEMA, "identity": identity, "lanes": lanes}


__all__ = ["SCHEMA", "STATUSES", "LANES", "QualityMatrixV2Error", "schema_text",
           "canonical_hash", "validate_v2", "evaluate_v2", "migrate_v1"]
