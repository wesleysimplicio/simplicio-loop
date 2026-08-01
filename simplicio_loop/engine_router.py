"""Side-effect-free optional backend discovery layered over the Python boundary."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping

from .engine_boundary import EngineSelectionReceipt, select_engine

PROBE_SCHEMA = "simplicio.loop-engine-probe/v1"
ENGINE_PROTOCOL = "simplicio.loop-engine/v1"
REQUIRED_OPERATIONS = frozenset({"single", "batch", "prism", "recovery", "delivery"})


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def probe_optional_backend(probe: Callable[[], Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Probe a provider without importing or executing a mutating operation."""
    if probe is None:
        payload = {"name": "rust", "available": False, "compatible": False,
                   "conformance_passed": False, "reason_code": "provider_not_configured"}
    else:
        try:
            raw = probe()
            if not isinstance(raw, Mapping):
                raise TypeError("provider probe must return an object")
            payload = dict(raw)
            payload["abi_valid"] = _valid_abi(payload)
            if payload.get("available") and not payload["abi_valid"]:
                payload["compatible"] = False
                payload["conformance_passed"] = False
                payload["reason_code"] = "provider_abi_invalid"
        except Exception as exc:
            payload = {"name": "rust", "available": False, "compatible": False,
                       "conformance_passed": False,
                       "reason_code": "probe_failed", "error": f"{type(exc).__name__}: {exc}"}
    payload.setdefault("name", "rust")
    payload["schema"] = PROBE_SCHEMA
    payload["probe_hash"] = _hash({key: value for key, value in payload.items() if key != "probe_hash"})
    return payload


def _valid_abi(payload: Mapping[str, Any]) -> bool:
    operations = payload.get("operations")
    return (payload.get("protocol") == ENGINE_PROTOCOL
            and isinstance(operations, (list, tuple, set, frozenset))
            and REQUIRED_OPERATIONS.issubset(set(operations)))


def route_backend(mode: str = "auto", *, probe: Callable[[], Mapping[str, Any]] | None = None,
                  attempt_id: str = "") -> tuple[EngineSelectionReceipt, dict[str, Any]]:
    observation = probe_optional_backend(probe)
    return select_engine(mode, rust_probe=observation, attempt_id=attempt_id), observation


__all__ = ["ENGINE_PROTOCOL", "PROBE_SCHEMA", "REQUIRED_OPERATIONS", "probe_optional_backend", "route_backend"]
