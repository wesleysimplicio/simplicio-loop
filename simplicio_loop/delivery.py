from __future__ import annotations

import json
import hashlib
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from . import freshness

DELIVERY_SCHEMA = "simplicio.delivery-receipt/v1"
RECONCILIATION_SCHEMA = "simplicio.delivery-reconciliation/v1"
DELIVERY_ORDER = [
    "planned",
    "implemented",
    "verified",
    "pr-open",
    "merge-ready",
    "merged",
    "released",
    "deployed",
]


class DeliveryTargetError(ValueError):
    """Raised when --delivery receives a value outside DELIVERY_ORDER[1:].

    Friendly, fail-closed: carries the bad value and the allowed list so the
    CLI can print a short corrective message without a traceback.
    """

    def __init__(self, value: str):
        self.value = value
        self.allowed = list(DELIVERY_ORDER[1:])
        message = (
            f"unsupported delivery target: {value!r}. "
            f"accepted values: {', '.join(self.allowed)}"
        )
        super().__init__(message)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def source_fingerprint(payload: Dict[str, Any]) -> str:
    """Return a stable identity for one external delivery observation.

    The fingerprint deliberately excludes the receipt timestamp and is based on
    canonical JSON, so two runtimes produce the same identity for the same source
    state while any check/review/branch/release change invalidates old evidence.
    """
    canonical = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_delivery_target(value: str) -> str:
    target = (value or "").strip().lower()
    if target not in DELIVERY_ORDER[1:]:
        raise DeliveryTargetError(value)
    return target


def delivery_rank(value: str) -> int:
    try:
        return DELIVERY_ORDER.index((value or "").strip().lower())
    except ValueError:
        return -1


def delivery_satisfies(current_state: str, target: str) -> bool:
    return delivery_rank(current_state) >= delivery_rank(target) >= 0


def reconcile_delivery_observation(previous_receipt: Dict[str, Any] | None,
                                   current_receipt: Dict[str, Any], *,
                                   expected_previous_fingerprint: str = "") -> Dict[str, Any]:
    """Classify a fresh observation against the last delivery receipt.

    This local contract makes the race visible: a target that was previously ready
    but no longer validates is ``reopened``.  An optimistic worker can require the
    fingerprint it read; a concurrent writer then yields ``stale`` instead of an
    unsafe overwrite.  External GitHub/release queries remain caller responsibilities.
    """
    previous = previous_receipt or {}
    previous_fp = str(previous.get("source_fingerprint") or "")
    current_fp = str(current_receipt.get("source_fingerprint") or "")
    target = str(current_receipt.get("target") or previous.get("target") or "")
    previous_state = str(previous.get("current_state") or "planned")
    current_state = str(current_receipt.get("current_state") or "planned")
    prior_ready = bool(previous.get("ready")) and delivery_satisfies(previous_state, target)
    current_ready = bool(current_receipt.get("ready")) and delivery_satisfies(current_state, target)
    result: Dict[str, Any] = {
        "schema": RECONCILIATION_SCHEMA,
        "status": "observed",
        "reason_code": "fresh_observation",
        "previous_fingerprint": previous_fp,
        "current_fingerprint": current_fp,
        "previous_state": previous_state,
        "current_state": current_state,
        "target": target,
    }
    if expected_previous_fingerprint and expected_previous_fingerprint != previous_fp:
        result.update({"status": "stale", "reason_code": "previous_observation_changed"})
    elif previous_fp and previous_fp == current_fp:
        result.update({"status": "unchanged", "reason_code": "same_source_observation"})
    elif prior_ready and not current_ready:
        failed = next((gate for gate in current_receipt.get("gates", [])
                       if gate.get("status") == "fail"), {})
        result.update({
            "status": "reopened",
            "reason_code": failed.get("reason_code", "delivery_target_regressed"),
        })
    elif current_ready and not prior_ready:
        result.update({"status": "advanced", "reason_code": "delivery_target_met"})
    return result


def _gate(name: str, ok: bool, reason_code: str, detail: str) -> Dict[str, Any]:
    return {
        "name": name,
        "status": "pass" if ok else "fail",
        "reason_code": reason_code,
        "detail": detail,
    }


def _require(payload: Dict[str, Any], path: str):
    cur: Any = payload
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _freshness_check(receipt: Dict[str, Any], *, now: Optional[datetime] = None,
                     ttl_overrides: Optional[Mapping[str, int]] = None) -> Dict[str, Any]:
    """#290 invariant: "Freshness" -- any cached `source_checked_at` older than its class
    TTL must be treated as stale and re-queried before being trusted for a terminal
    transition. Mirrors the `source_fingerprint` legacy-migration precedent a few lines
    below: a receipt that never carried `source_checked_at` (pre-dates this policy) stays
    readable so historical/synthetic receipts are not retroactively broken, but any
    receipt that DOES carry the field (every receipt `build_delivery_receipt` produces
    today) is held to the real TTL.
    """
    checked_at = receipt.get("source_checked_at")
    if not checked_at:
        return _gate("delivery_freshness", True, "freshness_legacy_unbound",
                     "delivery receipt has no source_checked_at; freshness policy not enforced")
    current_state = (receipt.get("current_state") or "").strip().lower()
    gate = freshness.freshness_gate(checked_at, current_state, overrides=ttl_overrides, now=now)
    return _gate("delivery_freshness", gate["status"] == "pass", gate["reason_code"], gate["detail"])


