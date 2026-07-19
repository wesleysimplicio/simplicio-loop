"""`simplicio.task-intake/v1` envelope (#284, "full task-intake/v1 envelope" gap).

Issue #284 asks for a single, versioned, immutable, hash-bound contract that
proves — before any mutation — that the Loop actually read the full source,
holds a lease/fencing identity, and has a frozen understanding of the task
(scope in/out, delivery target, acceptance criteria with stable IDs, origin,
and observable verification) rather than an ad hoc dict assembled per caller.

This module is intentionally data-only and model-free, same discipline as
`planning_gate.py`/`plan_contract.py`: it *assembles* an envelope from
artifacts that already exist (the task contract, an optional GitHub source
snapshot, optional lease/fencing identity, optional scope/dependency/risk
text supplied by the caller) — it does not invent content for a field the
caller did not actually supply, and it never rewrites or drops an explicit
(`origin=source`) acceptance criterion.

Wiring: `scripts/planning_gate.py build` calls `build_task_intake()` and
persists the result as `task-intake.json` next to `planning-receipt.json`;
`simplicio_loop.planning_gate.build_planning_receipt()` accepts an optional
`intake` mapping and folds its hash into the receipt for traceability. Both
are strictly additive — a caller that never builds an intake envelope keeps
working exactly as before.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence

INTAKE_SCHEMA = "simplicio.task-intake/v1"
INTAKE_FILENAME = "task-intake.json"

DELIVERY_TARGETS = frozenset((
    "implemented", "verified", "pr-open", "merge-ready", "merged", "released", "deployed",
))


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_hash(obj: Any) -> str:
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def _acceptance_criteria_from_contract(contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Project every scenario in the frozen task contract into an AC row with a
    stable ID, its observable result, and origin=source — #284's rule that an
    explicit AC from the source can never be silently dropped or reworded."""
    acs: List[Dict[str, Any]] = []
    for task in contract.get("tasks") or []:
        task_id = str(task.get("id") or "")
        for scenario in task.get("scenarios") or []:
            sid = str(scenario.get("id") or "")
            if not sid:
                continue
            acs.append({
                "id": sid,
                "task_id": task_id,
                "text": str(scenario.get("title") or ""),
                "origin": "source",
                "state": "pending",
                "given": scenario.get("given") or [],
                "when": scenario.get("when") or [],
                "then": scenario.get("then") or [],
                "rule_ids": list(scenario.get("rule_refs") or []),
            })
    return acs


def lint_task_intake(intake: Mapping[str, Any]) -> Dict[str, Any]:
    """Fail-closed lint over an assembled intake envelope (#284: "AC vago ... deve
    falhar no lint" / "Contrato sem AC deve ser inválido"). Returns a structured
    verdict, never raises."""
    errors: List[str] = []
    acs = list(intake.get("acceptance_criteria") or [])
    if not acs:
        errors.append("no_acceptance_criteria")
    seen_ids: set[str] = set()
    for ac in acs:
        aid = str(ac.get("id") or "")
        if not aid:
            errors.append("acceptance_criterion_missing_id")
            continue
        if aid in seen_ids:
            errors.append(f"duplicate_acceptance_criterion_id:{aid}")
        seen_ids.add(aid)
        if not str(ac.get("text") or "").strip():
            errors.append(f"acceptance_criterion_missing_text:{aid}")
        if ac.get("origin") not in ("source", "derived"):
            errors.append(f"acceptance_criterion_missing_origin:{aid}")
    understanding = dict(intake.get("understanding") or {})
    if understanding.get("delivery_target") not in DELIVERY_TARGETS:
        errors.append("delivery_target_invalid")
    return {"valid": not errors, "errors": errors}


def build_task_intake(
    *,
    run_id: str,
    attempt: int,
    contract: Mapping[str, Any],
    plan_hash: str = "",
    agent_id: str = "",
    runtime: str = "",
    device_id: str = "",
    session_id: str = "",
    protocol: str = "",
    capabilities: Optional[Sequence[str]] = None,
    lease_id: str = "",
    fencing_token: str = "",
    lease_expires_at: str = "",
    source_snapshot: Optional[Mapping[str, Any]] = None,
    repo_state: Optional[Mapping[str, Any]] = None,
    scope_in: Optional[Sequence[str]] = None,
    scope_out: Optional[Sequence[str]] = None,
    dependencies: Optional[Sequence[str]] = None,
    risks: Optional[Sequence[str]] = None,
    open_questions: Optional[Sequence[str]] = None,
    delivery_target: str = "implemented",
    stop_conditions: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Assemble a `simplicio.task-intake/v1` envelope from artifacts already
    computed elsewhere in the pipeline (task contract, source snapshot, lease).

    Nothing here re-derives task_contract content: `acceptance_criteria` is a
    direct, lossless projection of the contract's scenarios (see
    `_acceptance_criteria_from_contract`), so an AC present in the frozen
    contract is always present here with the same ID and text.
    """
    source = dict((source_snapshot or {}).get("source") or {})
    task_contract_hash = str(contract.get("collection_hash") or content_hash(contract))
    acs = _acceptance_criteria_from_contract(contract)
    target = delivery_target if delivery_target in DELIVERY_TARGETS else "implemented"

    envelope: Dict[str, Any] = {
        "schema": INTAKE_SCHEMA,
        "identity": {
            "run_id": str(run_id or ""),
            "attempt": int(attempt or 0),
            "agent_id": str(agent_id or ""),
            "runtime": str(runtime or ""),
            "device_id": str(device_id or ""),
            "session_id": str(session_id or ""),
            "protocol": str(protocol or ""),
            "capabilities": list(capabilities or []),
            "lease_id": str(lease_id or ""),
            "fencing_token": str(fencing_token or ""),
            "lease_expires_at": str(lease_expires_at or ""),
        },
        "source": source,
        "repo": dict(repo_state or {}),
        "hashes": {
            "task_contract_hash": task_contract_hash,
            "plan_hash": str(plan_hash or ""),
            "source_snapshot_hash": str(source.get("snapshot_hash") or ""),
        },
        "understanding": {
            "delivery_target": target,
            "scope_in": list(scope_in or []),
            "scope_out": list(scope_out or []),
            "dependencies": list(dependencies or []),
            "risks": list(risks or []),
            "open_questions": list(open_questions or []),
            "stop_conditions": list(stop_conditions or []),
        },
        "acceptance_criteria": acs,
    }
    envelope["intake_hash"] = content_hash(envelope)
    return envelope


def intake_path(run_dir: Any) -> Any:
    from pathlib import Path
    return Path(run_dir) / INTAKE_FILENAME


__all__ = [
    "INTAKE_SCHEMA",
    "INTAKE_FILENAME",
    "DELIVERY_TARGETS",
    "content_hash",
    "build_task_intake",
    "lint_task_intake",
    "intake_path",
]
