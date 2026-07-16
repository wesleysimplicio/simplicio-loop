"""completion_auditor: the final independent auditor (issue #431, epic #422).

Materializes the terminal role/stage already registered in the #423 contract
(``contracts/stage-agents/v1/stages.json`` — role_id ``completion_auditor``,
stage_id ``done``). This module is the last trust boundary before a run is
allowed to report ``COMPLETE``: it does not trust summaries, self-reports or a
bare "done" flag from any prior actor. It independently recomputes:

* graph completeness + lineage (every required stage has an accepted, fresh,
  same-lineage receipt; an optional skip has a condition receipt);
* identity/isolation (the auditor and the roles it audits are distinct
  ``agent_instance_id``s — same-actor pattern from :mod:`review_panel`);
* an AC -> stage -> evidence coverage matrix (complete and non-contradictory);
* the watcher challenge/receipt (challenge-bound, independent, fresh);
* delivery/source re-query freshness and observed state;
* regression against a prior ``COMPLETE`` completion receipt.

Pure reducer, stdlib-only, no I/O: every side effect (reading files, querying
the source, executing the watcher CLI) is performed by the caller and injected
as data. This keeps :func:`audit` exhaustively testable, replayable and
deterministic regardless of input ordering.

**Fail-closed by design.** Every branch that cannot positively confirm a fact
returns ``BLOCKED`` (or, for AC-level items, ``unverified``) — never
``COMPLETE``. Unknown/permission/rate-limited delivery or source state is
always ``UNVERIFIED``, matching invariant 8 of the issue. No code path in this
module accepts a bare self-reported ``done`` flag as sufficient: only a
content-addressed completion receipt built by :func:`build_completion_receipt`
and checked by :func:`gate_promise` can unblock a promise.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Mapping, MutableMapping, Sequence

from . import delivery
from . import freshness
from . import stage_agents as sa

SCHEMA_AUDIT_MATRIX = "simplicio.completion-audit-matrix/v1"
SCHEMA_COMPLETION_RECEIPT = "simplicio.completion-receipt/v1"

# --------------------------------------------------------------------------- #
# Verdicts + reason codes
# --------------------------------------------------------------------------- #

VERDICT_COMPLETE = "COMPLETE"
VERDICT_PARTIAL = "PARTIAL"
VERDICT_BLOCKED = "BLOCKED"
VERDICT_REGRESSED = "REGRESSED"

REASON_OK = "ok"
REASON_INVALID_GRAPH = "invalid_stage_graph"
REASON_MISSING_STAGE = "missing_required_stage_receipt"
REASON_OPTIONAL_SKIP_NO_CONDITION = "optional_skip_missing_condition_receipt"
REASON_STALE_RECEIPT = "stale_receipt"
REASON_LINEAGE_MISMATCH = "lineage_mismatch"
REASON_IDENTITY_COLLISION = "auditor_identity_collision"
REASON_FORGED_RECEIPT = "forged_or_unbound_receipt"
REASON_AC_COVERAGE_INCOMPLETE = "ac_coverage_incomplete"
REASON_AC_CONTRADICTION = "ac_coverage_contradictory"
REASON_AC_MISSING = "ac_no_evidence"
REASON_WATCHER_MISSING = "watcher_receipt_missing"
REASON_WATCHER_MISMATCH = "watcher_challenge_mismatch"
REASON_WATCHER_STALE = "watcher_stale"
REASON_WATCHER_UNVERIFIED = "watcher_unverified"
REASON_DELIVERY_MISSING = "delivery_receipt_missing"
REASON_DELIVERY_STALE = "delivery_stale"
REASON_DELIVERY_UNKNOWN = "delivery_state_unknown"
REASON_DELIVERY_REGRESSED = "delivery_regressed"
REASON_SOURCE_STALE = "source_requery_stale"
REASON_SOURCE_UNKNOWN = "source_state_unknown"
REASON_REGRESSION = "regression_detected"
REASON_NO_AUDIT_RECEIPT = "no_completion_audit_receipt"
REASON_RECEIPT_EXPIRED = "completion_receipt_expired"
REASON_RECEIPT_HASH_MISMATCH = "completion_receipt_hash_mismatch"
REASON_RECEIPT_VERDICT_NOT_COMPLETE = "completion_receipt_verdict_not_complete"

# States that must always be treated as UNVERIFIED, never as a pass — invariant 8.
_UNKNOWN_STATES = frozenset(("unknown", "permission_denied", "rate_limited", ""))

# States that positively indicate a rollback/undo of a prior delivery/source fact.
_REGRESSED_STATES = frozenset(("reverted", "rolled_back", "unmerged", "reopened"))

# GitHub-side terminal states not modeled by delivery.DELIVERY_ORDER (a closed
# issue/PR is a valid terminal fact even though it isn't a delivery-pipeline stage).
_GITHUB_TERMINAL_STATES = frozenset(("closed",))

_STAGE_NOT_AUDITED = "done"  # the auditor's own stage is not audited against itself.


class CompletionAuditorError(ValueError):
    """Raised for malformed inputs the reducer cannot safely reason about."""


def _sha256_json(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------------------- #
# 1. Graph completeness + lineage
# --------------------------------------------------------------------------- #


def required_stage_ids(graph: Mapping[str, Any]) -> list[str]:
    """Stage ids the auditor must find a receipt for (all but its own ``done`` stage)."""
    ok, errors = sa.validate_graph(graph)
    if not ok:
        raise CompletionAuditorError("invalid stage graph: " + "; ".join(errors))
    return [
        s["stage_id"] for s in graph["stages"]
        if s["stage_id"] != _STAGE_NOT_AUDITED
    ]


def _is_optional(stage: Mapping[str, Any]) -> bool:
    return bool(stage.get("optional", False))


def _receipts_for_stage(receipts: Sequence[Mapping[str, Any]], stage_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in receipts if r.get("stage_id") == stage_id]


def _lineage_matches(rec: Mapping[str, Any], run_identity: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in ("run_id", "task_id", "fence", "plan_revision"):
        expected = run_identity.get(key)
        if expected is None:
            continue
        if str(rec.get(key)) != str(expected):
            errors.append(f"receipt.{key} {rec.get(key)!r} != run identity {expected!r}")
    return errors


def validate_stage_lineage(
    graph: Mapping[str, Any],
    receipts: Sequence[Mapping[str, Any]],
    run_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a stage -> {required, receipt, verdict, reasons} map.

    Every stage is evaluated independently and inputs are stable-sorted before
    processing so the result never depends on caller-supplied ordering
    (reducer order independence).
    """
    stages = {s["stage_id"]: s for s in graph["stages"] if s["stage_id"] != _STAGE_NOT_AUDITED}
    receipts_sorted = sorted(receipts, key=lambda r: (str(r.get("stage_id", "")), str(r.get("receipt_id", ""))))

    matrix: dict[str, dict[str, Any]] = {}
    for stage_id in sorted(stages):
        stage = stages[stage_id]
        optional = _is_optional(stage)
        candidates = _receipts_for_stage(receipts_sorted, stage_id)

        entry: dict[str, Any] = {
            "stage_id": stage_id,
            "role_id": stage.get("role_id"),
            "required": not optional,
            "instance_id": None,
            "receipt_id": None,
            "evidence_refs": [],
            "verdict": "missing",
            "reasons": [],
        }

        if not candidates:
            if optional:
                entry["verdict"] = "missing_condition_receipt"
                entry["reasons"].append(REASON_OPTIONAL_SKIP_NO_CONDITION)
            else:
                entry["verdict"] = "missing"
                entry["reasons"].append(REASON_MISSING_STAGE)
            matrix[stage_id] = entry
            continue

        # An optional stage's condition receipt must explicitly carry verdict "skip"
        # with a reason; anything else for an optional stage still needs a real pass.
        accepted = [r for r in candidates if r.get("verdict") == "pass" and r.get("accepted") is True]
        skipped = [r for r in candidates if r.get("verdict") == "skip"]

        if optional and skipped and not accepted:
            rec = skipped[0]
            if not str(rec.get("skip_condition") or rec.get("reason_code") or "").strip():
                entry["verdict"] = "missing_condition_receipt"
                entry["reasons"].append(REASON_OPTIONAL_SKIP_NO_CONDITION)
                matrix[stage_id] = entry
                continue
            lineage_errors = _lineage_matches(rec, run_identity)
            if lineage_errors:
                entry["verdict"] = "lineage_mismatch"
                entry["reasons"].extend(lineage_errors)
                matrix[stage_id] = entry
                continue
            entry.update({
                "instance_id": rec.get("agent_instance_id"),
                "receipt_id": rec.get("receipt_id"),
                "evidence_refs": list(rec.get("evidence_refs", [])),
                "verdict": "skipped",
            })
            matrix[stage_id] = entry
            continue

        if not accepted:
            entry["verdict"] = "missing"
            entry["reasons"].append(REASON_MISSING_STAGE)
            matrix[stage_id] = entry
            continue

        # Split accepted receipts into ones whose lineage matches the current
        # run identity and ones that don't. A receipt belonging to a foreign
        # run/task/fence/plan_revision (e.g. left over from another run using
        # the same stage_id) is simply irrelevant noise here -- it must not be
        # able to either satisfy or corrupt this run's audit. A receipt that
        # DOES match the current run identity but disagrees with another
        # matching receipt is a genuine same-lineage conflict and blocks.
        matching = [r for r in accepted if not _lineage_matches(r, run_identity)]
        foreign = [r for r in accepted if r not in matching]

        if not matching:
            # Every accepted receipt for this stage belongs to a different
            # lineage; from this run's point of view the stage is either
            # missing (foreign-only, no signal at all) or stale (the one
            # in-place receipt we do have disagrees with our identity).
            if len(candidates) == 1:
                entry["verdict"] = "lineage_mismatch"
                entry["reasons"].extend(_lineage_matches(foreign[0] if foreign else candidates[0], run_identity))
            else:
                entry["verdict"] = "missing"
                entry["reasons"].append(REASON_MISSING_STAGE)
            matrix[stage_id] = entry
            continue

        rec = matching[0]
        lineage_errors: list[str] = []
        for other in matching[1:]:
            if rec.get("agent_instance_id") != other.get("agent_instance_id") or \
                    rec.get("receipt_id") != other.get("receipt_id"):
                lineage_errors.append(f"stage {stage_id} has conflicting accepted receipts within the same lineage")

        if lineage_errors:
            entry["verdict"] = "lineage_mismatch"
            entry["reasons"].extend(sorted(set(lineage_errors)))
            matrix[stage_id] = entry
            continue

        entry.update({
            "instance_id": rec.get("agent_instance_id"),
            "receipt_id": rec.get("receipt_id"),
            "evidence_refs": list(rec.get("evidence_refs", [])),
            "verdict": "pass",
        })
        matrix[stage_id] = entry

    return matrix