def validate_delivery_receipt(receipt: Dict[str, Any], target: str = "", *,
                              now: Optional[datetime] = None,
                              ttl_overrides: Optional[Mapping[str, int]] = None) -> Dict[str, Any]:
    gates: List[Dict[str, Any]] = []
    if receipt.get("schema") != DELIVERY_SCHEMA:
        gates.append(_gate("delivery_schema", False, "delivery_schema_invalid", "unexpected delivery receipt schema"))
        return {"ok": False, "gates": gates}
    gates.append(_gate("delivery_schema", True, "delivery_schema_valid", "delivery receipt schema is valid"))
    current_state = (receipt.get("current_state") or "").strip().lower()
    source_kind = (receipt.get("source_kind") or "").strip().lower()
    if current_state not in DELIVERY_ORDER:
        gates.append(_gate("delivery_state", False, "delivery_state_invalid", f"unsupported delivery state {current_state!r}"))
        return {"ok": False, "gates": gates}
    gates.append(_gate("delivery_state", True, "delivery_state_valid", f"delivery state {current_state!r} is recognized"))
    payload = receipt.get("source_payload") or {}
    expected_fingerprint = source_fingerprint(payload)
    if "source_fingerprint" not in receipt:
        # Receipts written before v3.25.2 remain readable during migration. New
        # receipts always include the binding, so callers can distinguish legacy
        # evidence and request a fresh source re-query before higher targets.
        gates.append(_gate("delivery_source_identity", True, "source_identity_legacy_unbound",
                           "legacy delivery receipt has no source fingerprint; re-query before external delivery"))
    elif receipt.get("source_fingerprint") != expected_fingerprint:
        gates.append(_gate("delivery_source_identity", False, "source_fingerprint_mismatch",
                           "delivery receipt does not match the canonical source observation"))
        return {"ok": False, "gates": gates}
    else:
        gates.append(_gate("delivery_source_identity", True, "source_fingerprint_valid",
                           "delivery receipt is bound to its canonical source observation"))
    normalized_target = ""
    if target:
        normalized_target = normalize_delivery_target(target)
        if receipt.get("target") != normalized_target:
            gates.append(_gate("delivery_target", False, "delivery_target_mismatch",
                               f"delivery receipt target {receipt.get('target')!r} does not match manifest target {normalized_target!r}"))
            return {"ok": False, "gates": gates}
        if not delivery_satisfies(current_state, normalized_target):
            if normalized_target == "merge-ready" and current_state == "pr-open":
                checks = payload.get("checks") or {}
                reviews = payload.get("reviews") or {}
                branch = payload.get("branch") or {}
                if checks.get("green") is False:
                    gates.append(_gate("delivery_target", False, "checks_not_green",
                                       "merge-ready target regressed because checks are not green"))
                    return {"ok": False, "gates": gates}
                if int(reviews.get("open_threads", 0)) != 0:
                    gates.append(_gate("delivery_target", False, "review_threads_open",
                                       "merge-ready target regressed because review threads are still open"))
                    return {"ok": False, "gates": gates}
                if int(reviews.get("approvals", 0)) < 1:
                    gates.append(_gate("delivery_target", False, "approvals_missing",
                                       "merge-ready target regressed because approvals are missing"))
                    return {"ok": False, "gates": gates}
                if branch.get("up_to_date") is False:
                    gates.append(_gate("delivery_target", False, "branch_drift_open",
                                       "merge-ready target regressed because the head branch is behind base"))
                    return {"ok": False, "gates": gates}
            gates.append(_gate("delivery_target", False, "delivery_target_not_met",
                               f"delivery state {current_state!r} has not reached target {normalized_target!r}"))
            return {"ok": False, "gates": gates}
        gates.append(_gate("delivery_target", True, "delivery_target_met",
                           f"delivery state {current_state!r} satisfies target {normalized_target!r}"))
    required = {
        "implemented": [],
        "verified": ["evidence_receipt", "criteria_verified"],
        "pr-open": ["pr.url", "pr.head_sha", "pr.base_sha", "pr.evidence"],
        "merge-ready": ["pr.url", "pr.head_sha", "pr.base_sha", "checks.green",
                        "reviews.approvals", "reviews.open_threads", "branch.up_to_date"],
        "merged": ["pr.url", "merge.commit_sha", "merge.default_branch", "merge.merged_at",
                   "merge.commit_in_default_branch"],
        "released": ["release.tag", "release.assets", "release.checksums_verified",
                     "release.signatures_verified", "release.sbom_present", "install_smoke.passed"],
        "deployed": ["deployment.environment", "deployment.verified_at", "deployment.smoke.passed"],
    }
    missing = []
    for key in required.get(current_state, []):
        value = _require(payload, key)
        if value is None or value == "" or value == []:
            missing.append(key)
    if missing:
        gates.append(_gate("delivery_source", False, "delivery_source_incomplete",
                           "delivery source payload missing required field(s): " + ", ".join(missing)))
        return {"ok": False, "gates": gates}
    if current_state == "merge-ready":
        if not payload.get("checks", {}).get("green"):
            gates.append(_gate("delivery_source", False, "checks_not_green", "merge-ready requires green checks"))
            return {"ok": False, "gates": gates}
        if int(payload.get("reviews", {}).get("open_threads", 1)) != 0:
            gates.append(_gate("delivery_source", False, "review_threads_open", "merge-ready requires zero open review threads"))
            return {"ok": False, "gates": gates}
        if int(payload.get("reviews", {}).get("approvals", 0)) < 1:
            gates.append(_gate("delivery_source", False, "approvals_missing", "merge-ready requires at least one approval"))
            return {"ok": False, "gates": gates}
        if not payload.get("branch", {}).get("up_to_date"):
            gates.append(_gate("delivery_source", False, "branch_drift_open", "merge-ready requires head branch up to date with base"))
            return {"ok": False, "gates": gates}
    if current_state == "merged" and not payload.get("merge", {}).get("commit_in_default_branch"):
        gates.append(_gate("delivery_source", False, "merge_not_visible_on_default_branch",
                           "merged state requires merge commit visible on default branch"))
        return {"ok": False, "gates": gates}
    if current_state == "released":
        if not payload.get("release", {}).get("checksums_verified"):
            gates.append(_gate("delivery_source", False, "release_checksum_missing", "released state requires verified checksums"))
            return {"ok": False, "gates": gates}
        if not payload.get("release", {}).get("signatures_verified"):
            gates.append(_gate("delivery_source", False, "release_signature_missing", "released state requires verified signatures"))
            return {"ok": False, "gates": gates}
        if not payload.get("release", {}).get("sbom_present"):
            gates.append(_gate("delivery_source", False, "release_sbom_missing", "released state requires SBOM evidence"))
            return {"ok": False, "gates": gates}
        if not payload.get("install_smoke", {}).get("passed"):
            gates.append(_gate("delivery_source", False, "install_smoke_failed", "released state requires clean install smoke pass"))
            return {"ok": False, "gates": gates}
    if current_state == "deployed" and not payload.get("deployment", {}).get("smoke", {}).get("passed"):
        gates.append(_gate("delivery_source", False, "deployment_smoke_failed", "deployed state requires smoke verification"))
        return {"ok": False, "gates": gates}
    gates.append(_gate("delivery_source", True, "delivery_source_complete",
                       f"delivery source payload satisfies state {current_state!r}"))
    freshness_gate_result = _freshness_check(receipt, now=now, ttl_overrides=ttl_overrides)
    gates.append(freshness_gate_result)
    if freshness_gate_result["status"] != "pass":
        return {"ok": False, "gates": gates}
    return {"ok": True, "gates": gates}


def build_delivery_receipt(run_dir: str, target: str, current_state: str = "planned",
                           source_kind: str = "local", source_payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    root = Path(run_dir)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    target = normalize_delivery_target(target)
    current_state = (current_state or "planned").strip().lower()
    if current_state not in DELIVERY_ORDER:
        raise ValueError(f"unsupported delivery state: {current_state!r}")
    receipt = {
        "schema": DELIVERY_SCHEMA,
        "run_id": manifest.get("run_id"),
        "target": target,
        "current_state": current_state,
        "source_kind": source_kind,
        "source_checked_at": _now(),
        "source_fingerprint": source_fingerprint(source_payload or {}),
        "ready": delivery_satisfies(current_state, target),
        "source_payload": source_payload or {},
    }
    if source_kind == "local" and current_state == "verified":
        receipt["delivery_certificate"] = {
            "kind": "local-verified",
            "summary": "Local verification certificate generated from run evidence",
        }
    validation = validate_delivery_receipt(receipt, target=target)
    receipt["ready"] = validation["ok"]
    receipt["gates"] = validation["gates"]
    return receipt


def write_delivery_receipt(run_dir: str, payload: Dict[str, Any]) -> Path:
    path = Path(run_dir) / "delivery-receipt.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
