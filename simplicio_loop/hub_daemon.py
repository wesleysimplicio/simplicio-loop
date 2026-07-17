"""Singleton Hub lock and versioned in-process IPC contract."""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Set


IPC_SCHEMA = "simplicio.hub-ipc/v1"
IPC_VERSION = 1
METHODS = frozenset(("register", "submit", "claim", "heartbeat", "progress", "cancel", "result", "report"))


class HubError(RuntimeError):
    """Base Hub error."""


class HubAlreadyRunning(HubError):
    """Raised when another live process owns the singleton lock."""


class HubProtocolError(HubError):
    """Raised for invalid or unknown IPC envelopes."""


def _pid_alive(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return pid == os.getpid()
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


class HubLock:
    """Exclusive PID lock with deterministic stale-lock reclamation."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._owned = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                descriptor = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump({"pid": os.getpid(), "schema": IPC_SCHEMA}, stream)
                self._owned = True
                return
            except FileExistsError:
                try:
                    payload = json.loads(self.path.read_text(encoding="utf-8"))
                    pid = int(payload.get("pid", 0))
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    pid = 0
                if _pid_alive(pid):
                    raise HubAlreadyRunning("Hub singleton is already owned")
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    continue

    def release(self) -> None:
        if not self._owned:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self._owned = False

    def __enter__(self) -> "HubLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


@dataclass(frozen=True)
class HubEnvelope:
    """Versioned request envelope; payload is opaque to transport."""

    request_id: str
    method: str
    payload: Dict[str, Any]
    version: int = IPC_VERSION
    schema: str = IPC_SCHEMA

    def encode(self) -> str:
        if not self.request_id or self.method not in METHODS:
            raise HubProtocolError("request_id and known method are required")
        return json.dumps({
            "schema": self.schema,
            "version": self.version,
            "request_id": self.request_id,
            "method": self.method,
            "payload": self.payload,
        }, sort_keys=True)

    @classmethod
    def decode(cls, raw: str) -> "HubEnvelope":
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HubProtocolError("invalid JSON envelope") from exc
        if value.get("schema") != IPC_SCHEMA or value.get("version") != IPC_VERSION:
            raise HubProtocolError("unsupported IPC schema/version")
        if value.get("method") not in METHODS or not value.get("request_id"):
            raise HubProtocolError("invalid request envelope")
        payload = value.get("payload")
        if not isinstance(payload, dict):
            raise HubProtocolError("payload must be an object")
        return cls(str(value["request_id"]), str(value["method"]), dict(payload))


class HubDaemon:
    """In-process lifecycle coordinator behind a singleton lock."""

    def __init__(self, lock_path: str) -> None:
        self.lock = HubLock(lock_path)
        self.started = False
        self.clients: Set[str] = set()
        self.jobs: Dict[str, Dict[str, Any]] = {}

    def start(self) -> None:
        if self.started:
            return
        self.lock.acquire()
        self.started = True

    def stop(self) -> None:
        self.jobs.clear()
        self.clients.clear()
        self.started = False
        self.lock.release()

    def handle(self, envelope: HubEnvelope) -> Dict[str, Any]:
        if not self.started:
            raise HubError("Hub is not started")
        if envelope.method == "register":
            client_id = str(envelope.payload.get("client_id") or "")
            if not client_id:
                raise HubProtocolError("client_id is required")
            self.clients.add(client_id)
            return {"ok": True, "client_id": client_id, "state": "registered"}
        job_id = str(envelope.payload.get("job_id") or "")
        if not job_id:
            raise HubProtocolError("job_id is required")
        if envelope.method == "submit":
            if job_id in self.jobs:
                raise HubProtocolError("job already exists")
            self.jobs[job_id] = {
                "job_id": job_id,
                "client_id": envelope.payload.get("client_id"),
                "state": "queued",
                "progress": 0,
                "result": None,
            }
            return {"ok": True, "job": dict(self.jobs[job_id])}
        job = self.jobs.get(job_id)
        if job is None:
            raise HubProtocolError("unknown job")
        if envelope.method == "claim":
            if job["state"] != "queued":
                raise HubProtocolError("job is not claimable")
            job["state"] = "claimed"
        elif envelope.method == "heartbeat":
            if job["state"] not in ("claimed", "running"):
                raise HubProtocolError("job has no active lease")
        elif envelope.method == "progress":
            progress = int(envelope.payload.get("progress", -1))
            if not 0 <= progress <= 100:
                raise HubProtocolError("progress must be between 0 and 100")
            job["progress"] = progress
            job["state"] = "running"
        elif envelope.method == "cancel":
            job["state"] = "cancelled"
        elif envelope.method == "result":
            job["result"] = envelope.payload.get("result")
            job["state"] = "completed"
        elif envelope.method == "report":
            return {"ok": True, "job": dict(job), "clients": sorted(self.clients)}
        return {"ok": True, "job": dict(job)}


class HubClient:
    """Small typed client facade for tests and future IPC transports."""

    def __init__(self, daemon: HubDaemon, client_id: str) -> None:
        self.daemon = daemon
        self.client_id = client_id

    def request(self, request_id: str, method: str, **payload: Any) -> Dict[str, Any]:
        payload.setdefault("client_id", self.client_id)
        return self.daemon.handle(HubEnvelope(request_id, method, payload))