# --------------------------------------------------------------------------- #
# 2. Identity / isolation
# --------------------------------------------------------------------------- #


def validate_auditor_isolation(
    *, auditor_instance_id: str, instances: Sequence[Mapping[str, Any]], graph: Mapping[str, Any]
) -> tuple[bool, list[str]]:
    """The auditor must be a separate actor from every implementer/delivery instance.

    Reuses :func:`stage_agents.enforce_independence` for the whole graph, and
    additionally rejects the auditor sharing its own instance id with any
    other role's instance (a stricter, explicit same-actor check for the
    terminal trust boundary).
    """
    errors: list[str] = []
    if not str(auditor_instance_id or "").strip():
        return False, ["auditor_instance_id is required"]

    ok, graph_errors = sa.enforce_independence(instances, graph)
    if not ok:
        errors.extend(graph_errors)

    for inst in instances:
        inst_role = inst.get("role_id")
        inst_id = inst.get("agent_instance_id")
        if inst_role == "completion_auditor":
            continue
        if inst_id == auditor_instance_id:
            errors.append(
                f"auditor_instance_id collides with role {inst_role!r} instance {inst_id!r}"
            )
    return (len(errors) == 0), errors


# --------------------------------------------------------------------------- #
# 3. AC -> stage -> evidence coverage matrix
# --------------------------------------------------------------------------- #


