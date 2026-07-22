"""Strict, transport-injected Hub client for stage-agent execution.

The client owns only the coordinator-side IPC contract.  Execution, leases and
resource admission remain Hub responsibilities.  In particular, this module
has no process or scheduler implementation and never creates a local worker.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import time
from pathlib import Path
from typing import Any, Mapping, Optional


IPC_SCHEMA = "simplicio.hub-ipc/v1"
AGENT_SCHEMA = "simplicio.hub-agent/v1"
CAPABILITY = "hub-agent-process/v1"
JOURNAL_SCHEMA = "simplicio.hub-agent-journal/v1"


class HubQueueAgentError(RuntimeError):
    """A fail-closed Hub client error with a stable reason code."""

    def __init__(self, message: str, *, reason_code: str = "hub_agent_error") -> None:
        super().__init__(message)
        self.reason_code = reason_code


class HubQueueAgentUnavailable(HubQueueAgentError):
    def __init__(self, message: str = "Hub agent capability is unavailable") -> None:
        super().__init__(message, reason_code="hub_unavailable")


class HubQueueAgentJournal:
    """Small append-only, fsynced hash-chain journal owned by the client."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._events = self._read()

    @staticmethod
    def _digest(record: Mapping[str, Any]) -> str:
        raw = json.dumps(dict(record), sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        previous = "0" * 64
        for expected_seq, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise HubQueueAgentError("Hub agent journal contains invalid JSON", reason_code="journal_corrupt") from exc
            if not isinstance(record, dict) or record.get("schema") != JOURNAL_SCHEMA:
                raise HubQueueAgentError("Hub agent journal schema is invalid", reason_code="journal_corrupt")
            unsigned = dict(record)
            actual_hash = unsigned.pop("record_hash", None)
            if record.get("seq") != expected_seq or record.get("prev_hash") != previous:
                raise HubQueueAgentError("Hub agent journal sequence/hash chain is broken", reason_code="journal_corrupt")
            if actual_hash != self._digest(unsigned):
                raise HubQueueAgentError("Hub agent journal record hash is invalid", reason_code="journal_corrupt")
            previous = str(actual_hash)
            events.append(record)
        return events

    def append(self, event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        previous = str(self._events[-1]["record_hash"]) if self._events else "0" * 64
        record: dict[str, Any] = {
            "schema": JOURNAL_SCHEMA,
            "seq": len(self._events) + 1,
            "ts": time.time(),
            "event_type": str(event_type),
            "payload": dict(payload),
            "prev_hash": previous,
        }
        record["record_hash"] = self._digest(record)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":"), default=str) + "\n")
            handle.flush()
            import os
            os.fsync(handle.fileno())
        self._events.append(record)
        return dict(record)

    def replay(self) -> list[dict[str, Any]]:
        return [dict(event) for event in self._events]

    def pending(self) -> list[dict[str, Any]]:
        """Return intents without a matching effect, without replaying them."""
        intents = {
            str(event["payload"].get("operation_id")): event
            for event in self._events
            if event.get("event_type") == "intent"
        }
        for event in self._events:
            if event.get("event_type") == "effect":
                intents.pop(str(event["payload"].get("operation_id")), None)
        return list(intents.values())


