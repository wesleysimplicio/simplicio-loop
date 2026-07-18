"""Singleton Hub lock and versioned in-process IPC contract."""

import json
import os
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Set


IPC_SCHEMA = "simplicio.hub-ipc/v1"
IPC_VERSION = 1
METHODS = frozenset(
    ("register", "submit", "claim", "heartbeat", "progress", "cancel", "result", "report", "ping")
)


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
        if envelope.method == "ping":
            return {"ok": True, "started": self.started, "clients": len(self.clients), "jobs": len(self.jobs)}
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


def _recv_line(conn: socket.socket) -> str:
    chunks = []
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    return b"".join(chunks).decode("utf-8").strip()


class HubSocketServer:
    """Unix domain socket transport for HubDaemon, restricted to the owning user.

    POSIX-only. A Windows deployment needs a named-pipe transport instead;
    this class is not usable there and no fallback is implemented yet.
    """

    def __init__(self, daemon: HubDaemon, socket_path: str) -> None:
        self.daemon = daemon
        self.socket_path = Path(socket_path)
        self._server: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        os.chmod(str(self.socket_path), 0o600)
        server.listen(8)
        self._server = server
        self._running = True
        self._thread = threading.Thread(target=self._serve_forever, daemon=True)
        self._thread.start()

    def _serve_forever(self) -> None:
        assert self._server is not None
        self._server.settimeout(0.5)
        while self._running:
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

    def _handle_conn(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(5)
            try:
                raw = _recv_line(conn)
                if not raw:
                    return
                try:
                    envelope = HubEnvelope.decode(raw)
                    response = self.daemon.handle(envelope)
                except HubError as exc:
                    response = {"ok": False, "error": str(exc)}
                conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
            except OSError:
                return

    def shutdown(self) -> None:
        if self._running:
            self._running = False
            if self._server is not None:
                try:
                    self._server.close()
                except OSError:
                    pass
                self._server = None
            if self._thread is not None:
                self._thread.join(timeout=2)
                self._thread = None
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass


class HubSocketClient:
    """Standalone client for HubSocketServer; usable with no daemon in-process."""

    def __init__(self, socket_path: str, timeout: float = 5.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def request(self, request_id: str, method: str, **payload: Any) -> Dict[str, Any]:
        envelope = HubEnvelope(request_id, method, payload)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(self.timeout)
            sock.connect(self.socket_path)
            sock.sendall((envelope.encode() + "\n").encode("utf-8"))
            raw = _recv_line(sock)
        if not raw:
            raise HubError("no response from Hub daemon")
        return json.loads(raw)


def doctor(lock_path: str, socket_path: str) -> Dict[str, Any]:
    """Report whether a live daemon owns the lock and answers on the socket."""
    lock_file = Path(lock_path)
    result: Dict[str, Any] = {
        "lock_exists": lock_file.exists(),
        "lock_pid_alive": False,
        "socket_exists": Path(socket_path).exists(),
        "socket_reachable": False,
    }
    if lock_file.exists():
        try:
            payload = json.loads(lock_file.read_text(encoding="utf-8"))
            pid = int(payload.get("pid", 0))
            result["pid"] = pid
            result["lock_pid_alive"] = _pid_alive(pid)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    if result["socket_exists"]:
        try:
            client = HubSocketClient(socket_path, timeout=1.0)
            response = client.request("doctor", "ping")
            result["socket_reachable"] = bool(response.get("ok"))
        except (OSError, HubError, json.JSONDecodeError):
            result["socket_reachable"] = False
    return result
