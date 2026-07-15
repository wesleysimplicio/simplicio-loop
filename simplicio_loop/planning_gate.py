"""Planning receipt + mutation authority (#284).

The runner already refuses `execute_operator()` without a *fresh* mapper/plan/
operator-preflight trio, a passing `validate_plan()`, an unchanged repo state
since planning, and an operator target confined to the plan's
`candidate_targets` (see `simplicio_loop/runner.py::execute_operator`). What was
missing, per issue #284, is a single, explicit, hash-bound **planning receipt**
that ties those checks together into one artifact plus a **mutation authority**
token derived from it — so a future caller cannot invoke the operator boundary
while silently skipping part of the intake, and any drift in the underlying
contract/plan/lease invalidates the authority immediately (fail-closed, never a
stale "yes").

This module is intentionally data-only and model-free, same discipline as
`plan_contract.py` and `quality_matrix.py`: it reads/writes a JSON receipt and a
derived token, and never invents evidence for a gate that wasn't actually
satisfied.

Scope landed here (opt-in, additive — a run that never builds a planning
receipt keeps working exactly as before):

  * `build_planning_receipt(...)` combines the task contract hash, the plan
    hash, the AC <-> plan coverage already computed by `validate_plan`, and the
    optional lease/fencing identity into one receipt with a
    `ready_for_mutation` verdict.
  * `mutation_authority_token(...)` derives a stable token from that identity
    tuple; `verify_mutation_authority(...)` recomputes it from the CURRENT
    identity tuple and fails closed on any mismatch (stale plan hash, task
    contract changed, lease/fencing rotated, etc.).

GitHub source-revision capture is now wired in for real: `source_snapshot.py`
(added for this increment) captures a content-addressed snapshot of the
canonical GitHub issue (title/body/labels/milestone/assignees/comments) via
`gh issue view`, fail-closed on any `gh` failure. Its `snapshot_hash` is an
OPTIONAL sixth member of the mutation-authority identity tuple here: when a
caller supplies `source_snapshot_hash`, any edit to the source between
planning and execution changes the hash and invalidates the authority exactly
like a stale plan hash or rotated lease does. Omitting it (the default, `""`)
preserves the exact previous behavior for local/non-GitHub runs and every
existing fixture.

`SIMPLICIO_REQUIRE_MUTATION_AUTHORITY` is now mandatory-BY-DEFAULT (see
`mutation_authority_required()`): both `execute_operator()` and
`execute_operator_batch()` refuse to mutate without a valid, on-disk, hash-bound
mutation authority unless the caller explicitly opts out. This is intentionally
strict -- no auto-generated fallback -- a caller MUST run the real planning-gate
step (`scripts/planning_gate.py build`, or `build_planning_receipt()` directly)
before it can mutate anything; every test that previously assumed no gate was
in effect now stages a real receipt fixture first (see
`tests/planning_gate_fixtures.py`).

This increment adds the full `simplicio.task-intake/v1` envelope
(`intake_contract.py`), the `impact-map.json` artifact (wired via
`scripts/impact_audit.py`'s existing dependency audit), the AC<->step<->
test<->evidence matrix (`traceability_matrix.py`, whose `coverage_ok` gates
`ready_for_mutation` exactly like an invalid `plan_validation` does), and a
genuine replan-on-drift path (`replan_on_drift()`) that bumps `plan_revision`
and records a semantic diff instead of blocking forever. All four are
strictly additive: `build_planning_receipt()`'s new params default to
`None`/`0`, so a caller that never builds these artifacts is unaffected.

The post-#373 round additionally landed `plan_contract.validate_plan()`'s optional
`dag`/`parallel_groups`/step-`depends_on` fields (a compatible plan/v1 evolution,
not a new schema string -- a plan that never sets `dag` is unaffected; one that
does gets a fail-closed check that a "parallelizable" group never actually
contains a dependent pair); and `simplicio_loop/runner.py::_maybe_auto_build_planning_receipt()`,
which wires `build_planning_receipt()` (and, when a GitHub source is present,
this module's own `publish_planning_receipt()`) into the REAL `arm_run()`
dispatch path.

This round makes `_maybe_auto_build_planning_receipt()` **mandatory-by-default**,
via `auto_planning_receipt_enabled()` -- the same polarity-flip pattern
`mutation_authority_required()` used for `SIMPLICIO_REQUIRE_MUTATION_AUTHORITY`
(#284/#360): unset/blank now means ON, and a caller that truly needs the legacy
opt-in posture (or a test asserting the missing-receipt fail-closed path) sets
`SIMPLICIO_LOOP_AUTO_PLANNING_RECEIPT` to an explicit falsy value
(`0/false/no/off/legacy`). See `docs/adr/0004-planning-gate-rollout.md` for the
rollout/migration strategy (backward-compat shims, what breaks, how to opt out).

This round additionally folds in a typed `verdict` field across the receipt and
`evaluate_mutation_authority()` (`COMPLETE`/`BLOCKED`/`STALE_SOURCE`/
`LEASE_LOST`/`AWAITING_DECISION`/`AUTHORITY_EXPIRED`, exported as `VERDICT_*`
constants) plus two more gates: `awaiting_decision`/`awaiting_reason` on
`build_planning_receipt()` marks a receipt `AWAITING_DECISION` (never ready for
mutation, even over an otherwise-valid plan) when an ambiguity needs a human
call; `authority_ttl_seconds` (default `DEFAULT_AUTHORITY_TTL_SECONDS`) stamps
an `authority_expires_at` on the receipt so `evaluate_mutation_authority()`
returns `AUTHORITY_EXPIRED` once the TTL elapses instead of trusting a receipt
indefinitely. A distinct `source_revision` (a raw revision string, e.g. a
GitHub issue `updated_at`) is folded into the mutation-authority identity
tuple alongside the existing `source_snapshot_hash`; a caller that supplies
`current_source_revision` to `evaluate_mutation_authority()` gets a
`STALE_SOURCE` verdict the moment it drifts, and a lease/fencing mismatch is
now reported as the more specific `LEASE_LOST` instead of a generic
`mutation_authority_invalid`. All of this is additive -- every new parameter
defaults to `""`/`False`/the TTL default, so a caller that never sets them
keeps the exact previous receipt shape and verdict behavior.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

PLANNING_RECEIPT_SCHEMA = "simplicio.planning-receipt/v1"
RECEIPT_FILENAME = "planning-receipt.json"

# Typed verdict codes shared by build_planning_receipt() and
# evaluate_mutation_authority() (#284 follow-up).
VERDICT_COMPLETE = "COMPLETE"
VERDICT_BLOCKED = "BLOCKED"
VERDICT_STALE_SOURCE = "STALE_SOURCE"
VERDICT_LEASE_LOST = "LEASE_LOST"
VERDICT_AWAITING_DECISION = "AWAITING_DECISION"
VERDICT_AUTHORITY_EXPIRED = "AUTHORITY_EXPIRED"

# Default mutation-authority TTL in seconds (30 minutes); callers may override
# or pass 0 to disable expiry entirely (omits `authority_expires_at`).
DEFAULT_AUTHORITY_TTL_SECONDS = 1800


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_hash(obj: Any) -> str:
    """Deterministic sha256 over a JSON-serializable structure."""
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def mutation_authority_token(*, run_id: str, attempt: int, task_contract_hash: str,
                            plan_hash: str, lease_id: str = "", fencing_token: str = "",
                            source_snapshot_hash: str = "", source_revision: str = "") -> str:
    """Derive the mutation-authority token from the exact identity tuple it authorizes.

    Any change to run/attempt/contract/plan/lease/fence/source-snapshot/source-revision
    changes the token, so an authority minted for one identity can never silently
    validate a different one. `source_snapshot_hash` defaults to `""` -- a run that
    never captures a GitHub source snapshot (local/non-GitHub sources) is unaffected.
    `source_revision` is a distinct, optional raw revision string (e.g. a GitHub issue
    `updated_at`) that a caller may fold in instead of/alongside the snapshot hash to
    get a `STALE_SOURCE` verdict from `evaluate_mutation_authority()`.
    """
    payload = {
        "run_id": str(run_id or ""),
        "attempt": int(attempt or 0),
        "task_contract_hash": str(task_contract_hash or ""),
        "plan_hash": str(plan_hash or ""),
        "lease_id": str(lease_id or ""),
        "fencing_token": str(fencing_token or ""),
        "source_snapshot_hash": str(source_snapshot_hash or ""),
        "source_revision": str(source_revision or ""),
    }
    return content_hash(payload)


def verify_mutation_authority(authority: str, *, run_id: str, attempt: int,
                              task_contract_hash: str, plan_hash: str,
                              lease_id: str = "", fencing_token: str = "",
                              source_snapshot_hash: str = "", source_revision: str = "") -> bool:
    """Fail-closed re-check: recompute the token from the CURRENT identity and compare."""
    if not str(authority or "").strip():
        return False
    expected = mutation_authority_token(
        run_id=run_id, attempt=attempt, task_contract_hash=task_contract_hash,
        plan_hash=plan_hash, lease_id=lease_id, fencing_token=fencing_token,
        source_snapshot_hash=source_snapshot_hash, source_revision=source_revision,
    )
    return expected == authority


def build_planning_receipt(
    *,
    run_id: str,
    attempt: int,
    contract: Mapping[str, Any],
    plan: Mapping[str, Any],
    plan_validation: Mapping[str, Any],
    lease_id: str = "",
    fencing_token: str = "",
    source_snapshot: Optional[Mapping[str, Any]] = None,
    intake: Optional[Mapping[str, Any]] = None,
    impact_map: Optional[Mapping[str, Any]] = None,
    traceability_matrix: Optional[Mapping[str, Any]] = None,
    plan_revision: int = 0,
    source_revision: str = "",
    awaiting_decision: bool = False,
    awaiting_reason: str = "",
    authority_ttl_seconds: int = DEFAULT_AUTHORITY_TTL_SECONDS,
) -> Dict[str, Any]:
    """Build the #284 planning receipt from already-computed intake artifacts.

    `plan_validation` is the dict returned by `plan_contract.validate_plan()` —
    this function does not re-derive plan/AC coverage, it packages the verdict
    already produced by the existing, tested validator into one hash-bound,
    immutable-once-ready receipt.

    `source_snapshot` is the optional `simplicio.source-snapshot/v1` dict from
    `source_snapshot.capture_github_issue_snapshot()`. When supplied, its
    `source.snapshot_hash` is folded into the mutation-authority identity tuple
    (source drift then invalidates the authority the same way a stale plan
    hash does) and the `source` block is embedded in the receipt for
    traceability. Omitting it keeps the receipt identical to before this
    increment.

    Three more artifacts are optional and strictly additive (#284 follow-up
    gaps): `intake` (a `simplicio.task-intake/v1` envelope from
    `intake_contract.build_task_intake()`), `impact_map` (a
    `simplicio.impact-audit/v1` result from `scripts/impact_audit.py audit`),
    and `traceability_matrix` (a `simplicio.ac-matrix/v1` result from
    `traceability_matrix.build_matrix()`). Each, when supplied, is folded into
    the receipt as a hash + summary for traceability. A `traceability_matrix`
    with a non-empty `gaps` list (an AC that needs a code change but has no
    test command and no declared evidence) makes the receipt NOT ready for
    mutation, the same fail-closed treatment as an invalid `plan_validation`
    — a caller that never builds a matrix is unaffected. `plan_revision`
    defaults to 0 (first planning pass); `replan_on_drift()` bumps it.

    `source_revision` is a distinct, optional raw revision string (e.g. a GitHub
    issue `updated_at`) folded into the mutation-authority identity tuple
    alongside `source_snapshot_hash`. `awaiting_decision`/`awaiting_reason` mark
    the receipt `AWAITING_DECISION` (never ready for mutation, even over an
    otherwise-valid plan) when an ambiguity needs a human call.
    `authority_ttl_seconds` (default `DEFAULT_AUTHORITY_TTL_SECONDS`, pass `0`
    to disable) stamps an `authority_expires_at` timestamp so the authority is
    not trusted indefinitely (`evaluate_mutation_authority()` returns
    `AUTHORITY_EXPIRED` once it elapses).
    """
    task_contract_hash = str(contract.get("collection_hash") or content_hash(contract))
    plan_hash = content_hash(plan)
    plan_valid = bool(plan_validation.get("valid"))
    matrix_ok = True if traceability_matrix is None else bool(traceability_matrix.get("coverage_ok"))
    plan_ready = plan_valid and matrix_ok

    if awaiting_decision:
        verdict = VERDICT_AWAITING_DECISION
        ready = False
    elif not plan_ready:
        verdict = VERDICT_BLOCKED
        ready = False
    else:
        verdict = VERDICT_COMPLETE
        ready = True

    source = dict((source_snapshot or {}).get("source") or {})
    source_snapshot_hash = str(source.get("snapshot_hash") or "")
    authority = ""
    authority_expires_at = ""
    if ready:
        authority = mutation_authority_token(
            run_id=run_id, attempt=attempt, task_contract_hash=task_contract_hash,
            plan_hash=plan_hash, lease_id=lease_id, fencing_token=fencing_token,
            source_snapshot_hash=source_snapshot_hash, source_revision=source_revision,
        )
        if authority_ttl_seconds and authority_ttl_seconds > 0:
            expires_ts = time.time() + authority_ttl_seconds
            authority_expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires_ts))
    receipt: Dict[str, Any] = {
        "schema": PLANNING_RECEIPT_SCHEMA,
        "run_id": str(run_id or ""),
        "attempt": int(attempt or 0),
        "lease_id": str(lease_id or ""),
        "fencing_token": str(fencing_token or ""),
        "source_revision": str(source_revision or ""),
        "task_contract_hash": task_contract_hash,
        "plan_hash": plan_hash,
        "plan_revision": int(plan_revision or 0),
        "plan_validation": {
            "valid": plan_valid,
            "errors": list(plan_validation.get("errors") or []),
            "warnings": list(plan_validation.get("warnings") or []),
            "checked_tasks": plan_validation.get("checked_tasks", 0),
        },
        "awaiting_decision": bool(awaiting_decision),
        "awaiting_reason": str(awaiting_reason or ""),
        "verdict": verdict,
        "ready_for_mutation": ready,
        "mutation_authority": authority,
        "authority_expires_at": authority_expires_at,
    }
    if source:
        receipt["source"] = source
    if intake:
        receipt["intake_hash"] = str(intake.get("intake_hash") or content_hash(intake))
        receipt["intake_summary"] = {
            "acceptance_criteria": len(intake.get("acceptance_criteria") or []),
            "delivery_target": (intake.get("understanding") or {}).get("delivery_target", ""),
        }
    if impact_map:
        receipt["impact_map_hash"] = content_hash(impact_map)
        receipt["impact_map_summary"] = dict(impact_map.get("counts") or {})
    if traceability_matrix:
        receipt["traceability_matrix_hash"] = str(
            traceability_matrix.get("matrix_hash") or content_hash(traceability_matrix)
        )
        receipt["traceability_summary"] = {
            "coverage_ok": matrix_ok,
            "gaps": list(traceability_matrix.get("gaps") or []),
            "counts": dict(traceability_matrix.get("counts") or {}),
        }
    return receipt


def receipt_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / RECEIPT_FILENAME


def load_planning_receipt(run_dir: str | Path) -> Dict[str, Any] | None:
    path = receipt_path(run_dir)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def evaluate_mutation_authority(
    run_dir: str | Path, *, run_id: str, attempt: int, task_contract_hash: str,
    plan_hash: str, lease_id: str = "", fencing_token: str = "",
    source_snapshot_hash: str = "",
    source_revision: str = "", current_source_revision: str = "",
) -> Dict[str, Any]:
    """Fail-closed gate: load the on-disk receipt and re-verify its authority
    against the CURRENT identity tuple the caller is about to execute with.

    `source_snapshot_hash`, when supplied, must match the hash the receipt was
    minted with -- a GitHub issue edited between planning and execution (source
    drift) invalidates the authority exactly like a stale plan hash does.
    `current_source_revision`, when supplied, is compared against the receipt's
    own `source_revision` for the same drift check via a distinct raw revision
    string, returning `STALE_SOURCE` on mismatch.

    Returns a structured verdict (never raises) so callers can render a clear
    BLOCKED reason instead of a bare exception. Every response carries a typed
    `verdict` (`COMPLETE`/`BLOCKED`/`STALE_SOURCE`/`LEASE_LOST`/
    `AWAITING_DECISION`/`AUTHORITY_EXPIRED`) alongside the legacy `reason_code`.
    """
    receipt = load_planning_receipt(run_dir)
    if not receipt:
        return {"ok": False, "verdict": VERDICT_BLOCKED,
                "reason_code": "planning_receipt_missing",
                "reason": f"{RECEIPT_FILENAME} is missing or unreadable"}
    if receipt.get("schema") != PLANNING_RECEIPT_SCHEMA:
        return {"ok": False, "verdict": VERDICT_BLOCKED,
                "reason_code": "planning_receipt_schema_invalid",
                "reason": "planning receipt schema mismatch"}

    # AWAITING_DECISION gate (checked before ready_for_mutation so the reason is clear).
    if receipt.get("awaiting_decision"):
        return {"ok": False, "verdict": VERDICT_AWAITING_DECISION,
                "reason_code": "planning_awaiting_decision",
                "reason": str(receipt.get("awaiting_reason") or "planning blocked — awaiting human decision")}

    if not receipt.get("ready_for_mutation"):
        return {"ok": False, "verdict": VERDICT_BLOCKED,
                "reason_code": "planning_not_ready",
                "reason": "planning receipt is not ready_for_mutation"}

    # AUTHORITY_EXPIRED gate.
    expires_at = str(receipt.get("authority_expires_at") or "")
    if expires_at:
        try:
            # calendar.timegm (not time.mktime) so the UTC string is parsed
            # correctly regardless of the local timezone.
            expiry_ts = calendar.timegm(time.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ"))
            if time.time() > expiry_ts:
                return {"ok": False, "verdict": VERDICT_AUTHORITY_EXPIRED,
                        "reason_code": "authority_expired",
                        "reason": f"mutation authority expired at {expires_at}; replanning required"}
        except (ValueError, OverflowError):
            pass  # malformed timestamp — don't block on our own formatting error

    # STALE_SOURCE gate (distinct raw source_revision, independent of source_snapshot_hash).
    if current_source_revision:
        recorded_revision = str(receipt.get("source_revision") or "")
        if recorded_revision and recorded_revision != current_source_revision:
            return {"ok": False, "verdict": VERDICT_STALE_SOURCE,
                    "reason_code": "source_revision_changed",
                    "reason": (
                        f"source revision changed since planning "
                        f"(recorded={recorded_revision!r}, current={current_source_revision!r}); "
                        "replanning required"
                    )}

    authority = str(receipt.get("mutation_authority") or "")
    receipt_source_hash = str((receipt.get("source") or {}).get("snapshot_hash") or "")
    receipt_source_revision = str(receipt.get("source_revision") or "")
    if not verify_mutation_authority(
        authority, run_id=run_id, attempt=attempt, task_contract_hash=task_contract_hash,
        plan_hash=plan_hash, lease_id=lease_id, fencing_token=fencing_token,
        source_snapshot_hash=receipt_source_hash, source_revision=receipt_source_revision,
    ):
        receipt_lease = str(receipt.get("lease_id") or "")
        receipt_fence = str(receipt.get("fencing_token") or "")
        if (receipt_lease and receipt_lease != lease_id) or \
                (receipt_fence and receipt_fence != fencing_token):
            return {"ok": False, "verdict": VERDICT_LEASE_LOST,
                    "reason_code": "lease_or_fence_mismatch",
                    "reason": "lease_id or fencing_token changed since planning; another worker may hold this item"}
        return {"ok": False, "verdict": VERDICT_BLOCKED,
                "reason_code": "mutation_authority_invalid",
                "reason": "mutation authority does not match the current run/attempt/contract/plan/lease/fence identity"}
    if source_snapshot_hash and receipt_source_hash and source_snapshot_hash != receipt_source_hash:
        return {"ok": False, "verdict": VERDICT_STALE_SOURCE,
                "reason_code": "source_drift",
                "reason": "GitHub source snapshot changed since planning; authority invalidated"}
    return {"ok": True, "verdict": VERDICT_COMPLETE,
            "reason_code": "mutation_authority_verified",
            "reason": "mutation authority verified for the current identity tuple"}


def replan_on_drift(
    run_dir: str | Path,
    *,
    run_id: str,
    attempt: int,
    contract: Mapping[str, Any],
    plan: Mapping[str, Any],
    plan_validation: Mapping[str, Any],
    lease_id: str = "",
    fencing_token: str = "",
    baseline_source_snapshot: Optional[Mapping[str, Any]] = None,
    current_source_snapshot: Optional[Mapping[str, Any]] = None,
    intake: Optional[Mapping[str, Any]] = None,
    impact_map: Optional[Mapping[str, Any]] = None,
    traceability_matrix: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """#284 Fase 6 -- genuine replanning/diff-on-drift.

    `evaluate_mutation_authority()` blocks fail-closed forever once source or
    repo drift is detected: it never re-plans, it just keeps saying no. This
    function is the missing recovery path: given a FRESH `contract`/`plan`
    (already re-derived by the caller against the current source/repo state)
    plus the freshest source snapshot, it builds a brand-new planning receipt,
    bumps `plan_revision` past whatever the previous on-disk receipt carried,
    and records a semantic diff (`replan.diff`) of exactly what changed --
    task-contract hash, plan hash, and source-snapshot hash -- instead of
    silently discarding the history of why a replan happened.

    `baseline_source_snapshot`/`current_source_snapshot` are compared via
    `source_snapshot.detect_source_drift()`; both may be omitted for a replan
    driven purely by repo drift (`plan_validation` already re-checked repo
    state via `plan_contract.validate_plan(..., current_state=...)`).

    This never removes or weakens an AC: it is a thin wrapper around
    `build_planning_receipt()` with the SAME contract passed in by the caller
    -- issue #284's rule that a replan may only add, never silently drop, an
    explicit acceptance criterion is enforced by the caller supplying a
    contract that still carries every `origin=source` AC, not by this
    function inventing one.
    """
    from .source_snapshot import detect_source_drift

    previous = load_planning_receipt(run_dir)
    previous_revision = int((previous or {}).get("plan_revision") or 0)
    drift = detect_source_drift(baseline_source_snapshot, current_source_snapshot)

    new_receipt = build_planning_receipt(
        run_id=run_id, attempt=attempt, contract=contract, plan=plan,
        plan_validation=plan_validation, lease_id=lease_id, fencing_token=fencing_token,
        source_snapshot=current_source_snapshot, intake=intake, impact_map=impact_map,
        traceability_matrix=traceability_matrix,
        plan_revision=previous_revision + 1 if previous else previous_revision,
    )

    prev_contract_hash = str((previous or {}).get("task_contract_hash") or "")
    prev_plan_hash = str((previous or {}).get("plan_hash") or "")
    new_receipt["replan"] = {
        "replanned": bool(previous),
        "drift_detected": bool(drift["drifted"]),
        "drift_reason_code": drift["reason_code"],
        "previous_revision": previous_revision,
        "diff": {
            "task_contract_changed": bool(previous) and prev_contract_hash != new_receipt["task_contract_hash"],
            "plan_changed": bool(previous) and prev_plan_hash != new_receipt["plan_hash"],
            "previous_task_contract_hash": prev_contract_hash,
            "previous_plan_hash": prev_plan_hash,
            "source_snapshot_before": drift["before"],
            "source_snapshot_after": drift["after"],
        },
    }

    path = receipt_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(new_receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return new_receipt


def publish_planning_receipt(
    receipt: Mapping[str, Any],
    *,
    publish_comment_fn: Any,
    runner: Any = None,
    timeout: int = 20,
    require_active: Any = None,
    outbox_dir: Optional[str | Path] = None,
    **render_kwargs: Any,
) -> Optional[Dict[str, Any]]:
    """Wire the #284 planning receipt into the #285 canonical GitHub status comment.

    Closes the remaining #284 gap "wiring github_lifecycle.py's comment publish into
    the gate itself": a receipt with `ready_for_mutation=True` is projected as a
    `PLANNED` status update on the SAME canonical comment `github_lifecycle.py`
    already uses for `CLAIMED` (idempotent create-or-update, re-query verified); a
    receipt that is NOT ready is projected as `BLOCKED` with its validator errors as
    blockers, instead of the issue silently staying on whatever state it was last in.

    Returns ``None`` (no-op) when the receipt carries no GitHub `source` block --
    the #284 requirement that non-GitHub/local sources register
    `source_sync=not_applicable` rather than fake a publish. Otherwise returns the
    `simplicio.github-lifecycle-receipt/v1` produced by
    `github_lifecycle.publish_lifecycle_state()` (never raises for an ordinary
    publish/re-query mismatch -- that surfaces as `verified: False` in the receipt --
    but a transport failure from `publish_comment_fn` propagates, fail-closed).
    """
    from . import github_lifecycle as _gh  # local import: no import cycle with runner.py

    source = dict(receipt.get("source") or {})
    if source.get("provider") != "github":
        return None
    owner_repo = str(source.get("repo") or "")
    issue = str(source.get("item_id") or "")
    if "/" not in owner_repo or not issue:
        return None
    owner, repo_name = owner_repo.split("/", 1)
    ready = bool(receipt.get("ready_for_mutation"))
    state = "PLANNED" if ready else "BLOCKED"
    render_kwargs = dict(render_kwargs)
    if not ready:
        errors = list((receipt.get("plan_validation") or {}).get("errors") or [])
        render_kwargs.setdefault("blockers", errors or ["planning gate: ready_for_mutation is False"])
    kwargs: Dict[str, Any] = dict(
        owner=owner, repo=repo_name, issue=issue, state=state,
        run_id=str(receipt.get("run_id") or ""), attempt_id=str(receipt.get("attempt") or ""),
        fencing_token=str(receipt.get("fencing_token") or ""),
        publish_comment_fn=publish_comment_fn, timeout=timeout,
        require_active=require_active, outbox_dir=outbox_dir,
        **render_kwargs,
    )
    if runner is not None:
        kwargs["runner"] = runner
    return _gh.publish_lifecycle_state(**kwargs)


def mutation_authority_required(env: Optional[Mapping[str, str]] = None) -> bool:
    """Mandatory-by-default (#284): the mutation-authority gate is ON unless the caller
    explicitly opts out via `SIMPLICIO_REQUIRE_MUTATION_AUTHORITY=0/false/no/off/legacy`.

    Historically this env var was opt-IN (default off). Flipping the polarity is the
    #284 DoD item "execute_operator() e batch recusam execução sem mutation authority
    válida" -- unconditionally, not only when a caller remembered to turn it on. The
    same name is kept (only the default changes) so existing opt-in `=1` deployments
    are unaffected; a legacy caller that truly cannot satisfy the gate yet must now
    set the var to an explicit falsy value instead of just never setting it.
    """
    import os as _os
    raw = (env if env is not None else _os.environ).get("SIMPLICIO_REQUIRE_MUTATION_AUTHORITY")
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "legacy")


def auto_planning_receipt_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """Mandatory-by-default (#284 follow-up): the real ``arm_run()`` dispatch path
    self-builds ``planning-receipt.json`` unless the caller explicitly opts out via
    ``SIMPLICIO_LOOP_AUTO_PLANNING_RECEIPT=0/false/no/off/legacy`` -- the exact same
    polarity-flip pattern ``mutation_authority_required()`` used for
    ``SIMPLICIO_REQUIRE_MUTATION_AUTHORITY`` (#284/#360).

    Historically this env var was opt-IN (default off), because dozens of tests
    deliberately exercised the mutation-authority gate's fail-closed behavior with
    NO receipt on disk. Those tests now stage a receipt explicitly via
    ``tests/planning_gate_fixtures.py`` (or a fixture-local
    ``SIMPLICIO_LOOP_AUTO_PLANNING_RECEIPT=0`` opt-out) when they intend to assert
    the missing-receipt path; every other real dispatch is safer with the receipt
    auto-built than without one. Unrecognized values are treated as "on" (fail
    closed toward requiring the receipt), matching ``mutation_authority_required()``.
    """
    import os as _os
    raw = (env if env is not None else _os.environ).get("SIMPLICIO_LOOP_AUTO_PLANNING_RECEIPT")
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "legacy")


__all__ = [
    "PLANNING_RECEIPT_SCHEMA",
    "RECEIPT_FILENAME",
    "VERDICT_COMPLETE",
    "VERDICT_BLOCKED",
    "VERDICT_STALE_SOURCE",
    "VERDICT_LEASE_LOST",
    "VERDICT_AWAITING_DECISION",
    "VERDICT_AUTHORITY_EXPIRED",
    "DEFAULT_AUTHORITY_TTL_SECONDS",
    "content_hash",
    "mutation_authority_token",
    "verify_mutation_authority",
    "build_planning_receipt",
    "receipt_path",
    "load_planning_receipt",
    "evaluate_mutation_authority",
    "mutation_authority_required",
    "auto_planning_receipt_enabled",
    "publish_planning_receipt",
    "replan_on_drift",
]
