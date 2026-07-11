"""Acceptance-criterion evidence and crash-safe phase-event recovery.

This module is deliberately transport agnostic.  A Runtime may persist the cursor in
its own store, while the local loop uses the same JSON envelope and reconciliation rules.
The important property is fail-closed recovery: duplicates are idempotent, but altered
duplicates, gaps, identity drift, and incomplete AC evidence are conflicts.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .phase_events import TERMINAL_PHASES, PhaseEventError, validate_phase_event

CURSOR_SCHEMA = "simplicio.loop-cursor/v1"
AC_RECEIPT_SCHEMA = "simplicio.ac-evidence-receipt/v1"
RECOVERY_SCHEMA = "simplicio.loop-recovery/v1"
CLAIM_TYPES = frozenset(("measured", "replayed", "benchmarked", "estimated"))


class RecoveryError(ValueError):
    """A recovery or evidence contract violation; callers must block, never guess."""


def _recovery_block(reason_code: str, detail: str, *, next_action: str = "inspect") -> Dict[str, Any]:
    """Build a machine-readable fail-closed outcome.

    Recovery is often called from a process-start path where raising an exception
    would lose the diagnostic (and could accidentally trigger a fresh attempt).  The
    orchestration helper below therefore converts every conflict into this explicit
    envelope while the lower-level cursor API remains strict for callers that want
    exceptions.
    """
    return {
        "schema": RECOVERY_SCHEMA,
        "status": "BLOCKED",
        "execution_allowed": False,
        "reason_code": reason_code,
        "detail": detail,
        "next_action": next_action,
        "conflicts": [{"code": reason_code, "detail": detail}],
    }


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RecoveryError("%s must be a non-empty string" % field)
    return value.strip()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def build_ac_evidence_receipt(*, run_id: str, work_item_id: str, attempt_id: str,
                              actor: str, environment_id: str,
                              criteria: Sequence[Mapping[str, Any]],
                              observed_at: str, challenge_id: str = "") -> Dict[str, Any]:
    """Build a receipt where every AC points to a concrete command/artifact.

    Each criterion entry requires ``id``, ``status`` (normally ``verified``), and at
    least one evidence item containing command, exit code, artifact hash and provenance.
    ``claim_type`` is explicit so estimated claims cannot silently masquerade as tests.
    """
    payload: Dict[str, Any] = {
        "schema": AC_RECEIPT_SCHEMA,
        "run_id": _text(run_id, "run_id"),
        "work_item_id": _text(work_item_id, "work_item_id"),
        "attempt_id": _text(attempt_id, "attempt_id"),
        "actor": _text(actor, "actor"),
        "environment_id": _text(environment_id, "environment_id"),
        "observed_at": _text(observed_at, "observed_at"),
        "challenge_id": challenge_id.strip() if isinstance(challenge_id, str) else "",
        "criteria": [dict(item) for item in criteria],
    }
    payload["receipt_hash"] = _sha256({key: value for key, value in payload.items()
                                       if key != "receipt_hash"})
    return validate_ac_evidence_receipt(payload)


def validate_ac_evidence_receipt(receipt: Mapping[str, Any], *,
                                 required_criteria: Optional[Iterable[str]] = None,
                                 expected_identity: Optional[Mapping[str, str]] = None
                                 ) -> Dict[str, Any]:
    """Validate an AC receipt and return a normalized copy.

    Validation is intentionally strict.  Missing ACs, duplicate AC rows, changed
    artifact hashes, non-zero commands, and estimated-only evidence are not valid proof.
    """
    if not isinstance(receipt, Mapping) or receipt.get("schema") != AC_RECEIPT_SCHEMA:
        raise RecoveryError("unsupported AC evidence receipt schema")
    normalized = dict(receipt)
    for field in ("run_id", "work_item_id", "attempt_id", "actor", "environment_id", "observed_at"):
        _text(normalized.get(field), field)
    criteria = normalized.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        raise RecoveryError("criteria must be a non-empty list")
    expected = set(str(item) for item in (required_criteria or ()))
    seen = set()
    for criterion in criteria:
        if not isinstance(criterion, Mapping):
            raise RecoveryError("criterion entry must be an object")
        criterion_id = _text(criterion.get("id"), "criterion.id")
        if criterion_id in seen:
            raise RecoveryError("duplicate criterion: %s" % criterion_id)
        seen.add(criterion_id)
        if criterion.get("status") != "verified":
            raise RecoveryError("criterion %s is not verified" % criterion_id)
        evidence = criterion.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise RecoveryError("criterion %s has no evidence" % criterion_id)
        valid_item = False
        for item in evidence:
            if not isinstance(item, Mapping):
                raise RecoveryError("evidence for %s must be an object" % criterion_id)
            _text(item.get("command"), "evidence.command")
            exit_code = item.get("exit_code")
            if isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code != 0:
                raise RecoveryError("evidence for %s did not exit zero" % criterion_id)
            artifact_hash = _text(item.get("artifact_hash"), "evidence.artifact_hash")
            if len(artifact_hash) != 64 or any(ch not in "0123456789abcdef" for ch in artifact_hash.lower()):
                raise RecoveryError("evidence for %s has invalid artifact hash" % criterion_id)
            _text(item.get("provenance"), "evidence.provenance")
            claim_type = _text(item.get("claim_type"), "evidence.claim_type").lower()
            if claim_type not in CLAIM_TYPES:
                raise RecoveryError("unsupported claim type: %s" % claim_type)
            valid_item = claim_type != "estimated"
        if not valid_item:
            raise RecoveryError("criterion %s has no reproducible evidence" % criterion_id)
    if expected and seen != expected:
        missing = sorted(expected - seen)
        extra = sorted(seen - expected)
        raise RecoveryError("AC set mismatch (missing=%s extra=%s)" % (missing, extra))
    if expected_identity:
        for field, value in expected_identity.items():
            if field in normalized and normalized[field] != value:
                raise RecoveryError("identity mismatch for %s" % field)
    expected_hash = _sha256({key: value for key, value in normalized.items() if key != "receipt_hash"})
    if normalized.get("receipt_hash") != expected_hash:
        raise RecoveryError("receipt hash mismatch")
    return normalized


def build_cursor(*, run_id: str, work_item_id: str, attempt_id: str, actor: str,
                 environment_id: str, last_sequence: int = 0,
                 applied_event_ids: Optional[Sequence[str]] = None,
                 projection_hash: str = "") -> Dict[str, Any]:
    """Create the persisted, replayable cursor envelope."""
    if isinstance(last_sequence, bool) or not isinstance(last_sequence, int) or last_sequence < 0:
        raise RecoveryError("last_sequence must be a non-negative integer")
    cursor = {
        "schema": CURSOR_SCHEMA, "run_id": _text(run_id, "run_id"),
        "work_item_id": _text(work_item_id, "work_item_id"), "attempt_id": _text(attempt_id, "attempt_id"),
        "actor": _text(actor, "actor"), "environment_id": _text(environment_id, "environment_id"),
        "last_sequence": last_sequence, "applied_event_ids": list(applied_event_ids or ()),
        "projection_hash": projection_hash or "", "terminal": False,
    }
    return cursor


def _validate_cursor(cursor: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(cursor, Mapping) or cursor.get("schema") != CURSOR_SCHEMA:
        raise RecoveryError("unsupported cursor schema")
    normalized = dict(cursor)
    for field in ("run_id", "work_item_id", "attempt_id", "actor", "environment_id"):
        _text(normalized.get(field), field)
    seq = normalized.get("last_sequence")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise RecoveryError("cursor last_sequence must be a non-negative integer")
    ids = normalized.get("applied_event_ids")
    if not isinstance(ids, list) or any(not isinstance(item, str) or not item for item in ids):
        raise RecoveryError("cursor applied_event_ids must be a list of strings")
    return normalized


def reconcile_after_crash(events: Iterable[Mapping[str, Any]], cursor: Mapping[str, Any]
                          ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Replay events after a crash, returning ``(cursor, diagnostics)``.

    Events at or below the cursor are accepted only when their IDs and complete envelopes
    match the acknowledged history.  A terminal cursor never schedules work again.
    """
    current = _validate_cursor(cursor)
    identity = {key: current[key] for key in ("run_id", "work_item_id", "attempt_id", "actor")}
    by_id: Dict[str, Dict[str, Any]] = {}
    for raw in events:
        event = validate_phase_event(raw)
        for key, value in identity.items():
            if event.get(key) != value:
                raise RecoveryError("event identity mismatch for %s" % key)
        previous = by_id.get(event["event_id"])
        if previous is not None and previous != event:
            raise RecoveryError("conflicting duplicate event_id: %s" % event["event_id"])
        by_id[event["event_id"]] = event
    ordered = sorted(by_id.values(), key=lambda item: (item["sequence"], item["event_id"]))
    known = set(current["applied_event_ids"])
    applied: List[Dict[str, Any]] = []
    expected = current["last_sequence"] + 1
    for event in ordered:
        seq = event["sequence"]
        if seq <= current["last_sequence"]:
            if event["event_id"] not in known:
                raise RecoveryError("acknowledged sequence has unknown event: %s" % event["event_id"])
            continue
        if seq != expected:
            raise RecoveryError("sequence gap at %s (expected %d)" % (event["event_id"], expected))
        applied.append(event)
        known.add(event["event_id"])
        current["last_sequence"] = seq
        expected += 1
    current["applied_event_ids"] = sorted(known)
    if applied:
        current["projection_hash"] = _sha256(applied if not current.get("projection_hash") else {
            "previous": current["projection_hash"], "events": applied})
        current["terminal"] = applied[-1]["to_phase"] in TERMINAL_PHASES
    diagnostics = {
        "status": "complete" if current.get("terminal") else ("resumed" if applied else "unchanged"),
        "applied_sequences": [event["sequence"] for event in applied],
        "replayed_event_ids": [event["event_id"] for event in ordered if event["sequence"] <= cursor["last_sequence"]],
        "next_sequence": current["last_sequence"] + 1,
        "execution_allowed": not bool(current.get("terminal")),
    }
    return current, diagnostics


