"""Typed Loop phase events and deterministic Runtime projection.

The Loop owns the protocol transition; the Runtime owns the board projection.
This module is intentionally transport agnostic so a local journal and a remote
Runtime can exchange the same event envelope and reconcile it after a disconnect.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

SCHEMA = "simplicio.loop-event/v1"
CONTRACT_VERSION = "1"

PHASES: Tuple[str, ...] = (
    "intake", "mapping", "planning", "executing", "validating", "watching", "delivering", "done",
)
TERMINAL_PHASES = frozenset(("done", "partial", "blocked", "cancelled"))
PAUSED_PHASES = frozenset(("awaiting_decision",))
BOARD_STATES = frozenset(("queued", "mapped", "planned", "in_progress", "verifying", "observing", "delivering", "completed", "partial", "blocked", "cancelled", "awaiting_decision"))

# Explicit edges make accidental jumps (e.g. intake -> done) fail closed.
ALLOWED_TRANSITIONS = {
    "intake": frozenset(("mapping", "cancelled", "blocked")),
    "mapping": frozenset(("planning", "cancelled", "blocked")),
    "planning": frozenset(("executing", "awaiting_decision", "cancelled", "blocked")),
    "executing": frozenset(("validating", "awaiting_decision", "cancelled", "blocked")),
    "validating": frozenset(("watching", "delivering", "executing", "awaiting_decision", "cancelled", "blocked")),
    "watching": frozenset(("delivering", "executing", "awaiting_decision", "cancelled", "blocked")),
    "delivering": frozenset(("done", "partial", "executing", "awaiting_decision", "cancelled", "blocked")),
    "awaiting_decision": frozenset(("planning", "executing", "watching", "delivering", "cancelled", "blocked")),
}

_BOARD_BY_PHASE = {
    "intake": "queued", "mapping": "mapped", "planning": "planned", "executing": "in_progress",
    "validating": "verifying", "watching": "observing", "delivering": "delivering", "done": "completed",
    "partial": "partial", "blocked": "blocked", "cancelled": "cancelled", "awaiting_decision": "awaiting_decision",
}


class PhaseEventError(ValueError):
    """Raised when a phase event is malformed or violates the state machine."""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PhaseEventError("%s must be a non-empty string" % field)
    return value.strip()


def phase_to_board_state(phase: str) -> str:
    """Derive a board state; callers must not assign cosmetic columns directly."""
    phase = _text(phase, "phase")
    try:
        return _BOARD_BY_PHASE[phase]
    except KeyError as exc:
        raise PhaseEventError("unknown phase: %s" % phase) from exc


def build_phase_event(*, run_id: str, work_item_id: str, actor: str, cause: str,
                      sequence: int, event_id: str, from_phase: Optional[str], to_phase: str,
                      attempt_id: Optional[str] = None, causation_id: Optional[str] = None,
                      reason_code: str = "phase_transition", payload: Optional[Mapping[str, Any]] = None,
                      observed_at: Optional[str] = None) -> Dict[str, Any]:
    """Build and validate one canonical transition envelope."""
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise PhaseEventError("sequence must be a positive integer")
    to_phase = _text(to_phase, "to_phase")
    if to_phase not in _BOARD_BY_PHASE:
        raise PhaseEventError("unknown phase: %s" % to_phase)
    if from_phase is not None:
        from_phase = _text(from_phase, "from_phase")
        if from_phase not in _BOARD_BY_PHASE:
            raise PhaseEventError("unknown phase: %s" % from_phase)
        if to_phase not in ALLOWED_TRANSITIONS.get(from_phase, frozenset()):
            raise PhaseEventError("invalid transition: %s -> %s" % (from_phase, to_phase))
    event: Dict[str, Any] = {
        "schema": SCHEMA, "contract_version": CONTRACT_VERSION, "event_id": _text(event_id, "event_id"),
        "sequence": sequence, "run_id": _text(run_id, "run_id"), "work_item_id": _text(work_item_id, "work_item_id"),
        "actor": _text(actor, "actor"), "cause": _text(cause, "cause"), "causation_id": _text(causation_id or event_id, "causation_id"),
        "reason_code": _text(reason_code, "reason_code"), "from_phase": from_phase, "to_phase": to_phase,
        "board_state": phase_to_board_state(to_phase), "payload": dict(payload or {}),
    }
    if attempt_id is not None:
        event["attempt_id"] = _text(attempt_id, "attempt_id")
    if observed_at is not None:
        event["observed_at"] = _text(observed_at, "observed_at")
    return validate_phase_event(event)


def validate_phase_event(event: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate an event received from local or remote transport."""
    if not isinstance(event, Mapping):
        raise PhaseEventError("phase event must be an object")
    normalized = dict(event)
    if normalized.get("schema") != SCHEMA or normalized.get("contract_version") != CONTRACT_VERSION:
        raise PhaseEventError("unsupported phase event contract")
    for field in ("event_id", "run_id", "work_item_id", "actor", "cause", "causation_id", "reason_code", "to_phase"):
        _text(normalized.get(field), field)
    sequence = normalized.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise PhaseEventError("sequence must be a positive integer")
    to_phase = normalized["to_phase"]
    phase_to_board_state(to_phase)
    from_phase = normalized.get("from_phase")
    if from_phase is not None:
        phase_to_board_state(from_phase)
        if to_phase not in ALLOWED_TRANSITIONS.get(from_phase, frozenset()):
            raise PhaseEventError("invalid transition: %s -> %s" % (from_phase, to_phase))
    if normalized.get("board_state") != phase_to_board_state(to_phase):
        raise PhaseEventError("board_state must be derived from to_phase")
    if not isinstance(normalized.get("payload", {}), Mapping):
        raise PhaseEventError("payload must be an object")
    return normalized


def reconcile_events(events: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Validate, deduplicate and order events for offline Runtime replay.

    Duplicate event IDs are accepted only when the complete envelope matches.
    Sequence gaps and conflicting causality are rejected instead of guessed.
    """
    unique: Dict[str, Dict[str, Any]] = {}
    for raw in events:
        event = validate_phase_event(raw)
        event_id = event["event_id"]
        previous = unique.get(event_id)
        if previous is not None and previous != event:
            raise PhaseEventError("conflicting duplicate event_id: %s" % event_id)
        unique[event_id] = event
    ordered = sorted(unique.values(), key=lambda item: (item["sequence"], item["event_id"]))
    expected = 1
    for event in ordered:
        if event["sequence"] != expected:
            raise PhaseEventError("sequence gap at %s (expected %d)" % (event["event_id"], expected))
        expected += 1
    return ordered


__all__ = ["ALLOWED_TRANSITIONS", "BOARD_STATES", "CONTRACT_VERSION", "PHASES", "SCHEMA", "PhaseEventError", "build_phase_event", "phase_to_board_state", "reconcile_events", "validate_phase_event"]