def build_ac_coverage_matrix(
    ac_items: Sequence[Mapping[str, Any]],
    criteria_results: Sequence[Mapping[str, Any]],
    *,
    evidence_stage_id: str = "watching",
) -> dict[str, Any]:
    """Build the AC coverage matrix from anchor criteria + watcher criteria_results.

    Detects: missing AC (no result at all), unverified AC (result present but
    not matched / no evidence), and contradiction (duplicate result ids that
    disagree with each other).
    """
    by_id: dict[str, list[Mapping[str, Any]]] = {}
    for item in criteria_results:
        cid = str(item.get("id") or "")
        if not cid:
            continue
        by_id.setdefault(cid, []).append(item)

    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    unverified: list[str] = []
    contradictory: list[str] = []

    for ac in sorted(ac_items, key=lambda a: str(a.get("id", ""))):
        cid = str(ac.get("id") or "")
        results = by_id.get(cid, [])
        if not results:
            missing.append(cid)
            rows.append({
                "ac_id": cid, "stage_id": evidence_stage_id, "evidence_refs": [],
                "verdict": "missing",
            })
            continue

        matches = {bool(r.get("match")) for r in results}
        evidence_id_sets = {tuple(sorted(r.get("evidence_ids") or [])) for r in results}
        if len(matches) > 1 or len(evidence_id_sets) > 1:
            contradictory.append(cid)
            rows.append({
                "ac_id": cid, "stage_id": evidence_stage_id,
                "evidence_refs": sorted({e for s in evidence_id_sets for e in s}),
                "verdict": "contradictory",
            })
            continue

        result = results[0]
        evidence_refs = [str(e).strip() for e in (result.get("evidence_ids") or []) if str(e).strip()]
        matched = bool(result.get("match")) and bool(evidence_refs)
        if not matched:
            unverified.append(cid)
        rows.append({
            "ac_id": cid,
            "stage_id": evidence_stage_id,
            "evidence_refs": evidence_refs,
            "verdict": "verified" if matched else "unverified",
        })

    complete = not missing and not unverified and not contradictory
    return {
        "schema": "simplicio.ac-coverage-matrix/v1",
        "rows": rows,
        "missing": sorted(missing),
        "unverified": sorted(unverified),
        "contradictory": sorted(contradictory),
        "complete": complete,
    }


