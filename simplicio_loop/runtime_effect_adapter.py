"""Explicit standalone/runtime-backed effect boundary (#695)."""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .runtime_bridge import RuntimeBridge, RuntimeBridgeError

SCHEMA = "simplicio.runtime-effect-adapter/v1"
PROFILES = frozenset(("standalone", "runtime-backed"))


class RuntimeEffectError(RuntimeError):
    """An effect was malformed, unauthorized or unavailable."""


@dataclass(frozen=True)
class EffectRequest:
    workspace: str
    idempotency_key: str
    write_set: tuple[str, ...]
    lease_id: str
    fencing_token: int
    cwd: str = "."
    timeout_ms: int = 120_000

    def __post_init__(self) -> None:
        if not self.workspace or not self.idempotency_key or not self.write_set or not self.lease_id or self.fencing_token < 1:
            raise RuntimeEffectError("workspace, idempotency, write set and lease/fence are required")
        if Path(self.cwd).is_absolute() or ".." in Path(self.cwd).parts:
            raise RuntimeEffectError("effect cwd must stay workspace-relative")


class RuntimeEffectAdapter:
    """One authoritative effect adapter for a selected Runtime-backed profile."""

    def __init__(self, *, profile: str, bridge: Optional[RuntimeBridge] = None) -> None:
        if profile not in PROFILES:
            raise RuntimeEffectError("unsupported execution profile")
        if profile == "runtime-backed" and bridge is None:
            raise RuntimeEffectError("runtime-backed profile requires RuntimeBridge")
        self.profile = profile
        self.bridge = bridge

    def _receipt(self, request: EffectRequest, *, kind: str, result: Mapping[str, Any], status: str = "MEASURED") -> Dict[str, Any]:
        return {
            "schema": SCHEMA, "profile": self.profile,
            "executor": "simplicio-runtime" if self.profile == "runtime-backed" else "standalone",
            "status": status, "kind": kind, "workspace": request.workspace,
            "idempotency_key": request.idempotency_key, "lease_id": request.lease_id,
            "fencing_token": request.fencing_token, "write_set": list(request.write_set),
            "result": dict(result), "correlation_id": uuid.uuid4().hex,
        }

    def execute(self, request: EffectRequest, argv: list[str], *, env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
        if not argv:
            raise RuntimeEffectError("effect argv is required")
        if self.profile == "standalone":
            return self._receipt(request, kind="execute", result={"status": "UNAVAILABLE", "reason": "standalone_profile"}, status="UNAVAILABLE")
        try:
            result = self.bridge.execute(request.workspace, argv, cwd=request.cwd, env=env, timeout_ms=request.timeout_ms, idempotency_key=request.idempotency_key)  # type: ignore[union-attr]
        except RuntimeBridgeError as exc:
            return self._receipt(request, kind="execute", result={"status": "UNAVAILABLE", "reason": str(exc)}, status="UNAVAILABLE")
        return self._receipt(request, kind="execute", result=result)

    def call(self, request: EffectRequest, tool: str, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        if self.profile == "standalone":
            return self._receipt(request, kind="call", result={"status": "UNAVAILABLE", "reason": "standalone_profile"}, status="UNAVAILABLE")
        try:
            result = self.bridge.runtime_call(request.workspace, tool, arguments, cwd=request.cwd, timeout_ms=request.timeout_ms, idempotency_key=request.idempotency_key)  # type: ignore[union-attr]
        except RuntimeBridgeError as exc:
            return self._receipt(request, kind="call", result={"status": "UNAVAILABLE", "reason": str(exc)}, status="UNAVAILABLE")
        return self._receipt(request, kind="call", result=result)


__all__ = ["EffectRequest", "PROFILES", "RuntimeEffectAdapter", "RuntimeEffectError", "SCHEMA"]