def recover_after_crash(
    events: Iterable[Mapping[str, Any]],
    cursor: Mapping[str, Any],
    *,
    source_state: Optional[Mapping[str, Any]] = None,
    runtime_reconcile: Optional[Callable[[], Mapping[str, Any]]] = None,
    lease: Optional[Mapping[str, Any]] = None,
    provider_identity: Optional[Mapping[str, Any]] = None,
    persist_path: Optional[Union[os.PathLike, str]] = None,
) -> Dict[str, Any]:
    """Perform one complete, fail-closed recovery handshake.

    ``reconcile_after_crash`` is the strict event primitive.  This orchestration
    boundary adds the checks that a restarted host must perform before scheduling
    work again: runtime outbox reconciliation, live source re-query, lease/agent
    ownership and provider/environment identity.  Every negative result is a
    structured ``BLOCKED`` or ``RECLAIM_REQUIRED`` outcome; callers must not start a
    new attempt from an exception or from a stale local cursor.

    ``source_state`` is a live re-query envelope with ``status`` in ``open``,
    ``done``/``closed``, ``blocked`` or ``unknown``.  ``runtime_reconcile`` is a
    zero-argument callback so adapters can perform their own transport-specific
    outbox replay without this module importing a Runtime implementation.
    """
    try:
        normalized_cursor = _validate_cursor(cursor)
    except RecoveryError as exc:
        return _recovery_block("cursor_invalid", str(exc))

    expected_identity = {
        "run_id": normalized_cursor["run_id"],
        "work_item_id": normalized_cursor["work_item_id"],
        "attempt_id": normalized_cursor["attempt_id"],
        "actor": normalized_cursor["actor"],
        "environment_id": normalized_cursor["environment_id"],
    }
    if provider_identity is not None:
        if not isinstance(provider_identity, Mapping):
            return _recovery_block("provider_identity_invalid", "provider identity must be an object")
        for field in ("actor", "environment_id"):
            supplied = provider_identity.get(field)
            if supplied is not None and str(supplied) != expected_identity[field]:
                return _recovery_block("identity_drift", "%s differs from persisted cursor" % field,
                                       next_action="handoff")

    runtime_result: Dict[str, Any] = {"status": "SKIPPED", "pending": 0}
    if runtime_reconcile is not None:
        try:
            raw_runtime = runtime_reconcile()
            if not isinstance(raw_runtime, Mapping):
                return _recovery_block("runtime_reconcile_invalid", "runtime reconciliation did not return an object")
            runtime_result = dict(raw_runtime)
        except Exception as exc:  # transport errors must not trigger duplicate work
            return _recovery_block("runtime_unavailable", str(exc), next_action="retry_runtime")
        runtime_status = str(runtime_result.get("status") or "").upper()
        pending = runtime_result.get("pending", 0)
        if runtime_status not in {"MEASURED", "OK", "DELIVERED"} or (
            isinstance(pending, int) and not isinstance(pending, bool) and pending > 0
        ):
            return _recovery_block("runtime_reconcile_pending",
                                   "runtime outbox is not fully reconciled",
                                   next_action="retry_runtime")

    try:
        recovered_cursor, diagnostics = reconcile_after_crash(events, normalized_cursor)
    except (RecoveryError, PhaseEventError) as exc:
        return _recovery_block("event_conflict", str(exc), next_action="inspect_events")

    source_result: Dict[str, Any] = {"status": "SKIPPED"}
    if source_state is not None:
        if not isinstance(source_state, Mapping):
            return _recovery_block("source_invalid", "source re-query did not return an object")
        source_result = dict(source_state)
        status = str(source_result.get("status") or "unknown").strip().lower()
        if status not in {"open", "done", "closed", "blocked", "unknown"}:
            return _recovery_block("source_invalid", "unsupported source status: %s" % status)
        for field in ("run_id", "work_item_id"):
            if field in source_result and str(source_result[field]) != expected_identity[field]:
                return _recovery_block("source_identity_drift", "%s differs from persisted cursor" % field)
        if status == "unknown":
            return _recovery_block("source_unavailable", "live source state is unknown", next_action="requery_source")
        # A source that says done while the local event stream is non-terminal is
        # ambiguous: never re-run or force the cursor forward based on that fact.
        if status in {"done", "closed"} and not recovered_cursor.get("terminal"):
            return _recovery_block("source_local_conflict", "source is terminal but local cursor is not",
                                   next_action="inspect_runtime")
        if recovered_cursor.get("terminal") and status not in {"done", "closed"}:
            return _recovery_block("terminal_source_conflict", "local cursor is terminal but source is not",
                                   next_action="requery_source")

    if lease is not None:
        if not isinstance(lease, Mapping):
            return _recovery_block("lease_invalid", "lease state must be an object")
        lease_state = str(lease.get("state") or "").strip().lower()
        owner = str(lease.get("owner") or lease.get("actor") or "").strip()
        if lease_state in {"active", "held"} and owner and owner != expected_identity["actor"]:
            return _recovery_block("lease_owned", "lease is held by another actor", next_action="wait_or_reclaim")
        if lease_state in {"expired", "missing", "reclaim_required"}:
            return {
                "schema": RECOVERY_SCHEMA, "status": "RECLAIM_REQUIRED", "execution_allowed": False,
                "reason_code": "lease_reclaim_required", "detail": "lease must be reclaimed before resume",
                "next_action": "reclaim_lease", "conflicts": [], "cursor": recovered_cursor,
                "diagnostics": diagnostics, "runtime": runtime_result, "source": source_result,
            }
        if lease_state not in {"", "active", "held", "owned"}:
            return _recovery_block("lease_invalid", "unsupported lease state: %s" % lease_state)

    if persist_path is not None:
        try:
            persist_cursor(persist_path, recovered_cursor)
        except (OSError, RecoveryError) as exc:
            return _recovery_block("cursor_persist_failed", str(exc), next_action="retry_persist")

    status = "COMPLETE" if recovered_cursor.get("terminal") else (
        "RESUMED" if diagnostics.get("applied_sequences") else "UNCHANGED"
    )
    return {
        "schema": RECOVERY_SCHEMA,
        "status": status,
        "execution_allowed": not bool(recovered_cursor.get("terminal")),
        "reason_code": "terminal_cursor" if recovered_cursor.get("terminal") else "cursor_reconciled",
        "next_action": "requery_source" if recovered_cursor.get("terminal") else "resume_attempt",
        "conflicts": [],
        "cursor": recovered_cursor,
        "diagnostics": diagnostics,
        "runtime": runtime_result,
        "source": source_result,
    }


def persist_cursor(path: Union[os.PathLike, str], cursor: Mapping[str, Any]) -> Path:
    """Atomically persist a validated cursor and fsync both file and replacement."""
    normalized = _validate_cursor(cursor)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".%s." % target.name, dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(normalized, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
        try:
            directory_fd = os.open(str(target.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return target


__all__ = ["AC_RECEIPT_SCHEMA", "CLAIM_TYPES", "CURSOR_SCHEMA", "RECOVERY_SCHEMA", "RecoveryError",
           "build_ac_evidence_receipt", "build_cursor", "persist_cursor",
           "reconcile_after_crash", "recover_after_crash", "validate_ac_evidence_receipt"]
