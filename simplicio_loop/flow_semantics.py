"""Model-free converge and drain scheduling semantics.

The runtime may provide persistence and leases, but the Loop owns this small,
deterministic decision surface.  Inputs are immutable attempt/poll snapshots;
outputs are JSON-safe receipts suitable for a board projection or a retry.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Sequence

SCHEMA = "simplicio.flow-semantics/v1"


def _result(mode: str, status: str, reason_code: str, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "schema": SCHEMA,
        "mode": mode,
        "status": status,
        "verdict": status,
        "reason_code": reason_code,
        "tag": "MEASURED",
    }
    out.update(extra)
    return out


def evaluate_converge(
    attempts: Sequence[Mapping[str, Any]], *, max_attempts: int = 3, stall_threshold: int = 3
) -> Dict[str, Any]:
    """Evaluate one converge work item without treating a no-op as completion.

    An attempt is complete only when its ``verified`` gate is true.  Consecutive
    identical failure fingerprints require a strategy change; repeating the same
    strategy through the threshold escalates instead of oscillating forever.
    """
    if not isinstance(attempts, Sequence) or isinstance(attempts, (str, bytes)):
        return _result("converge", "BLOCKED", "attempts_invalid")
    if max_attempts < 1 or stall_threshold < 1:
        return _result("converge", "BLOCKED", "limits_invalid")
    rows = [dict(row) for row in attempts if isinstance(row, Mapping)]
    if len(rows) != len(attempts):
        return _result("converge", "BLOCKED", "attempt_invalid")
    if not rows:
        return _result("converge", "CONTINUE", "no_attempts", attempt_count=0)

    latest = rows[-1]
    if bool(latest.get("verified")):
        return _result("converge", "COMPLETE", "verified", attempt_count=len(rows), strategy_changed=False)
    if len(rows) >= max_attempts:
        return _result("converge", "ESCALATE", "attempt_cap", attempt_count=len(rows))

    fingerprint = str(latest.get("failure_fingerprint") or latest.get("fingerprint") or "").strip()
    repeat = 0
    if fingerprint:
        for row in reversed(rows):
            value = str(row.get("failure_fingerprint") or row.get("fingerprint") or "").strip()
            if value != fingerprint:
                break
            repeat += 1
    previous = rows[-2] if len(rows) > 1 else None
    strategy_changed = bool(latest.get("strategy_changed"))
    if previous is not None:
        old_strategy = previous.get("strategy_id", previous.get("strategy"))
        new_strategy = latest.get("strategy_id", latest.get("strategy"))
        strategy_changed = strategy_changed or (old_strategy is not None and new_strategy is not None and old_strategy != new_strategy)
    if repeat >= stall_threshold:
        if strategy_changed:
            return _result("converge", "RETRY", "strategy_changed", attempt_count=len(rows), failure_fingerprint=fingerprint, repeat_count=repeat, strategy_changed=True)
        return _result("converge", "ESCALATE", "stall_escalation", attempt_count=len(rows), failure_fingerprint=fingerprint, repeat_count=repeat, strategy_changed=False)
    return _result("converge", "RETRY", "no_progress" if not bool(latest.get("changed")) else "attempt_failed", attempt_count=len(rows), failure_fingerprint=fingerprint, repeat_count=repeat, strategy_changed=strategy_changed)


def _items(value: Any) -> List[str]:
    if isinstance(value, Mapping):
        value = value.keys()
    if isinstance(value, (str, bytes)) or value is None:
        return [str(value)] if value else []
    try:
        return sorted({str(item) for item in value if str(item)})
    except TypeError:
        return []


def evaluate_drain(rounds: Sequence[Mapping[str, Any]], *, k: int = 2) -> Dict[str, Any]:
    """Evaluate dependency-aware drain polls, quarantining blocked items.

    A drain needs ``k`` identical empty *and idle* source polls.  A ready item
    appearing after an empty poll is reported as ``late_arrival`` and keeps the
    queue active.  Blocked items are quarantined with their dead ends, so they
    do not hide otherwise drainable work.
    """
    if not isinstance(rounds, Sequence) or isinstance(rounds, (str, bytes)):
        return _result("drain", "BLOCKED", "rounds_invalid")
    if k < 1:
        return _result("drain", "BLOCKED", "limit_invalid")
    polls = [dict(row) for row in rounds if isinstance(row, Mapping)]
    if len(polls) != len(rounds):
        return _result("drain", "BLOCKED", "round_invalid")
    quarantined: List[Dict[str, Any]] = []
    late: List[str] = []
    previous_empty = False
    empty_tail = 0
    for poll in polls:
        ready = _items(poll.get("ready"))
        active = _items(poll.get("active"))
        blocked = poll.get("blocked") or []
        if isinstance(blocked, Mapping):
            blocked = [blocked]
        for item in blocked if isinstance(blocked, Iterable) and not isinstance(blocked, (str, bytes)) else []:
            if isinstance(item, Mapping):
                quarantined.append({"id": str(item.get("id") or item.get("item_id") or ""), "reason": str(item.get("reason") or "blocked"), "dead_ends": _items(item.get("dead_ends"))})
            else:
                quarantined.append({"id": str(item), "reason": "blocked", "dead_ends": []})
        is_empty = not ready and not active
        if previous_empty and ready:
            late.extend(ready)
        if is_empty:
            empty_tail += 1
        else:
            empty_tail = 0
        previous_empty = is_empty
    quarantined = sorted(quarantined, key=lambda item: (item["id"], item["reason"], item["dead_ends"]))
    late = sorted(set(late))
    if late:
        return _result("drain", "CONTINUE", "late_arrival", late_arrivals=late, quarantined=quarantined, empty_rounds=empty_tail)
    if empty_tail >= k:
        return _result("drain", "DRAINED", "drain_verified", empty_rounds=empty_tail, quarantined=quarantined, late_arrivals=[])
    return _result("drain", "CONTINUE", "source_not_quiet", empty_rounds=empty_tail, required_empty_rounds=k, quarantined=quarantined, late_arrivals=[])


__all__ = ["SCHEMA", "evaluate_converge", "evaluate_drain"]
