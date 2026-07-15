"""Task intake contract (#284).

Implements ``simplicio.task-intake/v1`` — the frozen, hash-bound envelope that
MUST exist (and be COMPLETE) before any mutation is allowed.

Design constraints:
- Model-free and data-only, like ``plan_contract.py`` and ``planning_gate.py``.
- Never invents evidence; every gate either passes (returns a typed dict) or
  raises an ``IntakeBlockedError`` with a structured reason code.
- AC IDs are stable identifiers (``AC-001``, ``AC-002`` …) preserved across
  replannings; they are never renumbered or merged silently.
- Ambiguity that affects business logic, security, data, or delivery MUST
  surface as ``AWAITING_DECISION`` — never resolved silently.

The public surface this module adds:

  ``compile_intake(…)``      Build a frozen intake envelope from raw source data.
  ``lint_intake(…)``         Structural + semantic validation; returns reason codes.
  ``freeze_intake(…)``       Hash the envelope; returns ``(envelope, sha256-hex)``.
  ``IntakeBlockedError``     Raised when a hard gate fails.
  ``INTAKE_SCHEMA``          The schema identifier.

See issue #284 for the full scope and acceptance criteria.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Mapping, Optional, Sequence

INTAKE_SCHEMA = "simplicio.task-intake/v1"
AC_ID_PREFIX = "AC-"

DELIVERY_TARGETS = {
    "implemented", "verified", "pr-open", "merge-ready", "merged",
    "released", "deployed",
}

IMPACT_CATEGORIES = [
    "code", "reverse_dependents", "public_contracts", "data_persistence",
    "security", "concurrency", "performance", "installation_docs", "tests",
]

VERDICT_COMPLETE = "COMPLETE"
VERDICT_BLOCKED = "BLOCKED"
VERDICT_AWAITING_DECISION = "AWAITING_DECISION"
VERDICT_STALE_SOURCE = "STALE_SOURCE"
VERDICT_LEASE_LOST = "LEASE_LOST"


class IntakeBlockedError(RuntimeError):
    """Raised when the intake fails a hard gate."""

    def __init__(self, reason_code: str, reason: str) -> None:
        super().__init__(f"[{reason_code}] {reason}")
        self.reason_code = reason_code
        self.reason = reason


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _stable_hash(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _norm(text: str) -> str:
    return " ".join((text or "").split())


# ---------------------------------------------------------------------------
# Acceptance-criteria helpers
# ---------------------------------------------------------------------------

def make_ac_id(index: int) -> str:
    """Stable AC identifier: ``AC-001``, ``AC-002`` …"""
    return f"{AC_ID_PREFIX}{index:03d}"


def normalize_ac(
    raw: Mapping[str, Any],
    index: int,
    *,
    origin: str = "source",
) -> Dict[str, Any]:
    """Normalise a raw AC dict into the canonical shape.

    ``origin`` must be ``'source'`` (extracted from the source issue/ticket)
    or ``'derived'`` (added by the agent to cover an obvious gap).  The caller
    is responsible for tagging derived ACs.
    """
    text = _norm(str(raw.get("text") or raw.get("description") or ""))
    ac_id = str(raw.get("id") or make_ac_id(index))
    if not text:
        raise IntakeBlockedError(
            "ac_text_empty",
            f"AC {ac_id!r} has no observable text; a vague AC blocks intake.",
        )
    verification = _norm(str(raw.get("verification") or ""))
    if not verification:
        raise IntakeBlockedError(
            "ac_verification_missing",
            f"AC {ac_id!r} has no reproducible verification method.",
        )
    return {
        "id": ac_id,
        "text": text,
        "origin": str(raw.get("origin") or origin),
        "source_ref": _norm(str(raw.get("source_ref") or "")),
        "verification": verification,
        "evidence_type": _norm(str(raw.get("evidence_type") or "test_output")),
        "preconditions": [_norm(str(p)) for p in (raw.get("preconditions") or []) if str(p).strip()],
        "positive_cases": [_norm(str(c)) for c in (raw.get("positive_cases") or []) if str(c).strip()],
        "negative_cases": [_norm(str(c)) for c in (raw.get("negative_cases") or []) if str(c).strip()],
        "edge_cases": [_norm(str(c)) for c in (raw.get("edge_cases") or []) if str(c).strip()],
        "step_ids": [str(s) for s in (raw.get("step_ids") or []) if str(s).strip()],
        "status": "pending",
    }


# ---------------------------------------------------------------------------
# Impact map helpers
# ---------------------------------------------------------------------------

def normalize_impact_entry(category: str, raw: Any) -> Dict[str, Any]:
    """Normalise one impact-map category entry.

    ``raw`` may be ``'not_applicable'``, a justification string, or a mapping
    with ``status`` and optional ``items``/``justification``.  Every category
    MUST be present — missing categories block the lint gate.
    """
    if raw is None:
        raise IntakeBlockedError(
            f"impact_category_missing:{category}",
            f"Impact category '{category}' is missing (use 'not_applicable' with justification).",
        )
    if isinstance(raw, str):
        if _norm(raw).lower() == "not_applicable":
            raise IntakeBlockedError(
                f"impact_not_applicable_no_justification:{category}",
                f"Impact category '{category}' is not_applicable but has no justification.",
            )
        # A plain string is treated as a justification for not_applicable.
        return {"category": category, "status": "not_applicable", "justification": _norm(raw)}
    if isinstance(raw, Mapping):
        status = _norm(str(raw.get("status") or "confirmed"))
        justification = _norm(str(raw.get("justification") or ""))
        if status == "not_applicable" and not justification:
            raise IntakeBlockedError(
                f"impact_not_applicable_no_justification:{category}",
                f"Impact category '{category}' is not_applicable but has no justification.",
            )
        return {
            "category": category,
            "status": status,
            "items": list(raw.get("items") or []),
            "justification": justification,
        }
    raise IntakeBlockedError(
        f"impact_category_invalid_type:{category}",
        f"Impact category '{category}' must be a string or mapping, got {type(raw).__name__}.",
    )


# ---------------------------------------------------------------------------
# Source snapshot
# ---------------------------------------------------------------------------

def make_source_snapshot(
    *,
    provider: str,
    repo: str,
    item_id: str,
    url: str = "",
    revision: str,
    snapshot_hash: str,
    observed_at: str = "",
    title_hash: str = "",
    body_hash: str = "",
    comments_hash: str = "",
) -> Dict[str, Any]:
    """Build a frozen source-snapshot dict.

    ``revision`` is the canonical immutable identifier of the source state
    (e.g. GitHub issue ``updated_at`` timestamp, commit SHA, or a composite
    hash of title + body + labels + all comment IDs).  The runner MUST re-query
    the source and compare ``revision`` before emitting a mutation authority; a
    changed revision invalidates the authority (``STALE_SOURCE``).
    """
    return {
        "provider": _norm(str(provider or "")),
        "repo": _norm(str(repo or "")),
        "item_id": _norm(str(item_id or "")),
        "url": _norm(str(url or "")),
        "revision": _norm(str(revision or "")),
        "snapshot_hash": _norm(str(snapshot_hash or "")),
        "observed_at": _norm(str(observed_at or _now_iso())),
        "title_hash": _norm(str(title_hash or "")),
        "body_hash": _norm(str(body_hash or "")),
        "comments_hash": _norm(str(comments_hash or "")),
    }


# ---------------------------------------------------------------------------
# compile_intake
# ---------------------------------------------------------------------------

def compile_intake(
    *,
    run_id: str,
    work_item_id: str,
    attempt_id: str,
    agent_id: str = "",
    source_snapshot: Mapping[str, Any],
    repo_head: str = "",
    repo_tree_hash: str = "",
    repo_branch: str = "",
    title: str,
    objective: str,
    current_state: str = "",
    desired_state: str = "",
    delivery_target: str = "verified",
    scope_in: Sequence[str] = (),
    scope_out: Sequence[str] = (),
    constraints: Sequence[str] = (),
    dependencies: Sequence[str] = (),
    risks: Sequence[str] = (),
    open_questions: Sequence[str] = (),
    rollback_plan: str = "",
    stop_conditions: Sequence[str] = (),
    acceptance_criteria: Sequence[Mapping[str, Any]] = (),
    impact_map: Mapping[str, Any] | None = None,
    lease_id: str = "",
    fencing_token: str = "",
    observed_at: str = "",
) -> Dict[str, Any]:
    """Build a frozen ``simplicio.task-intake/v1`` envelope.

    Returns the *un-hashed* envelope dict.  Call ``freeze_intake`` next to
    attach the canonical hash before persisting.

    Hard gates (raise ``IntakeBlockedError``):
    - ``objective`` empty
    - ``delivery_target`` not in ``DELIVERY_TARGETS``
    - any AC missing text or verification
    - any AC with ``origin=source`` has no ``source_ref``
    - any impact-map category missing or ``not_applicable`` without justification
    - ``scope_in`` AND ``scope_out`` both empty (at least one must be explicit)
    """
    if not _norm(objective):
        raise IntakeBlockedError("objective_empty", "objective must not be empty")
    if delivery_target not in DELIVERY_TARGETS:
        raise IntakeBlockedError(
            "delivery_target_invalid",
            f"delivery_target={delivery_target!r} is not one of {sorted(DELIVERY_TARGETS)}",
        )

    # Normalise ACs
    acs_raw = list(acceptance_criteria)
    if not acs_raw:
        raise IntakeBlockedError("no_acceptance_criteria", "at least one AC is required")
    normalised_acs: List[Dict[str, Any]] = []
    for i, raw_ac in enumerate(acs_raw, start=1):
        ac = normalize_ac(raw_ac, i)
        if ac["origin"] == "source" and not ac["source_ref"]:
            raise IntakeBlockedError(
                "source_ac_missing_ref",
                f"AC {ac['id']!r} has origin=source but no source_ref (line/section/comment).",
            )
        normalised_acs.append(ac)

    # Normalise impact map
    normalised_impact: Dict[str, Any] = {}
    raw_impact = dict(impact_map or {})
    for cat in IMPACT_CATEGORIES:
        raw_entry = raw_impact.get(cat)
        normalised_impact[cat] = normalize_impact_entry(cat, raw_entry)

    # Scope check
    scope_in_list = [_norm(s) for s in scope_in if _norm(s)]
    scope_out_list = [_norm(s) for s in scope_out if _norm(s)]
    if not scope_in_list and not scope_out_list:
        raise IntakeBlockedError(
            "scope_both_empty",
            "scope_in and scope_out cannot both be empty; at least one must be explicit.",
        )

    return {
        "schema": INTAKE_SCHEMA,
        "run_id": _norm(str(run_id or "")),
        "work_item_id": _norm(str(work_item_id or "")),
        "attempt_id": _norm(str(attempt_id or "")),
        "agent_id": _norm(str(agent_id or "")),
        "lease_id": _norm(str(lease_id or "")),
        "fencing_token": _norm(str(fencing_token or "")),
        "source_snapshot": dict(source_snapshot),
        "repo": {
            "head": _norm(str(repo_head or "")),
            "tree_hash": _norm(str(repo_tree_hash or "")),
            "branch": _norm(str(repo_branch or "")),
        },
        "task": {
            "title": _norm(str(title or "")),
            "objective": _norm(str(objective or "")),
            "current_state": _norm(str(current_state or "")),
            "desired_state": _norm(str(desired_state or "")),
            "delivery_target": delivery_target,
            "scope_in": scope_in_list,
            "scope_out": scope_out_list,
            "constraints": [_norm(s) for s in constraints if _norm(s)],
            "dependencies": [_norm(s) for s in dependencies if _norm(s)],
            "risks": [_norm(s) for s in risks if _norm(s)],
            "open_questions": [_norm(s) for s in open_questions if _norm(s)],
            "rollback_plan": _norm(str(rollback_plan or "")),
            "stop_conditions": [_norm(s) for s in stop_conditions if _norm(s)],
        },
        "acceptance_criteria": normalised_acs,
        "impact_map": normalised_impact,
        "observed_at": _norm(str(observed_at or _now_iso())),
    }


# ---------------------------------------------------------------------------
# lint_intake
# ---------------------------------------------------------------------------

def lint_intake(envelope: Mapping[str, Any]) -> Dict[str, Any]:
    """Structural + semantic lint over an intake envelope.

    Returns ``{"valid": True/False, "errors": [...], "warnings": [...]}``
    without raising.  This is the non-fatal complement to the hard gates in
    ``compile_intake`` — it catches schema drift and stale-snapshot cases that
    only become detectable after the envelope is built.
    """
    errors: List[str] = []
    warnings: List[str] = []

    if envelope.get("schema") != INTAKE_SCHEMA:
        errors.append("schema_invalid")

    task = envelope.get("task") or {}
    if not _norm(str(task.get("objective") or "")):
        errors.append("objective_empty")
    if task.get("delivery_target") not in DELIVERY_TARGETS:
        errors.append("delivery_target_invalid")

    acs = list(envelope.get("acceptance_criteria") or [])
    if not acs:
        errors.append("no_acceptance_criteria")
    seen_ids: set = set()
    for ac in acs:
        ac_id = str(ac.get("id") or "")
        if ac_id in seen_ids:
            errors.append(f"duplicate_ac_id:{ac_id}")
        seen_ids.add(ac_id)
        if not _norm(str(ac.get("text") or "")):
            errors.append(f"ac_text_empty:{ac_id}")
        if not _norm(str(ac.get("verification") or "")):
            errors.append(f"ac_verification_missing:{ac_id}")
        if ac.get("origin") == "source" and not _norm(str(ac.get("source_ref") or "")):
            errors.append(f"source_ac_missing_ref:{ac_id}")
        if ac.get("status") != "pending" and not ac.get("evidence"):
            warnings.append(f"ac_done_without_evidence:{ac_id}")

    impact = envelope.get("impact_map") or {}
    for cat in IMPACT_CATEGORIES:
        if cat not in impact:
            errors.append(f"impact_category_missing:{cat}")
        else:
            entry = impact[cat]
            if not isinstance(entry, Mapping):
                errors.append(f"impact_category_invalid_type:{cat}")
            elif entry.get("status") == "not_applicable" and not _norm(str(entry.get("justification") or "")):
                errors.append(f"impact_not_applicable_no_justification:{cat}")

    snapshot = envelope.get("source_snapshot") or {}
    if not _norm(str(snapshot.get("revision") or "")):
        errors.append("source_snapshot_revision_missing")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# freeze_intake
# ---------------------------------------------------------------------------

def freeze_intake(envelope: Dict[str, Any]) -> tuple[Dict[str, Any], str]:
    """Attach a canonical ``intake_hash`` to the envelope and return ``(envelope, hash)``.

    The hash covers every field in ``envelope`` (including any existing
    ``intake_hash`` field is stripped first so the computation is idempotent).
    """
    # Strip any stale hash before computing
    clean = {k: v for k, v in envelope.items() if k != "intake_hash"}
    h = _stable_hash(clean)
    envelope = dict(clean)
    envelope["intake_hash"] = h
    return envelope, h


__all__ = [
    "INTAKE_SCHEMA",
    "DELIVERY_TARGETS",
    "IMPACT_CATEGORIES",
    "VERDICT_COMPLETE",
    "VERDICT_BLOCKED",
    "VERDICT_AWAITING_DECISION",
    "VERDICT_STALE_SOURCE",
    "VERDICT_LEASE_LOST",
    "IntakeBlockedError",
    "make_ac_id",
    "normalize_ac",
    "normalize_impact_entry",
    "make_source_snapshot",
    "compile_intake",
    "lint_intake",
    "freeze_intake",
]
