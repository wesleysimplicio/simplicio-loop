"""Side-effect-free optional backend discovery layered over the Python boundary."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping

from .engine_boundary import EngineSelectionReceipt, select_engine

PROBE_SCHEMA = "simplicio.loop-engine-probe/v1"


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
        except Exception as exc:
            payload = {"name": "rust", "available": False, "compatible": False,
                       "conformance_passed": False,
                       "reason_code": "probe_failed", "error": f"{type(exc).__name__}: {exc}"}
    payload.setdefault("name", "rust")
    payload["schema"] = PROBE_SCHEMA
    payload["probe_hash"] = _hash({key: value for key, value in payload.items() if key != "probe_hash"})
    return payload


def route_backend(mode: str = "auto", *, probe: Callable[[], Mapping[str, Any]] | None = None,
                  attempt_id: str = "") -> tuple[EngineSelectionReceipt, dict[str, Any]]:
    observation = probe_optional_backend(probe)
    return select_engine(mode, rust_probe=observation, attempt_id=attempt_id), observation


__all__ = ["PROBE_SCHEMA", "probe_optional_backend", "route_backend"]
