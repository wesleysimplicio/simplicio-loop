"""Evidence-gated, deterministic executor discovery and negotiation."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

DOCTOR_SCHEMA = "simplicio.ecosystem-doctor/v1"
RECEIPT_SCHEMA = "simplicio.executor-negotiation-receipt/v1"


class NegotiationError(ValueError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _strings(values: Sequence[Any]) -> list[str]:
    return sorted({str(value).strip() for value in values if str(value).strip()})


def _doctor_index(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    if report.get("schema") != DOCTOR_SCHEMA:
        raise NegotiationError("unsupported_doctor_schema")
    handshake = report.get("handshake") or {}
    record = handshake.get("record") or {}
    if not handshake.get("written") or not str(record.get("handshake_sha") or "").startswith("sha256:"):
        raise NegotiationError("doctor_handshake_evidence_missing")
    return {
        str(component.get("name")): component
        for component in report.get("components") or ()
        if component.get("name")
    }


def _evidence_for(
    component: Mapping[str, Any], capability: str
) -> Mapping[str, Any] | None:
    evidence = component.get("capability_evidence") or {}
    row = evidence.get(capability)
    if not isinstance(row, Mapping):
        return None
    if row.get("status") != "verified" or not row.get("evidence_ref"):
        return None
    return row


def discover(
    doctor_report: Mapping[str, Any],
    manifests: Sequence[Mapping[str, Any]],
    *,
    denied_executor_ids: Sequence[str] = (),
    denied_languages: Sequence[str] = (),
) -> dict[str, Any]:
    """Join manifests with observed doctor facts without probing or execution."""
    components = _doctor_index(doctor_report)
    denied_ids, denied_langs = set(denied_executor_ids), set(denied_languages)
    available, unavailable = [], []
    for source in sorted(manifests, key=lambda row: str(row.get("executor_id") or "")):
        row = dict(source)
        executor_id = str(row.get("executor_id") or "").strip()
        component_name = str(row.get("component") or "").strip()
        language = str(row.get("language") or "").lower()
        capabilities = _strings(row.get("capabilities") or ())
        reason = None
        component = components.get(component_name)
        evidence: dict[str, Any] = {}
        if not executor_id or not component_name or language not in {"python", "rust"}:
            reason = "manifest_identity_invalid"
        elif executor_id in denied_ids or language in denied_langs:
            reason = "policy_denied"
        elif component is None:
            reason = "doctor_component_missing"
        elif component.get("status") != "available":
            reason = f"doctor_{component.get('status') or 'unavailable'}"
        elif row.get("starts_model_or_provider"):
            reason = "provider_side_effect_forbidden"
        else:
            for capability in capabilities:
                proof = _evidence_for(component, capability)
                if proof is None:
                    reason = "capability_evidence_missing"
                    break
                evidence[capability] = dict(proof)
        normalized = {
            "executor_id": executor_id,
            "component": component_name,
            "language": language,
            "capabilities": capabilities,
            "compatibility": int(row.get("compatibility", 0)),
            "cost_rank": int(row.get("cost_rank", 100)),
            "offline": bool(row.get("offline")),
            "parity_contract_hash": row.get("parity_contract_hash"),
            "parity_status": row.get("parity_status"),
            "doctor_status": component.get("status") if component else "missing",
            "doctor_reason_code": component.get("reason_code") if component else None,
            "capability_evidence": evidence,
            "reason_code": reason,
        }
        (unavailable if reason else available).append(normalized)
    return {
        "available": available,
        "unavailable": unavailable,
        "side_effects": [],
        "model_provider_started": False,
    }


def _parity_verified(candidate: Mapping[str, Any], peers: Sequence[Mapping[str, Any]]) -> bool:
    contract = candidate.get("parity_contract_hash")
    if candidate.get("language") != "rust":
        return True
    if candidate.get("parity_status") != "verified" or not contract:
        return False
    return any(
        peer.get("language") == "python"
        and peer.get("parity_status") == "verified"
        and peer.get("parity_contract_hash") == contract
        and set(peer["capabilities"]) == set(candidate["capabilities"])
        for peer in peers
    )


def _rank_key(
    candidate: Mapping[str, Any], preferred_languages: Sequence[str]
) -> tuple[Any, ...]:
    preferences = list(preferred_languages)
    language_rank = (
        preferences.index(candidate["language"])
        if candidate["language"] in preferences else len(preferences)
    )
    return (
        -int(candidate["compatibility"]),
        language_rank,
        int(candidate["cost_rank"]),
        candidate["executor_id"],
    )


def negotiate_stage(
    doctor_report: Mapping[str, Any],
    manifests: Sequence[Mapping[str, Any]],
    requirement: Mapping[str, Any],
    *,
    policy: Mapping[str, Any] | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Select an executor or return BLOCKED; never starts the selected tool."""
    policy = dict(policy or {})
    discovery = discover(
        doctor_report,
        manifests,
        denied_executor_ids=policy.get("denied_executor_ids") or (),
        denied_languages=policy.get("denied_languages") or (),
    )
    stage_id = str(requirement.get("stage_id") or "").strip()
    primary = _strings(requirement.get("required_capabilities") or ())
    alternatives = [
        _strings(group) for group in requirement.get("alternatives") or ()
    ]
    if not stage_id or not primary:
        raise NegotiationError("stage_requirement_invalid")
    capability_options = [primary, *alternatives]
    considered: list[dict[str, Any]] = []
    selected = None
    selected_option = None
    for option_index, required in enumerate(capability_options):
        candidates = []
        for candidate in discovery["available"]:
            reason = None
            if not set(required).issubset(candidate["capabilities"]):
                reason = "stage_capability_mismatch"
            elif requirement.get("offline_required") and not candidate["offline"]:
                reason = "offline_requirement_not_met"
            elif int(candidate["cost_rank"]) > int(requirement.get("max_cost_rank", 100)):
                reason = "cost_policy_exceeded"
            elif not _parity_verified(candidate, discovery["available"]):
                reason = "rust_python_parity_unverified"
            if reason:
                considered.append({
                    "executor_id": candidate["executor_id"],
                    "option": option_index,
                    "status": "skipped",
                    "reason_code": reason,
                })
            else:
                candidates.append(candidate)
        if candidates:
            candidates.sort(
                key=lambda candidate: _rank_key(
                    candidate, requirement.get("preferred_languages") or ("rust", "python")
                )
            )
            selected, selected_option = candidates[0], option_index
            for candidate in candidates[1:]:
                considered.append({
                    "executor_id": candidate["executor_id"],
                    "option": option_index,
                    "status": "skipped",
                    "reason_code": "ranked_lower",
                })
            break
    fallback = {
        "used": bool(selected is not None and selected_option),
        "from_capabilities": primary,
        "to_capabilities": (
            capability_options[selected_option] if selected is not None else None
        ),
        "reason_code": (
            "primary_unavailable_explicit_fallback"
            if selected is not None and selected_option else None
        ),
    }
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "stage_id": stage_id,
        "status": "SELECTED" if selected else "BLOCKED",
        "reason_code": None if selected else "no_safe_executor",
        "dry_run": bool(dry_run),
        "required_capabilities": primary,
        "selected": selected,
        "skipped": sorted(
            considered, key=lambda row: (row["executor_id"], row["option"], row["reason_code"])
        ),
        "unavailable": discovery["unavailable"],
        "fallback": fallback,
        "doctor_handshake_sha": doctor_report["handshake"]["record"]["handshake_sha"],
        "model_provider_started": False,
        "execution_started": False,
    }
    receipt["receipt_hash"] = _hash(receipt)
    return receipt