# --------------------------------------------------------------------------- #
# 4. Watcher revalidation (challenge-bound, independent — reuses watcher_verify.py's pattern)
# --------------------------------------------------------------------------- #


def revalidate_watcher(
    watcher_receipt: Mapping[str, Any] | None,
    watcher_challenge: Mapping[str, Any] | None,
    *,
    now: float | None = None,
    ttl_seconds: int = 900,
) -> dict[str, Any]:
    """Independently re-check the watcher receipt against its own challenge.

    Mirrors ``scripts/watcher_verify.py``'s approach: a challenge alone never
    approves anything, and a receipt is only trusted when it is bound to the
    *current* challenge, reports ``match``/``MEASURED``, and is fresh.
    """
    if not watcher_receipt:
        return {"ok": False, "reason_code": REASON_WATCHER_MISSING, "detail": "no watcher receipt supplied"}
    if not watcher_challenge:
        return {"ok": False, "reason_code": REASON_WATCHER_MISSING, "detail": "no watcher challenge supplied"}

    expected_challenge = str(watcher_challenge.get("challenge") or "")
    actual_challenge = str(watcher_receipt.get("challenge") or "")
    if not expected_challenge or expected_challenge != actual_challenge:
        return {"ok": False, "reason_code": REASON_WATCHER_MISMATCH,
                "detail": "watcher receipt is not bound to the current challenge"}

    if watcher_receipt.get("status") != "MEASURED" or not watcher_receipt.get("match"):
        return {"ok": False, "reason_code": REASON_WATCHER_UNVERIFIED,
                "detail": str(watcher_receipt.get("reported") or "watcher did not report a verified match")}

    checked_at = watcher_receipt.get("checked_at")
    gate = freshness.freshness_gate(checked_at, "merge-ready", overrides={"merge-ready": ttl_seconds})
    if gate["status"] != "pass":
        return {"ok": False, "reason_code": REASON_WATCHER_STALE, "detail": gate["detail"]}

    return {"ok": True, "reason_code": REASON_OK, "detail": "watcher receipt is challenge-bound, matched and fresh"}


# --------------------------------------------------------------------------- #
# 5. Delivery / source re-query revalidation
# --------------------------------------------------------------------------- #


