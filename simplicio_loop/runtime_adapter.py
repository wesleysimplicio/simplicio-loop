"""Transport-neutral Loop to Runtime compatibility adapter.

The loop remains usable without a runtime, but a runtime bind is explicit and
version-negotiated.  When a bound transport disappears, mutations are written
to a durable outbox and replayed idempotently after reconnect; completion is
never acknowledged while the runtime is unavailable.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Union

from .phase_events import PhaseEventError, validate_phase_event

SCHEMA = "simplicio.loop-runtime-adapter/v1"
CONTRACT_VERSION = "1"
RUNTIME_CONTRACT = "simplicio.runtime/v1"
OUTBOX_SCHEMA = "simplicio.runtime-outbox/v1"
REQUIRED_CAPABILITIES = frozenset(("events", "leases", "evidence", "completion"))


class RuntimeAdapterError(ValueError):
    """Base error for malformed or incompatible adapter operations."""


class RuntimeCompatibilityError(RuntimeAdapterError):
    """The bound runtime does not support this adapter contract."""


class RuntimeUnavailable(RuntimeAdapterError):
    """The runtime transport cannot currently accept an operation."""


class RuntimeTransport(Protocol):
    """Minimal transport expected from CLI, MCP, Desktop, or HTTP bindings."""

    def negotiate(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def apply(self, operation: Mapping[str, Any]) -> Mapping[str, Any]: ...


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeAdapterError(f"{field} must be a non-empty string")
    return value.strip()


def _envelope(kind: str, run_id: str, work_item_id: str, actor: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "operation_id": "op-" + uuid.uuid4().hex,
        "kind": _text(kind, "kind"),
        "run_id": _text(run_id, "run_id"),
        "work_item_id": _text(work_item_id, "work_item_id"),
        "actor": _text(actor, "actor"),
        "created_at": _now(),
        "payload": dict(payload),
    }


class LoopRuntimeAdapter:
    """Bridge loop operations to a negotiated runtime with safe offline mode.

    ``transport=None`` is standalone mode and must be explicit.  In that mode
    operations are persisted as receipts, never presented as runtime delivery.
    A transport can implement either a local runtime, MCP, or an HTTP client.
    """

    def __init__(self, *, run_id: str, work_item_id: str, actor: str,
                 transport: Optional[RuntimeTransport] = None,
                 outbox_path: Optional[Union[str, Path]] = None,
                 standalone: bool = False) -> None:
        self.run_id = _text(run_id, "run_id")
        self.work_item_id = _text(work_item_id, "work_item_id")
        self.actor = _text(actor, "actor")
        if standalone and transport is not None:
            raise RuntimeAdapterError("standalone mode cannot also bind a runtime")
        if transport is None and not standalone:
            raise RuntimeAdapterError("runtime bind is explicit; pass transport or standalone=True")
        self.transport = transport
        self.standalone = standalone
        self.outbox_path = Path(outbox_path) if outbox_path else None
        self.degraded = False
        self.negotiated: Dict[str, Any] = {}
        self._negotiated_ok = standalone
        self._ensure_outbox()

    @property
    def mode(self) -> str:
        return "standalone" if self.standalone else ("degraded" if self.degraded else "runtime")

    def negotiate(self) -> Dict[str, Any]:
        if self.standalone:
            self.negotiated = {"mode": "standalone", "contract": RUNTIME_CONTRACT,
                               "contract_version": CONTRACT_VERSION, "capabilities": []}
            self._negotiated_ok = True
            return dict(self.negotiated)
        request = {"schema": SCHEMA, "contract": RUNTIME_CONTRACT,
                   "contract_version": CONTRACT_VERSION, "run_id": self.run_id}
        try:
            response = dict(self.transport.negotiate(request))  # type: ignore[union-attr]
        except Exception as exc:
            self.degraded = True
            raise RuntimeUnavailable("runtime negotiation failed") from exc
        if response.get("contract") != RUNTIME_CONTRACT or str(response.get("contract_version")) != CONTRACT_VERSION:
            raise RuntimeCompatibilityError(
                "incompatible runtime contract; expected %s/%s, got %s/%s"
                % (RUNTIME_CONTRACT, CONTRACT_VERSION, response.get("contract"), response.get("contract_version"))
            )
        capabilities = response.get("capabilities")
        if not isinstance(capabilities, (list, tuple, set)) or not REQUIRED_CAPABILITIES.issubset(set(capabilities)):
            missing = sorted(REQUIRED_CAPABILITIES.difference(set(capabilities or [])))
            raise RuntimeCompatibilityError("runtime is missing required capabilities: " + ", ".join(missing))
        self.negotiated = response
        self._negotiated_ok = True
        self.degraded = False
        return dict(response)

    def register_run(self, manifest: Mapping[str, Any]) -> Dict[str, Any]:
        return self._submit(_envelope("register_run", self.run_id, self.work_item_id, self.actor, manifest))

    def emit_event(self, event: Mapping[str, Any]) -> Dict[str, Any]:
        try:
            normalized = validate_phase_event(event)
        except PhaseEventError as exc:
            raise RuntimeAdapterError(str(exc)) from exc
        if normalized["run_id"] != self.run_id or normalized["work_item_id"] != self.work_item_id:
            raise RuntimeAdapterError("event identity does not match adapter run/work item")
        return self._submit(_envelope("event", self.run_id, self.work_item_id, self.actor, normalized))

    def lease(self, action: str, lease: Mapping[str, Any]) -> Dict[str, Any]:
        return self._submit(_envelope("lease_" + _text(action, "action"), self.run_id, self.work_item_id, self.actor, lease))

    def record_evidence(self, receipt: Mapping[str, Any]) -> Dict[str, Any]:
        if not receipt.get("schema") or not receipt.get("status"):
            raise RuntimeAdapterError("evidence receipt requires schema and status")
        return self._submit(_envelope("evidence", self.run_id, self.work_item_id, self.actor, receipt))

    def complete(self, receipt: Mapping[str, Any]) -> Dict[str, Any]:
        if receipt.get("ready") is not True or receipt.get("verdict") != "COMPLETE":
            raise RuntimeAdapterError("completion requires a COMPLETE receipt; outage cannot create Done")
        return self._submit(_envelope("complete", self.run_id, self.work_item_id, self.actor, receipt))

    def reconcile(self) -> Dict[str, Any]:
        pending = self._read_outbox()
        if not pending or self.standalone:
            return {"mode": self.mode, "replayed": 0, "pending": len(pending), "status": "DEGRADED" if pending and not self.standalone else "MEASURED"}
        if not self._negotiated_ok:
            raise RuntimeCompatibilityError("negotiate before reconciling runtime operations")
        replayed: List[Dict[str, Any]] = []
        remaining: List[Dict[str, Any]] = []
        for index, operation in enumerate(pending):
            try:
                response = dict(self.transport.apply(operation))  # type: ignore[union-attr]
                replayed.append({"operation_id": operation["operation_id"], "response": response})
            except Exception:
                remaining.append(operation)
                remaining.extend(pending[index + 1:])
                break
        self._write_outbox(remaining)
        self.degraded = bool(remaining)
        return {"mode": self.mode, "replayed": len(replayed), "pending": len(remaining), "status": "DEGRADED" if remaining else "MEASURED", "operations": replayed}

    def _submit(self, operation: Dict[str, Any]) -> Dict[str, Any]:
        if self.standalone:
            self._append_outbox(operation)
            return {"status": "STANDALONE", "operation_id": operation["operation_id"], "run_id": self.run_id, "work_item_id": self.work_item_id}
        if not self._negotiated_ok:
            raise RuntimeCompatibilityError("negotiate before runtime mutation")
        try:
            response = dict(self.transport.apply(operation))  # type: ignore[union-attr]
            self.degraded = False
            return {"status": "DELIVERED", "operation_id": operation["operation_id"], "run_id": self.run_id, "work_item_id": self.work_item_id, "response": response}
        except Exception:
            self.degraded = True
            self._append_outbox(operation)
            return {"status": "BUFFERED", "operation_id": operation["operation_id"], "run_id": self.run_id, "work_item_id": self.work_item_id, "degraded": True}

    def _ensure_outbox(self) -> None:
        if self.outbox_path and not self.outbox_path.exists():
            self.outbox_path.parent.mkdir(parents=True, exist_ok=True)
            self.outbox_path.write_text("", encoding="utf-8")

    def _read_outbox(self) -> List[Dict[str, Any]]:
        if not self.outbox_path or not self.outbox_path.exists():
            return []
        values: List[Dict[str, Any]] = []
        for line in self.outbox_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                values.append(json.loads(line))
        return values

    def _write_outbox(self, operations: Iterable[Mapping[str, Any]]) -> None:
        if not self.outbox_path:
            return
        self.outbox_path.parent.mkdir(parents=True, exist_ok=True)
        self.outbox_path.write_text("".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in operations), encoding="utf-8")

    def _append_outbox(self, operation: Mapping[str, Any]) -> None:
        if not self.outbox_path:
            return
        self.outbox_path.parent.mkdir(parents=True, exist_ok=True)
        with self.outbox_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(operation, ensure_ascii=False, sort_keys=True) + "\n")


__all__ = ["CONTRACT_VERSION", "LoopRuntimeAdapter", "OUTBOX_SCHEMA", "REQUIRED_CAPABILITIES", "RUNTIME_CONTRACT", "RuntimeAdapterError", "RuntimeCompatibilityError", "RuntimeTransport", "RuntimeUnavailable", "SCHEMA"]