class HubQueueAgentClient:
    """Typed Hub IPC client used by :class:`QueueAgentAdapter`.

    ``hub`` may be a ``HubSocketClient``, a ``HubClient`` or a small fake with
    the same ``request(request_id, method, **payload)`` surface.  Direct
    ``hub_agent_*`` methods are also accepted for extension conformance tests.
    """

    accepts_handle = True
    kind = "hub"

    def __init__(
        self,
        hub: Any,
        *,
        client_id: str = "stage-coordinator",
        worker_id: Optional[str] = None,
        journal_path: str | Path | None = None,
        journal: HubQueueAgentJournal | None = None,
        strict: bool = True,
    ) -> None:
        self._hub = hub
        self.client_id = str(client_id)
        self.worker_id = str(worker_id or client_id)
        self.strict = bool(strict)
        self.journal = journal or (HubQueueAgentJournal(journal_path) if journal_path else None)
        self._capability_checked = False
        self._capabilities: set[str] = set()

    def _request_id(self, operation: str, identity: str) -> str:
        digest = hashlib.sha256(f"{self.client_id}:{operation}:{identity}".encode("utf-8")).hexdigest()[:20]
        return f"hub-agent-{operation}-{digest}"

    @staticmethod
    def _unwrap(response: Any) -> dict[str, Any]:
        if not isinstance(response, Mapping):
            raise HubQueueAgentError("Hub returned a non-object response", reason_code="invalid_hub_response")
        if response.get("ok") is False:
            reason = str(response.get("reason_code") or response.get("error_code") or "hub_rejected")
            message = str(response.get("error") or response.get("message") or reason)
            if reason in {"stale_fence", "lease_lost", "fence_mismatch"}:
                raise HubQueueAgentError(message, reason_code="stale_fence")
            raise HubQueueAgentError(message, reason_code=reason)
        return dict(response)

    def _invoke(self, method: str, payload: Mapping[str, Any], *, identity: str = "") -> dict[str, Any]:
        if self._hub is None:
            raise HubQueueAgentUnavailable()
        direct = getattr(self._hub, method, None)
        if direct is None and method.startswith("hub_agent_"):
            direct = getattr(self._hub, method[len("hub_agent_"):], None)
        if direct is None and method == "hub_agent_capabilities":
            direct = getattr(self._hub, "capabilities", None)
            if isinstance(direct, Mapping):
                return {"ok": True, "capabilities": list(direct.get("capabilities", direct.get("supported_capabilities", [])))}
        if callable(direct):
            try:
                return self._unwrap(direct(**dict(payload)))
            except TypeError:
                signature = inspect.signature(direct)
                if len(signature.parameters) == 1:
                    return self._unwrap(direct(dict(payload)))
                raise
        request = getattr(self._hub, "request", None)
        if not callable(request):
            raise HubQueueAgentUnavailable("Hub binding has no request surface")
        request_id = self._request_id(method, identity)
        try:
            response = request(request_id, method, **dict(payload))
        except TypeError:
            # Extension fakes commonly expose request(method, payload).  This
            # branch is shape adaptation only; it never retries an IPC effect.
            response = request(method, dict(payload))
        return self._unwrap(response)

    def _ensure_capability(self) -> None:
        if self._capability_checked:
            return
        response = self._invoke("hub_agent_capabilities", {"client_id": self.client_id}, identity="capabilities")
        advertised = response.get("capabilities") or response.get("supported_capabilities") or []
        self._capabilities = {str(value) for value in advertised}
        if self.strict and CAPABILITY not in self._capabilities:
            raise HubQueueAgentUnavailable("Hub did not advertise hub-agent-process/v1")
        self._capability_checked = True

    def probe(self) -> bool:
        try:
            self._ensure_capability()
            return True
        except HubQueueAgentError:
            return False

    @staticmethod
    def _identity(context: Mapping[str, Any], role: str, stage: str) -> str:
        values = [context.get(name) for name in ("run_id", "task_id", "attempt_id")]
        if any(value in (None, "") for value in values):
            raise HubQueueAgentError("claim requires run/task/attempt identity", reason_code="invalid_identity")
        return ":".join(str(value) for value in (*values, stage, role))

    def _journal_intent(self, operation: str, operation_id: str, payload: Mapping[str, Any]) -> None:
        if self.journal:
            self.journal.append("intent", {"operation": operation, "operation_id": operation_id, **dict(payload)})

    def _journal_effect(self, operation: str, operation_id: str, response: Mapping[str, Any]) -> None:
        if self.journal:
            self.journal.append("effect", {"operation": operation, "operation_id": operation_id, "response": dict(response)})

    def claim(self, *, role: str, stage: str, context: Mapping[str, Any]) -> dict[str, Any]:
        self._ensure_capability()
        context = dict(context)
        identity = self._identity(context, str(role), str(stage))
        idempotency_key = str(context.get("idempotency_key") or identity)
        payload: dict[str, Any] = {
            "schema": AGENT_SCHEMA,
            "client_id": self.client_id,
            "worker_id": self.worker_id,
            "role_id": str(role),
            "stage_id": str(stage),
            "context": context,
            "idempotency_key": idempotency_key,
            "priority": context.get("priority", "test" if str(stage) in {"validating", "testing"} else "build"),
            "resources": dict(context.get("resources") or {}),
            "deadline_at": context.get("deadline_at"),
            "timeout_seconds": context.get("timeout_seconds"),
        }
        if context.get("process_spec") is not None:
            payload["process_spec"] = dict(context["process_spec"])
        self._journal_intent("claim", idempotency_key, {"payload": payload})
        response = self._invoke("hub_agent_claim", payload, identity=idempotency_key)
        handle = response.get("handle") or response.get("lease") or response.get("claimed") or response
        if not isinstance(handle, Mapping):
            raise HubQueueAgentError("Hub claim returned no handle", reason_code="invalid_hub_response")
        handle = dict(handle)
        handle.setdefault("idempotency_key", idempotency_key)
        handle.setdefault("schema", AGENT_SCHEMA)
        handle.setdefault("client_id", self.client_id)
        if not any(handle.get(name) for name in ("lease_id", "handle_id", "job_id")):
            raise HubQueueAgentError("Hub handle has no stable id", reason_code="invalid_hub_response")
        self._journal_effect("claim", idempotency_key, {"handle": handle})
        return handle

    @staticmethod
    def _handle_id(handle: str | Mapping[str, Any]) -> str:
        if isinstance(handle, Mapping):
            value = handle.get("handle_id") or handle.get("lease_id") or handle.get("job_id")
        else:
            value = handle
        if not value:
            raise HubQueueAgentError("Hub handle id is required", reason_code="invalid_handle")
        return str(value)

    def _handle_payload(self, handle: str | Mapping[str, Any]) -> dict[str, Any]:
        return dict(handle) if isinstance(handle, Mapping) else {"lease_id": self._handle_id(handle)}

    def status(self, handle: str | Mapping[str, Any]) -> dict[str, Any]:
        self._ensure_capability()
        value = self._handle_payload(handle)
        response = self._invoke("hub_agent_status", {"client_id": self.client_id, "handle": value}, identity=self._handle_id(handle))
        return dict(response.get("status") or response.get("job") or response)

    def heartbeat(self, handle: str | Mapping[str, Any]) -> dict[str, Any]:
        value = self._handle_payload(handle)
        return self._invoke("hub_agent_heartbeat", {"client_id": self.client_id, "handle": value}, identity=self._handle_id(handle))

    def progress(self, handle: str | Mapping[str, Any], progress: float) -> dict[str, Any]:
        value = self._handle_payload(handle)
        return self._invoke("hub_agent_progress", {"client_id": self.client_id, "handle": value, "progress": progress}, identity=self._handle_id(handle))

    def send(self, handle: str | Mapping[str, Any], stage_input: Mapping[str, Any]) -> dict[str, Any]:
        self._ensure_capability()
        value = self._handle_payload(handle)
        operation_id = str(value.get("idempotency_key") or self._handle_id(handle)) + ":send"
        payload = {"client_id": self.client_id, "handle": value, "stage_input": dict(stage_input), "operation_id": operation_id}
        self._journal_intent("send", operation_id, {"handle": value})
        response = self._invoke("hub_agent_send", payload, identity=operation_id)
        self._journal_effect("send", operation_id, response)
        return response

    def collect(self, handle: str | Mapping[str, Any]) -> dict[str, Any]:
        self._ensure_capability()
        value = self._handle_payload(handle)
        response = self._invoke("hub_agent_collect", {"client_id": self.client_id, "handle": value}, identity=self._handle_id(handle))
        result = dict(response.get("result") or response)
        if "output" not in result and "stage_output" in result:
            result["output"] = result["stage_output"]
        if "receipt" not in result and "stage_receipt" in result:
            result["receipt"] = result["stage_receipt"]
        return result

    def cancel(self, handle: str | Mapping[str, Any], *, reason: str) -> dict[str, Any]:
        self._ensure_capability()
        value = self._handle_payload(handle)
        operation_id = str(value.get("idempotency_key") or self._handle_id(handle)) + ":cancel:" + str(reason)
        payload = {"client_id": self.client_id, "handle": value, "reason": str(reason), "operation_id": operation_id}
        self._journal_intent("cancel", operation_id, {"handle": value, "reason": str(reason)})
        response = self._invoke("hub_agent_cancel", payload, identity=operation_id)
        self._journal_effect("cancel", operation_id, response)
        return response

    def recover(self, handle: str | Mapping[str, Any]) -> dict[str, Any]:
        """Reconnect by observing the existing handle; never redispatch it."""
        return self.status(handle)

    reconnect = recover


__all__ = [
    "AGENT_SCHEMA", "CAPABILITY", "HubQueueAgentClient", "HubQueueAgentError",
    "HubQueueAgentJournal", "HubQueueAgentUnavailable", "IPC_SCHEMA",
]
