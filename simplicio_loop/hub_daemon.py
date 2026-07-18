"""Singleton Hub lock and versioned in-process IPC contract."""

import json
import hashlib
import os
import socket
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Set


IPC_SCHEMA = "simplicio.hub-ipc/v1"
IPC_VERSION = 1
METHODS = frozenset(("ping", "register", "submit", "claim", "heartbeat", "progress", "cancel", "result", "report"))


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
            return {"ok": True, "state": "ready", "clients": len(self.clients), "jobs": len(self.jobs)}
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


def default_endpoint(root: Optional[str] = None) -> str:
    """Return a deterministic local endpoint for the current platform."""
    if os.name == "nt":
        suffix = "default" if not root else hashlib.sha256(os.path.abspath(root).encode("utf-8")).hexdigest()[:12]
        return r"\\.\pipe\simplicio-loop-hub-%s" % suffix
    base = Path(root) if root else Path(tempfile.gettempdir())
    return str(base / "simplicio-loop-hub.sock")


def default_transport() -> str:
    return "named-pipe" if os.name == "nt" else "unix"


def _pipe_listener(endpoint: str):
    from multiprocessing.connection import Listener
    return Listener(endpoint, family="AF_PIPE")


def _pipe_client(endpoint: str):
    from multiprocessing.connection import Client
    return Client(endpoint, family="AF_PIPE")


class HubSocketServer:
    """Real local IPC server around ``HubDaemon``.

    POSIX uses a filesystem Unix socket with mode 0600. Windows uses a named pipe through the
    stdlib multiprocessing connection implementation; TCP is intentionally not selected by
    default and remains an explicit future fallback.
    """

    def __init__(self, daemon: HubDaemon, endpoint: str, transport: Optional[str] = None) -> None:
        self.daemon = daemon
        self.endpoint = endpoint
        self.transport = transport or default_transport()
        if self.transport not in {"unix", "named-pipe"}:
            raise ValueError("transport must be unix or named-pipe")
        self._listener = None
        self._socket = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if self.transport == "named-pipe" and os.name != "nt":
            raise RuntimeError("named-pipe transport requires Windows")
        self.daemon.start()
        self._stop.clear()
        if self.transport == "named-pipe":
            self._listener = _pipe_listener(self.endpoint)
        else:
            path = Path(self.endpoint)
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._socket.bind(str(path))
            os.chmod(path, 0o600)
            self._socket.listen(32)
            self._socket.settimeout(0.2)
        self._thread = threading.Thread(target=self._serve, name="simplicio-hub", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection = self._listener.accept() if self.transport == "named-pipe" else self._socket.accept()[0]
            except (OSError, EOFError):
                if self._stop.is_set():
                    return
                continue
            threading.Thread(target=self._serve_connection, args=(connection,), daemon=True).start()

    def _dispatch(self, raw: str) -> str:
        try:
            envelope = HubEnvelope.decode(raw)
            result = self.daemon.handle(envelope)
            return json.dumps({"ok": True, "request_id": envelope.request_id, "result": result})
        except HubError as exc:
            return json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__})
        except (TypeError, ValueError) as exc:
            return json.dumps({"ok": False, "error": str(exc), "error_type": "HubProtocolError"})

    def _serve_connection(self, connection) -> None:
        try:
            if self.transport == "named-pipe":
                raw = connection.recv_bytes().decode("utf-8")
                connection.send_bytes(self._dispatch(raw).encode("utf-8"))
            else:
                with connection:
                    reader = connection.makefile("rb")
                    raw = reader.readline().decode("utf-8")
                    connection.sendall((self._dispatch(raw) + "\n").encode("utf-8"))
        except (OSError, EOFError):
            pass
        finally:
            try:
                connection.close()
            except OSError:
                pass

    def stop(self) -> None:
        self._stop.set()
        for resource in (self._listener, self._socket):
            if resource is not None:
                try:
                    resource.close()
                except OSError:
                    pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._thread = None
        self._listener = None
        self._socket = None
        if self.transport == "unix":
            try:
                Path(self.endpoint).unlink()
            except FileNotFoundError:
                pass
        self.daemon.stop()

    def __enter__(self) -> "HubSocketServer":
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.stop()


class HubSocketClient:
    """One-request local IPC client; absence of a server is a clean connection error."""

    def __init__(self, endpoint: str, transport: Optional[str] = None, timeout: float = 5.0) -> None:
        self.endpoint = endpoint
        self.transport = transport or default_transport()
        self.timeout = timeout

    def raw_request(self, raw: str) -> Dict[str, Any]:
        if self.transport == "named-pipe":
            connection = _pipe_client(self.endpoint)
            try:
                connection.send_bytes(raw.encode("utf-8"))
                response = json.loads(connection.recv_bytes().decode("utf-8"))
            finally:
                connection.close()
        else:
            with socket.create_connection(self.endpoint, timeout=self.timeout) as connection:
                connection.sendall((raw + "\n").encode("utf-8"))
                response = json.loads(connection.makefile("rb").readline().decode("utf-8"))
        if not response.get("ok"):
            raise HubProtocolError(response.get("error") or "Hub request failed")
        return dict(response.get("result") or {})

    def request(self, request_id: str, method: str, **payload: Any) -> Dict[str, Any]:
        return self.raw_request(HubEnvelope(request_id, method, payload).encode())


def doctor(lock_path: str, endpoint: str, transport: Optional[str] = None) -> Dict[str, Any]:
    """Check singleton ownership and real endpoint reachability without starting a daemon."""
    lock = Path(lock_path)
    lock_exists = lock.exists()
    pid = 0
    if lock_exists:
        try:
            pid = int(json.loads(lock.read_text(encoding="utf-8")).get("pid", 0))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pid = 0
    selected_transport = transport or default_transport()
    endpoint_exists = (selected_transport == "named-pipe" and os.name == "nt") or Path(endpoint).exists()
    reachable = False
    if endpoint_exists:
        try:
            HubSocketClient(endpoint, transport=selected_transport, timeout=0.5).request("doctor", "ping")
            reachable = True
        except (OSError, EOFError, HubError, ValueError):
            reachable = False
    return {
        "schema": "simplicio.hub-doctor/v1",
        "transport": selected_transport,
        "lock_exists": lock_exists,
        "pid": pid,
        "pid_alive": _pid_alive(pid) if pid else False,
        "endpoint": endpoint,
        "endpoint_exists": endpoint_exists,
        "reachable": reachable,
        "ok": reachable and (not lock_exists or _pid_alive(pid)),
    }


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="simplicio-hub")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--lock", default=str(Path(tempfile.gettempdir()) / "simplicio-loop-hub.lock"))
    serve.add_argument("--endpoint", default=default_endpoint())
    serve.add_argument("--transport", choices=("unix", "named-pipe"), default=default_transport())
    check = sub.add_parser("doctor")
    check.add_argument("--lock", default=str(Path(tempfile.gettempdir()) / "simplicio-loop-hub.lock"))
    check.add_argument("--endpoint", default=default_endpoint())
    check.add_argument("--transport", choices=("unix", "named-pipe"), default=default_transport())
    args = parser.parse_args(argv)
    if args.command == "doctor":
        print(json.dumps(doctor(args.lock, args.endpoint, args.transport), sort_keys=True))
        return 0
    server = HubSocketServer(HubDaemon(args.lock), args.endpoint, args.transport)
    try:
        server.start()
        print(json.dumps({"ready": True, "endpoint": args.endpoint, "transport": args.transport}), flush=True)
        threading.Event().wait()
    except KeyboardInterrupt:
        return 0
    finally:
        server.stop()
    return 0
