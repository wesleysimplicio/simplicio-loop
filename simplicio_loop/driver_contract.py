"""Host-neutral loop driver contract.

Hook callbacks and scheduler ticks are transports, not separate state machines.  This
module normalizes either transport into one deterministic decision and canonical event
stream.  It is intentionally stdlib-only so runtimes can import it without the CLI.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List, Mapping

SCHEMA = "simplicio.loop-driver/v1"


class DriverContractError(ValueError):
    pass


def _canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def event_id(event: Mapping[str, Any]) -> str:
    """Return a stable id when a transport did not provide one."""
    explicit = str(event.get("event_id") or "").strip()
    if explicit:
        return explicit
    seed = {k: event.get(k) for k in ("run_id", "iteration", "phase", "decision", "reason_code")}
    return "evt-" + hashlib.sha256(_canon(seed).encode("utf-8")).hexdigest()[:20]


def normalize_event(raw: Mapping[str, Any], source: str) -> Dict[str, Any]:
    """Normalize hook/self-paced observations into the same envelope."""
    if not isinstance(raw, Mapping):
        raise DriverContractError("driver event must be an object")
    source = str(source or raw.get("source") or "").strip().lower()
    if source not in {"hook", "self-paced"}:
        raise DriverContractError("source must be hook or self-paced")
    event = {
        "schema": SCHEMA,
        "event_id": event_id(raw),
        "source": source,
        "run_id": str(raw.get("run_id") or "").strip(),
        "iteration": int(raw.get("iteration") or 0),
        "phase": str(raw.get("phase") or "tick").strip(),
        "decision": str(raw.get("decision") or "continue").strip().lower(),
        "reason_code": str(raw.get("reason_code") or "").strip(),
        "gates": dict(raw.get("gates") or {}),
    }
    if event["iteration"] < 0:
        raise DriverContractError("iteration must be non-negative")
    if event["decision"] not in {"continue", "stop", "block"}:
        raise DriverContractError("invalid driver decision")
    return event


def reconcile_events(events: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplicate identical callbacks and reject conflicting duplicate ids."""
    seen: Dict[str, Dict[str, Any]] = {}
    for raw in events:
        source = str(raw.get("source") or "").strip().lower()
        event = normalize_event(raw, source)
        key = event["event_id"]
        prior = seen.get(key)
        if prior is not None:
            # Hook and scheduler may deliver the same logical tick.  Transport
            # identity is deliberately excluded from the conflict comparison.
            left = dict(prior); right = dict(event)
            left.pop("source", None); right.pop("source", None)
            if _canon(left) != _canon(right):
                raise DriverContractError("conflicting duplicate event: %s" % key)
            continue
        seen[key] = event
    return sorted(seen.values(), key=lambda item: (item["iteration"], item["event_id"]))


def evaluate_tick(snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    """Apply shared cap/STOP/evidence gates; absent hook delivery never completes."""
    if not isinstance(snapshot, Mapping):
        raise DriverContractError("snapshot must be an object")
    mode = str(snapshot.get("mode") or "self-paced").strip().lower()
    if mode not in {"hook", "self-paced"}:
        raise DriverContractError("mode must be hook or self-paced")
    iteration = int(snapshot.get("iteration") or 0)
    maximum = int(snapshot.get("max_iterations") or 1)
    if iteration < 0 or maximum < 1:
        raise DriverContractError("invalid iteration cap")
    gates = dict(snapshot.get("gates") or {})
    if snapshot.get("hook_delivered") is False:
        return {"schema": SCHEMA, "decision": "block", "reason_code": "hook_missing", "tag": "UNVERIFIED", "mode": mode}
    if snapshot.get("stop_requested") is True:
        return {"schema": SCHEMA, "decision": "block", "reason_code": "stop_requested", "tag": "UNVERIFIED", "mode": mode}
    if iteration >= maximum:
        return {"schema": SCHEMA, "decision": "block", "reason_code": "iteration_cap", "tag": "UNVERIFIED", "mode": mode}
    if snapshot.get("promise_exact") is True and all(bool(gates.get(name)) for name in ("watcher", "evidence", "oracle")):
        return {"schema": SCHEMA, "decision": "stop", "reason_code": "completion_verified", "tag": "MEASURED", "mode": mode}
    return {"schema": SCHEMA, "decision": "continue", "reason_code": "gates_pending", "tag": "UNVERIFIED", "mode": mode}


__all__ = ["SCHEMA", "DriverContractError", "event_id", "normalize_event", "reconcile_events", "evaluate_tick"]
