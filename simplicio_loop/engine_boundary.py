"""Canonical Python engine boundary and optional backend selection receipt."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

SCHEMA = "simplicio.loop-engine-capabilities/v1"
SELECTION_SCHEMA = "simplicio.loop-engine-selection/v1"
MODES = ("auto", "python", "rust", "shadow", "off")


class EngineSelectionError(RuntimeError):
    """The requested engine mode cannot be honored safely."""


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EngineCapabilities:
    name: str = "python-loop"
    protocol: str = "simplicio.loop-engine/v1"
    build: str = "source"
    operations: tuple[str, ...] = ("single", "batch", "prism", "recovery", "delivery")
    effect: bool = True
    recovery: bool = True
    cancellation: bool = True
    evidence: bool = True
    conformance_hash: str | None = None
    optional: bool = False
    limits: Mapping[str, int] = field(default_factory=lambda: {"max_tasks": 50, "max_depth": 4})

    def to_dict(self) -> dict[str, Any]:
        payload = {"schema": SCHEMA, "name": self.name, "protocol": self.protocol,
                   "build": self.build, "operations": list(self.operations),
                   "effect": self.effect, "recovery": self.recovery,
                   "cancellation": self.cancellation, "evidence": self.evidence,
                   "conformance_hash": self.conformance_hash, "optional": self.optional,
                   "limits": dict(self.limits)}
        payload["capabilities_hash"] = _hash(payload)
        return payload


@dataclass(frozen=True)
class EngineSelectionReceipt:
    requested_mode: str
    selected_engine: str
    reason_code: str
    candidates: tuple[Mapping[str, Any], ...]
    authority_boundary: str = "simplicio_loop"
    schema: str = SELECTION_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        payload = {"schema": self.schema, "requested_mode": self.requested_mode,
                   "selected_engine": self.selected_engine, "reason_code": self.reason_code,
                   "candidates": [dict(item) for item in self.candidates],
                   "authority_boundary": self.authority_boundary}
        payload["receipt_hash"] = _hash(payload)
        return payload


class PythonLoopEngine:
    """Reference engine: no Runtime/Rust import or executable is required."""

    capabilities = EngineCapabilities()

    def execute(self, operation: Callable[..., Mapping[str, Any]], *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = operation(*args, **kwargs)
        if not isinstance(result, Mapping):
            raise TypeError("Loop engine operation must return an object")
        return dict(result)


def select_engine(mode: str = "auto", *, rust_probe: Mapping[str, Any] | None = None,
                  attempt_id: str = "") -> EngineSelectionReceipt:
    requested = str(mode or "auto").strip().lower()
    if requested not in MODES:
        raise ValueError(f"engine mode must be one of {MODES}")
    probe = dict(rust_probe or {})
    rust_ready = bool(probe.get("compatible") and probe.get("conformance_passed"))
    rust_candidate = {"name": str(probe.get("name") or "rust"),
                      "available": bool(probe.get("available")),
                      "compatible": bool(probe.get("compatible")),
                      "conformance_passed": bool(probe.get("conformance_passed")),
                      "reason_code": str(probe.get("reason_code") or "not_probed")}
    if requested == "rust" and not rust_ready:
        raise EngineSelectionError("rust_requested_but_conformance_unavailable")
    if requested in {"python", "off"} or (requested == "auto" and not rust_ready):
        reason = "python_forced" if requested in {"python", "off"} else "rust_unavailable_python_canonical"
        selected = "python-loop"
    elif requested == "shadow":
        selected, reason = "python-loop", "shadow_read_only_python_authority"
    else:
        selected, reason = "rust", "rust_conformance_passed"
    candidates = ({"name": "python-loop", "selected": selected == "python-loop",
                   "reason_code": "canonical_reference", "attempt_id": attempt_id}, rust_candidate)
    return EngineSelectionReceipt(requested, selected, reason, candidates)


__all__ = ["MODES", "SCHEMA", "SELECTION_SCHEMA", "EngineCapabilities", "EngineSelectionError",
           "EngineSelectionReceipt", "PythonLoopEngine", "select_engine"]