def revalidate_delivery(
    delivery_receipt: Mapping[str, Any] | None,
    source_requery: Mapping[str, Any] | None,
    *,
    ttl_overrides: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Re-check delivery confirmation + a fresh source re-query.

    Unknown/permission/rate-limit states are always UNVERIFIED, never a pass
    (invariant 8) — this never assumes success from a missing or ambiguous
    observation.
    """
    if not delivery_receipt:
        return {"ok": False, "reason_code": REASON_DELIVERY_MISSING, "detail": "no delivery receipt supplied"}

    state = str(delivery_receipt.get("current_state") or delivery_receipt.get("state") or "").strip().lower()
    if state in _UNKNOWN_STATES:
        return {"ok": False, "reason_code": REASON_DELIVERY_UNKNOWN, "detail": f"delivery state {state!r} is unknown"}
    if state in _REGRESSED_STATES:
        return {"ok": False, "reason_code": REASON_DELIVERY_REGRESSED, "detail": f"delivery state regressed to {state!r}"}
    if delivery.delivery_rank(state) < 0 and state not in _GITHUB_TERMINAL_STATES:
        return {"ok": False, "reason_code": REASON_DELIVERY_UNKNOWN, "detail": f"delivery state {state!r} is not recognized"}

    checked_at = delivery_receipt.get("source_checked_at") or delivery_receipt.get("checked_at")
    gate = freshness.freshness_gate(checked_at, state, overrides=ttl_overrides)
    if gate["status"] != "pass":
        return {"ok": False, "reason_code": REASON_DELIVERY_STALE, "detail": gate["detail"]}

    if not source_requery:
        return {"ok": False, "reason_code": REASON_SOURCE_UNKNOWN, "detail": "no source re-query supplied"}

    source_state = str(source_requery.get("state") or "").strip().lower()
    if source_state in _UNKNOWN_STATES:
        return {"ok": False, "reason_code": REASON_SOURCE_UNKNOWN, "detail": f"source state {source_state!r} is unknown"}
    if source_state in _REGRESSED_STATES:
        return {"ok": False, "reason_code": REASON_DELIVERY_REGRESSED, "detail": f"source state regressed to {source_state!r}"}

    source_checked_at = source_requery.get("checked_at")
    source_gate = freshness.freshness_gate(source_checked_at, source_state, overrides=ttl_overrides)
    if source_gate["status"] != "pass":
        return {"ok": False, "reason_code": REASON_SOURCE_STALE, "detail": source_gate["detail"]}

    # No shared helper exists for cross-checking delivery vs source state; require
    # them to agree literally, or that both independently resolve to a recognized
    # terminal state at least as advanced as the delivery-reported one. A source
    # that disagrees or hasn't reached the delivery-reported state is treated as
    # an unresolved/unknown observation, never a silent pass.
    consistent = source_state == state or (
        source_state in _GITHUB_TERMINAL_STATES and state in _GITHUB_TERMINAL_STATES
    ) or (
        delivery.delivery_rank(source_state) >= 0 and delivery.delivery_rank(state) >= 0
        and delivery.delivery_rank(source_state) >= delivery.delivery_rank(state)
    )
    if not consistent:
        return {"ok": False, "reason_code": REASON_DELIVERY_UNKNOWN,
                "detail": f"delivery state {state!r} and source state {source_state!r} disagree"}

    return {"ok": True, "reason_code": REASON_OK, "detail": "delivery and source re-query are fresh and consistent"}


# --------------------------------------------------------------------------- #
# 6. Regression detection
# --------------------------------------------------------------------------- #


def detect_regression(
    previous_completion_receipt: Mapping[str, Any] | None,
    *,
    ac_coverage: Mapping[str, Any],
    watcher_check: Mapping[str, Any],
    delivery_check: Mapping[str, Any],
) -> dict[str, Any]:
    """A prior COMPLETE that no longer holds is a regression, never a silent re-PARTIAL."""
    if not previous_completion_receipt:
        return {"regressed": False, "reason_code": REASON_OK}
    if previous_completion_receipt.get("verdict") != VERDICT_COMPLETE:
        return {"regressed": False, "reason_code": REASON_OK}

    if not ac_coverage.get("complete") or not watcher_check.get("ok") or not delivery_check.get("ok"):
        return {
            "regressed": True,
            "reason_code": REASON_REGRESSION,
            "detail": "a previously COMPLETE run no longer satisfies AC coverage, watcher, or delivery checks",
        }
    return {"regressed": False, "reason_code": REASON_OK}


# --------------------------------------------------------------------------- #
# 7. The pure audit reducer
# --------------------------------------------------------------------------- #


def audit(
    *,
    graph: Mapping[str, Any],
    instances: Sequence[Mapping[str, Any]],
    receipts: Sequence[Mapping[str, Any]],
    run_identity: Mapping[str, Any],
    auditor_instance_id: str,
    ac_items: Sequence[Mapping[str, Any]],
    criteria_results: Sequence[Mapping[str, Any]],
    watcher_receipt: Mapping[str, Any] | None = None,
    watcher_challenge: Mapping[str, Any] | None = None,
    delivery_receipt: Mapping[str, Any] | None = None,
    source_requery: Mapping[str, Any] | None = None,
    previous_completion_receipt: Mapping[str, Any] | None = None,
    journal_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Independently recompute the terminal verdict. Never trusts a summary.

    Deterministic regardless of the order ``instances``/``receipts``/``ac_items``/
    ``criteria_results`` are supplied in (every internal step sorts by a stable
    key before reducing). Defaults to :data:`VERDICT_BLOCKED` on any ambiguous
    or missing input — this function must never fail open.
    """
    try:
        ok, graph_errors = sa.validate_graph(graph)
    except Exception as exc:  # pragma: no cover - defensive
        ok, graph_errors = False, [str(exc)]
    if not ok:
        return blocked_result(REASON_INVALID_GRAPH, graph_errors)

    stage_matrix = validate_stage_lineage(graph, receipts, run_identity)
    missing_stages = sorted(sid for sid, e in stage_matrix.items() if e["verdict"] == "missing")
    missing_conditions = sorted(sid for sid, e in stage_matrix.items() if e["verdict"] == "missing_condition_receipt")
    stale_stages = sorted(sid for sid, e in stage_matrix.items() if e["verdict"] == "lineage_mismatch")

    isolation_ok, isolation_errors = validate_auditor_isolation(
        auditor_instance_id=auditor_instance_id, instances=instances, graph=graph,
    )

    ac_coverage = build_ac_coverage_matrix(ac_items, criteria_results)
    watcher_check = revalidate_watcher(watcher_receipt, watcher_challenge)
    delivery_check = revalidate_delivery(delivery_receipt, source_requery)
    regression = detect_regression(
        previous_completion_receipt,
        ac_coverage=ac_coverage, watcher_check=watcher_check, delivery_check=delivery_check,
    )

    all_evidence_refs = sorted({
        ref
        for entry in stage_matrix.values()
        for ref in entry.get("evidence_refs", [])
    } | {ref for row in ac_coverage["rows"] for ref in row.get("evidence_refs", [])})

    result: dict[str, Any] = {
        "schema": SCHEMA_AUDIT_MATRIX,
        "run_identity": dict(run_identity),
        "audit_matrix": stage_matrix,
        "missing_stages": missing_stages,
        "missing_optional_conditions": missing_conditions,
        "stale_stages": stale_stages,
        "isolation_ok": isolation_ok,
        "isolation_errors": isolation_errors,
        "ac_coverage": ac_coverage,
        "watcher_check": watcher_check,
        "delivery_check": delivery_check,
        "regression": regression,
        "evidence_refs": all_evidence_refs,
        "journal_state": dict(journal_state or {}),
    }

    # Priority order is fixed and mechanical, so the chosen reason_code never
    # depends on input ordering: regression first (it invalidates a prior
    # terminal outright), then structural/identity failures, then AC/watcher/
    # delivery failures, then success.
    if regression["regressed"]:
        result["verdict"] = VERDICT_REGRESSED
        result["reason_code"] = regression["reason_code"]
        result["reopen_graph"] = True
        return result

    if missing_stages:
        return _finish_blocked(result, REASON_MISSING_STAGE, {"stages": missing_stages})
    if missing_conditions:
        return _finish_blocked(result, REASON_OPTIONAL_SKIP_NO_CONDITION, {"stages": missing_conditions})
    if stale_stages:
        return _finish_blocked(result, REASON_LINEAGE_MISMATCH, {"stages": stale_stages})
    if not isolation_ok:
        return _finish_blocked(result, REASON_IDENTITY_COLLISION, {"errors": isolation_errors})

    if ac_coverage["contradictory"]:
        return _finish_blocked(result, REASON_AC_CONTRADICTION, {"ac_ids": ac_coverage["contradictory"]})
    if ac_coverage["missing"]:
        return _finish_blocked(result, REASON_AC_MISSING, {"ac_ids": ac_coverage["missing"]})

    if not watcher_check["ok"]:
        return _finish_blocked(result, watcher_check["reason_code"], {"detail": watcher_check["detail"]})
    if not delivery_check["ok"]:
        return _finish_blocked(result, delivery_check["reason_code"], {"detail": delivery_check["detail"]})

    if ac_coverage["unverified"]:
        # Structure is sound and nothing is stale/conflicting/missing outright —
        # this is genuinely still in progress, not a hard block.
        result["verdict"] = VERDICT_PARTIAL
        result["reason_code"] = REASON_AC_COVERAGE_INCOMPLETE
        result["detail"] = {"ac_ids": ac_coverage["unverified"]}
        return result

    result["verdict"] = VERDICT_COMPLETE
    result["reason_code"] = REASON_OK
    return result


def blocked_result(reason_code: str, errors: Sequence[str]) -> dict[str, Any]:
    return {
        "schema": SCHEMA_AUDIT_MATRIX,
        "verdict": VERDICT_BLOCKED,
        "reason_code": reason_code,
        "detail": {"errors": list(errors)},
        "audit_matrix": {},
        "missing_stages": [],
        "missing_optional_conditions": [],
        "stale_stages": [],
        "isolation_ok": False,
        "isolation_errors": list(errors),
        "ac_coverage": {"rows": [], "missing": [], "unverified": [], "contradictory": [], "complete": False},
        "watcher_check": {"ok": False, "reason_code": REASON_WATCHER_MISSING},
        "delivery_check": {"ok": False, "reason_code": REASON_DELIVERY_MISSING},
        "regression": {"regressed": False, "reason_code": REASON_OK},
        "evidence_refs": [],
        "journal_state": {},
    }


def _finish_blocked(result: MutableMapping[str, Any], reason_code: str, detail: Mapping[str, Any]) -> dict[str, Any]:
    result["verdict"] = VERDICT_BLOCKED
    result["reason_code"] = reason_code
    result["detail"] = dict(detail)
    return result


# --------------------------------------------------------------------------- #
# 8. Completion receipt — content-addressed, lineage-bound, TTL'd
# --------------------------------------------------------------------------- #


def build_completion_receipt(
    audit_result: Mapping[str, Any],
    *,
    created_at: str | None = None,
    ttl_seconds: int = 3600,
) -> dict[str, Any]:
    """Build the immutable completion receipt from an :func:`audit` result.

    The receipt hashes the *entire* evidence set (audit matrix + AC coverage +
    watcher/delivery checks + evidence refs) so any later tampering with any
    one artifact changes the hash and invalidates the receipt.
    """
    created_at = created_at or _now_iso()
    evidence_set = {
        "audit_matrix": audit_result.get("audit_matrix", {}),
        "ac_coverage": audit_result.get("ac_coverage", {}),
        "watcher_check": audit_result.get("watcher_check", {}),
        "delivery_check": audit_result.get("delivery_check", {}),
        "evidence_refs": audit_result.get("evidence_refs", []),
    }
    evidence_set_hash = _sha256_json(evidence_set)
    run_identity = audit_result.get("run_identity", {})

    payload = {
        "schema": SCHEMA_COMPLETION_RECEIPT,
        "verdict": audit_result.get("verdict"),
        "reason_code": audit_result.get("reason_code"),
        "run_id": run_identity.get("run_id"),
        "task_id": run_identity.get("task_id"),
        "fence": run_identity.get("fence"),
        "plan_revision": run_identity.get("plan_revision"),
        "evidence_set_hash": evidence_set_hash,
        "created_at": created_at,
        "ttl_seconds": ttl_seconds,
    }
    receipt_id = _sha256_json(payload)
    return {**payload, "receipt_id": receipt_id}


def validate_completion_receipt(
    receipt: Mapping[str, Any] | None,
    audit_result: Mapping[str, Any],
    *,
    now: float | None = None,
) -> tuple[bool, str]:
    """Re-validate a persisted completion receipt against a freshly computed audit.

    Recomputes the receipt from ``audit_result`` and compares hashes rather
    than trusting the stored ``verdict``/``receipt_id`` fields at face value —
    a tampered or replayed receipt from another run/lineage is rejected.
    """
    if not receipt:
        return False, REASON_NO_AUDIT_RECEIPT

    recomputed = build_completion_receipt(
        audit_result, created_at=receipt.get("created_at"), ttl_seconds=receipt.get("ttl_seconds", 3600),
    )
    if recomputed["receipt_id"] != receipt.get("receipt_id"):
        return False, REASON_RECEIPT_HASH_MISMATCH

    if receipt.get("verdict") != VERDICT_COMPLETE:
        return False, REASON_RECEIPT_VERDICT_NOT_COMPLETE

    created_at = receipt.get("created_at")
    ttl_seconds = int(receipt.get("ttl_seconds", 3600))
    gate = freshness.freshness_gate(created_at, "merge-ready", overrides={"merge-ready": ttl_seconds})
    if gate["status"] != "pass":
        return False, REASON_RECEIPT_EXPIRED

    return True, REASON_OK


def gate_promise(
    *, completion_receipt: Mapping[str, Any] | None, audit_result: Mapping[str, Any],
    self_reported_done: bool = False,
) -> tuple[bool, str]:
    """Block a promise/"done" flag unless a valid completion receipt exists.

    ``self_reported_done`` is accepted as a parameter only so callers cannot
    bypass this gate by omitting it — it is NEVER read; a bare self-report is
    never sufficient on its own, by construction.
    """
    del self_reported_done  # intentionally unused: no code path may honor a bare self-report.
    ok, reason = validate_completion_receipt(completion_receipt, audit_result)
    return ok, reason


# --------------------------------------------------------------------------- #
# 9. Human-readable report + machine payload
# --------------------------------------------------------------------------- #


def human_report(audit_result: Mapping[str, Any]) -> str:
    verdict = audit_result.get("verdict", VERDICT_BLOCKED)
    reason = audit_result.get("reason_code", "")
    lines = [f"completion_auditor verdict: {verdict} ({reason})"]
    matrix = audit_result.get("audit_matrix") or {}
    if matrix:
        lines.append("stages:")
        for stage_id in sorted(matrix):
            entry = matrix[stage_id]
            lines.append(f"  - {stage_id}: {entry['verdict']}")
    coverage = audit_result.get("ac_coverage") or {}
    if coverage.get("rows"):
        done, total = sum(1 for r in coverage["rows"] if r["verdict"] == "verified"), len(coverage["rows"])
        lines.append(f"acceptance criteria: {done}/{total} verified")
    return "\n".join(lines)


def machine_payload(audit_result: Mapping[str, Any], completion_receipt: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(audit_result)
    if completion_receipt is not None:
        payload["completion_receipt"] = dict(completion_receipt)
    return payload


__all__ = [
    "SCHEMA_AUDIT_MATRIX",
    "SCHEMA_COMPLETION_RECEIPT",
    "VERDICT_COMPLETE",
    "VERDICT_PARTIAL",
    "VERDICT_BLOCKED",
    "VERDICT_REGRESSED",
    "REASON_OK",
    "REASON_INVALID_GRAPH",
    "REASON_MISSING_STAGE",
    "REASON_OPTIONAL_SKIP_NO_CONDITION",
    "REASON_STALE_RECEIPT",
    "REASON_LINEAGE_MISMATCH",
    "REASON_IDENTITY_COLLISION",
    "REASON_FORGED_RECEIPT",
    "REASON_AC_COVERAGE_INCOMPLETE",
    "REASON_AC_CONTRADICTION",
    "REASON_AC_MISSING",
    "REASON_WATCHER_MISSING",
    "REASON_WATCHER_MISMATCH",
    "REASON_WATCHER_STALE",
    "REASON_WATCHER_UNVERIFIED",
    "REASON_DELIVERY_MISSING",
    "REASON_DELIVERY_STALE",
    "REASON_DELIVERY_UNKNOWN",
    "REASON_DELIVERY_REGRESSED",
    "REASON_SOURCE_STALE",
    "REASON_SOURCE_UNKNOWN",
    "REASON_REGRESSION",
    "REASON_NO_AUDIT_RECEIPT",
    "REASON_RECEIPT_EXPIRED",
    "REASON_RECEIPT_HASH_MISMATCH",
    "REASON_RECEIPT_VERDICT_NOT_COMPLETE",
    "CompletionAuditorError",
    "required_stage_ids",
    "validate_stage_lineage",
    "validate_auditor_isolation",
    "build_ac_coverage_matrix",
    "revalidate_watcher",
    "revalidate_delivery",
    "detect_regression",
    "audit",
    "build_completion_receipt",
    "validate_completion_receipt",
    "gate_promise",
    "blocked_result",
    "human_report",
    "machine_payload",
]
