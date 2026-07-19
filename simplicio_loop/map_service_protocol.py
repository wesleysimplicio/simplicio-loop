"""Versioned request/response contract for the Map Service.

The registry implementation is intentionally transport-agnostic.  This module is the
small, fail-closed boundary used by IPC/SDK adapters so every operation has the same
version negotiation, required fields, and typed error shape.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional

PROTOCOL_SCHEMA = "simplicio.map-service/v1"
PROTOCOL_VERSION = 1

OPERATIONS = frozenset(
    (
        "resolve_repo", "get_view", "build_canonical", "build_overlay",
        "subscribe", "invalidate", "release", "gc",
    )
)

_REQUIRED = {
    "resolve_repo": ("path",),
    "get_view": ("cache_key",),
    "build_canonical": ("identity_key", "tree_hash"),
    "build_overlay": ("identity_key", "tree_hash"),
    "subscribe": ("identity_key",),
    "invalidate": ("identity_key",),
    "release": ("cache_key",),
    "gc": (),
}


class MapProtocolError(ValueError):
    """A stable, machine-readable protocol validation failure."""

    def __init__(self, code: str, message: str, *, details: Optional[Mapping[str, Any]] = None):
        self.code = str(code)
        self.details = dict(details or {})
        super().__init__(str(message))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": PROTOCOL_SCHEMA,
            "version": PROTOCOL_VERSION,
            "code": self.code,
            "message": str(self),
            "details": dict(self.details),
        }


def negotiate(client_version: int, *, supported: Iterable[int] = (PROTOCOL_VERSION,)) -> Dict[str, Any]:
    """Negotiate one exact protocol version without silently downgrading."""
    versions = sorted({int(version) for version in supported})
    if int(client_version) not in versions:
        raise MapProtocolError(
            "unsupported_version",
            "client and server have no compatible Map Service version",
            details={"client_version": int(client_version), "supported": versions},
        )
    return {"schema": PROTOCOL_SCHEMA, "version": int(client_version), "operations": sorted(OPERATIONS)}


def validate_request(operation: str, payload: Mapping[str, Any], *, version: int = PROTOCOL_VERSION) -> Dict[str, Any]:
    """Validate and normalize an operation payload, failing closed on malformed input."""
    if int(version) != PROTOCOL_VERSION:
        raise MapProtocolError("unsupported_version", "unsupported Map Service protocol version", details={"version": version})
    operation = str(operation)
    if operation not in OPERATIONS:
        raise MapProtocolError("unknown_operation", "unknown Map Service operation", details={"operation": operation})
    if not isinstance(payload, Mapping):
        raise MapProtocolError("invalid_payload", "payload must be an object")
    normalized = dict(payload)
    missing = [field for field in _REQUIRED[operation] if not str(normalized.get(field, "")).strip()]
    if missing:
        raise MapProtocolError("missing_field", "required Map Service field is missing", details={"fields": missing})
    if operation in {"build_canonical", "build_overlay"} and not isinstance(normalized.get("tree_hash"), str):
        raise MapProtocolError("invalid_field", "tree_hash must be a string")
    if operation == "build_overlay" and not isinstance(normalized.get("dirty_files", []), (list, tuple)):
        raise MapProtocolError("invalid_field", "dirty_files must be an array")
    return normalized


def success(operation: str, result: Mapping[str, Any], *, version: int = PROTOCOL_VERSION) -> Dict[str, Any]:
    validate_request(operation, {}, version=version) if operation == "gc" else None
    if operation not in OPERATIONS:
        raise MapProtocolError("unknown_operation", "unknown Map Service operation")
    return {"schema": PROTOCOL_SCHEMA, "version": int(version), "ok": True, "operation": operation, "result": dict(result)}


def failure(error: MapProtocolError) -> Dict[str, Any]:
    return {"schema": PROTOCOL_SCHEMA, "version": PROTOCOL_VERSION, "ok": False, "error": error.to_dict()}


__all__ = [
    "OPERATIONS", "PROTOCOL_SCHEMA", "PROTOCOL_VERSION", "MapProtocolError",
    "failure", "negotiate", "success", "validate_request",
]
