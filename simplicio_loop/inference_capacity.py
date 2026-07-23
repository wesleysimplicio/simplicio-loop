"""Optional Runtime inference-capacity snapshots and locality decisions.

This module is deliberately pure and Runtime-optional.  Loop may consume a
sanitized snapshot when Runtime provides one, but an absent or unsupported
snapshot is represented by ``None`` and never changes the legacy path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence


SCHEMA = "simplicio.inference-capabilities/v1"


class CapacitySnapshotError(ValueError):
    """Raised when an optional capacity snapshot is malformed."""


@dataclass(frozen=True)
class CapacitySnapshot:
    """Sanitized capacity facts; raw prompts and slot identifiers are excluded."""

    backend: str
    model: str
    generation: str
    available_slots: int
    max_slots: int
    queue_depth: int = 0
    healthy: bool = True
    affinity_hint: str = ""

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> Optional["CapacitySnapshot"]:
        """Parse a Runtime payload, returning ``None`` for absent/unsupported data."""
        if payload is None:
            return None
        if not isinstance(payload, Mapping) or payload.get("schema") != SCHEMA:
            return None
        try:
            backend = _text(payload, "backend")
            model = _text(payload, "model")
            generation = _text(payload, "generation")
            available = _count(payload, "available_slots")
            maximum = _count(payload, "max_slots")
            queue_depth = _count(payload, "queue_depth", default=0)
            healthy = payload.get("healthy", True)
            if not isinstance(healthy, bool):
                raise CapacitySnapshotError("healthy must be boolean")
            if maximum == 0 or available > maximum:
                raise CapacitySnapshotError("available_slots must not exceed max_slots")
            # The hint is an opaque, already-redacted affinity token.  Refuse
            # obvious raw slot identifiers rather than persisting them in Loop.
            hint = payload.get("affinity_hint", "")
            if not isinstance(hint, str) or len(hint) > 256:
                raise CapacitySnapshotError("affinity_hint must be a short string")
            if hint.lower().startswith(("slot:", "slot_id:", "prompt:")):
                raise CapacitySnapshotError("raw slot or prompt data is not allowed")
        except KeyError as exc:
            raise CapacitySnapshotError(f"missing capacity field: {exc.args[0]}") from exc
        return cls(backend, model, generation, available, maximum, queue_depth, healthy, hint)


def _text(payload: Mapping[str, Any], name: str) -> str:
    value = payload[name]
    if not isinstance(value, str) or not value.strip():
        raise CapacitySnapshotError(f"{name} must be a non-empty string")
    return value.strip()


def _count(payload: Mapping[str, Any], name: str, *, default: int | None = None) -> int:
    value = payload.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CapacitySnapshotError(f"{name} must be a non-negative integer")
    return value


def choose_affinity(
    task: Mapping[str, Any],
    candidates: Sequence[CapacitySnapshot],
) -> Optional[CapacitySnapshot]:
    """Choose a healthy locality candidate without starving older work.

    Locality is only a hint: an explicit deadline, queue-age threshold, or
    priority override wins.  Sorting is stable and contains no machine-local
    identifiers, making the result deterministic and safe to receipt.
    """
    if task.get("deadline_override") or task.get("locality_disabled"):
        return None
    wanted_backend = task.get("backend")
    wanted_model = task.get("model")
    wanted_hint = task.get("affinity_hint")
    queue_age = int(task.get("queue_age", 0) or 0)
    priority = int(task.get("priority", 0) or 0)
    if queue_age >= int(task.get("max_locality_age", 300) or 300):
        return None
    ranked = []
    for candidate in candidates:
        if not candidate.healthy or candidate.available_slots <= 0:
            continue
        if wanted_backend and candidate.backend != wanted_backend:
            continue
        if wanted_model and candidate.model != wanted_model:
            continue
        affinity = bool(wanted_hint and candidate.affinity_hint == wanted_hint)
        # Priority and age dominate locality; locality only breaks ties.
        score = (priority, -queue_age, int(affinity), -candidate.queue_depth)
        ranked.append((score, candidate))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1]


__all__ = ["SCHEMA", "CapacitySnapshot", "CapacitySnapshotError", "choose_affinity"]
