"""Singleton Hub lock and versioned in-process IPC contract."""

import json
import hashlib
import asyncio
import os
import socket
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Set

from .hub_queue_retry import HubRetryQueue
from .hub_scheduler import FairScheduler, QuotaExceededError, ScheduledJob, SchedulerError
from .process_enforcement import ProcessRegistry
from .process_supervisor import ProcessSpec, ProcessSpecError
from .process_supervisor_rust import backend_name, run_with_fallback


IPC_SCHEMA = "simplicio.hub-ipc/v1"
IPC_VERSION = 1
METHODS = frozenset(
    (
        "register", "submit", "claim", "claim_next", "heartbeat", "progress",
        "cancel", "result", "report", "execute", "ping", "scheduler_status",
    )
)


class HubError(RuntimeError):
    """Base Hub error."""


class HubAlreadyRunning(HubError):
    """Raised when another live process owns the singleton lock."""


class HubProtocolError(HubError):
    """Raised for invalid or unknown IPC envelopes."""


class HubBackpressureError(HubProtocolError):
    """Raised when a submit is rejected by a scheduler quota (client/workspace/global)."""

    def __init__(self, quota_error: QuotaExceededError) -> None:
        self.signal = quota_error.to_backpressure_signal()
        super().__init__(str(quota_error))


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

    def __init__(
        self,
        lock_path: str,
        queue_path: Optional[str] = None,
        scheduler: Optional[FairScheduler] = None,
        *,
        process_registry: Optional[ProcessRegistry] = None,
    ) -> None:
        self.lock = HubLock(lock_path)
        self.started = False
        self.clients: Set[str] = set()
        self.queue_path = queue_path or (str(self.lock.path) + ".jobs.db")
        self.queue = HubRetryQueue(self.queue_path)
        self.scheduler = scheduler if scheduler is not None else FairScheduler()
        self._queue_lock = threading.Lock()
        self.process_registry = process_registry or ProcessRegistry()

    def start(self) -> None:
        if self.started:
            return
        self.lock.acquire()
        self.started = True

    def stop(self) -> None:
        self.clients.clear()
        self.started = False
        self.lock.release()
        self.queue.close()

    def handle(self, envelope: HubEnvelope) -> Dict[str, Any]:
        if not self.started:
            raise HubError("Hub is not started")
        if envelope.method == "ping":
            with self._queue_lock:
                job_count = self.queue.count()
            return {"ok": True, "started": self.started, "clients": len(self.clients), "jobs": job_count}
        if envelope.method == "register":
            client_id = str(envelope.payload.get("client_id") or "")
            if not client_id:
                raise HubProtocolError("client_id is required")
            self.clients.add(client_id)
            return {"ok": True, "client_id": client_id, "state": "registered"}
        if envelope.method == "scheduler_status":
            return {"ok": True, "scheduler": self.scheduler.status()}
        if envelope.method == "claim_next":
            worker_id = str(envelope.payload.get("client_id") or "worker")
            with self._queue_lock:
                job = self._claim_next_locked(worker_id)
            return {"ok": True, "job": job}
        if envelope.method == "execute":
            raw_spec = envelope.payload.get("process_spec")
            if not isinstance(raw_spec, dict):
                raise HubProtocolError("process_spec must be an object")
            allowed = {
                "argv", "cwd", "cwd_allowlist", "env", "env_allowlist",
                "timeout_seconds", "max_output_bytes", "priority",
                "idempotency_key", "shell",
            }
            unknown = sorted(set(raw_spec) - allowed - {"schema", "spec_hash"})
            if unknown:
                raise HubProtocolError("unknown ProcessSpec fields: " + ", ".join(unknown))
            if raw_spec.get("schema") not in (None, "simplicio.process-spec/v1"):
                raise HubProtocolError("unsupported ProcessSpec schema")
            try:
                spec = ProcessSpec(
                    argv=tuple(raw_spec.get("argv", ())),
                    cwd=raw_spec.get("cwd"),
                    cwd_allowlist=tuple(raw_spec.get("cwd_allowlist", ())),
                    env=dict(raw_spec.get("env", {})),
                    env_allowlist=tuple(raw_spec.get("env_allowlist", ())),
                    timeout_seconds=raw_spec.get("timeout_seconds", 30.0),
                    max_output_bytes=int(raw_spec.get("max_output_bytes", 65536)),
                    priority=int(raw_spec.get("priority", 0)),
                    idempotency_key=str(raw_spec.get("idempotency_key", "")),
                    shell=bool(raw_spec.get("shell", False)),
                )
            except (TypeError, ValueError, ProcessSpecError) as exc:
                raise HubProtocolError(f"invalid ProcessSpec: {exc}") from exc
            registered: Dict[str, int] = {}

            def on_spawned(process: Any) -> None:
                registered["pid"] = process.pid
                self.process_registry.register(
                    process.pid,
                    lease_id=spec.idempotency_key or f"hub-execute-{envelope.request_id}",
                    spec_hash=spec.spec_hash,
                    argv=spec.argv,
                )

            lease_id = spec.idempotency_key or f"hub-execute-{envelope.request_id}"
            try:
                result = run_with_fallback(spec, on_spawned=on_spawned)
            except (OSError, RuntimeError, asyncio.CancelledError) as exc:
                raise HubError(f"supervisor execution failed: {exc}") from exc
            finally:
                if "pid" in registered:
                    self.process_registry.unregister(registered["pid"])
            return {
                "ok": True, "backend": backend_name(), "result": result.to_dict(),
                "lease_id": lease_id,
            }
        if envelope.method == "cancel":
            lease_id = str(envelope.payload.get("lease_id") or "")
            process_cancel = self.process_registry.terminate(lease_id) if lease_id else None
            job_id = str(envelope.payload.get("job_id") or "")
            if not job_id:
                if process_cancel is None:
                    raise HubProtocolError("job_id or lease_id is required")
                return {"ok": True, "process": process_cancel}
            with self._queue_lock:
                task_id = self.queue.find_task_id(job_id)
                if task_id is None:
                    raise HubProtocolError("unknown job")
                row = self.queue.get_row(task_id)
                job = dict(row["payload"])
                job["state"] = "cancelled"
                self.scheduler.cancel(job_id)
                self.queue.update_payload(task_id, job)
            response = {"ok": True, "job": dict(job)}
            if process_cancel is not None:
                response["process"] = process_cancel
            return response
        job_id = str(envelope.payload.get("job_id") or "")
        if not job_id:
            raise HubProtocolError("job_id is required")
        with self._queue_lock:
            if envelope.method == "submit":
                if self.queue.find_task_id(job_id) is not None:
                    raise HubProtocolError("job already exists")
                client_id = str(envelope.payload.get("client_id") or "")
                try:
                    self.scheduler.enqueue(
                        ScheduledJob(
                            task_id=job_id,
                            client_id=client_id,
                            weight=int(envelope.payload.get("weight", 1)),
                            cost=int(envelope.payload.get("cost", 1)),
                            workspace_id=str(envelope.payload.get("workspace_id") or "default"),
                            priority=str(envelope.payload.get("priority") or "background"),
                        )
                    )
                except QuotaExceededError as exc:
                    raise HubBackpressureError(exc) from exc
                except SchedulerError as exc:
                    raise HubProtocolError(str(exc)) from exc
                job = {
                    "job_id": job_id,
                    "client_id": envelope.payload.get("client_id"),
                    "state": "queued",
                    "progress": 0,
                    "result": None,
                }
                self.queue.submit(job, idempotency_key=job_id)
                return {"ok": True, "job": dict(job)}
            task_id = self.queue.find_task_id(job_id)
            if task_id is None:
                raise HubProtocolError("unknown job")
            row = self.queue.get_row(task_id)
            job = dict(row["payload"])
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
            elif envelope.method == "result":
                job["result"] = envelope.payload.get("result")
                job["state"] = "completed"
                try:
                    self.scheduler.complete(job_id)
                except SchedulerError:
                    pass
            elif envelope.method == "report":
                return {"ok": True, "job": dict(job), "clients": sorted(self.clients)}
            self.queue.update_payload(task_id, job)
            return {"ok": True, "job": dict(job)}

    def _claim_next_locked(self, worker_id: str) -> Optional[Dict[str, Any]]:
        """Pop the next task the FairScheduler's DRR/quota order selects, skipping any
        stale scheduler entries that no longer map to a live queued job (defensive only:
        cancel()/complete() already retire scheduler entries within this same session)."""
        max_attempts = 4096
        for _ in range(max_attempts):
            scheduled = self.scheduler.next()
            if scheduled is None:
                return None
            task_id = self.queue.find_task_id(scheduled.task_id)
            if task_id is None:
                self._retire_scheduler_entry(scheduled.task_id)
                continue
            row = self.queue.get_row(task_id)
            job = dict(row["payload"])
            if job.get("state") != "queued":
                self._retire_scheduler_entry(scheduled.task_id)
                continue
            job["state"] = "claimed"
            self.queue.update_payload(task_id, job)
            return job
        return None

    def _retire_scheduler_entry(self, task_id: str) -> None:
        try:
            self.scheduler.complete(task_id)
        except SchedulerError:
            pass


class HubClient:
    """Small typed client facade for tests and future IPC transports."""

    def __init__(self, daemon: HubDaemon, client_id: str) -> None:
        self.daemon = daemon
        self.client_id = client_id

    def request(self, request_id: str, method: str, **payload: Any) -> Dict[str, Any]:
        payload.setdefault("client_id", self.client_id)
        return self.daemon.handle(HubEnvelope(request_id, method, payload))


def default_transport() -> str:
    return "named-pipe" if os.name == "nt" else "unix"


def default_endpoint(root: Optional[str] = None) -> str:
    if os.name == "nt":
        suffix = "default" if not root else hashlib.sha256(os.path.abspath(root).encode("utf-8")).hexdigest()[:12]
        return r"\\.\pipe\simplicio-loop-hub-%s" % suffix
    return str((Path(root) if root else Path(tempfile.gettempdir())) / "simplicio-loop-hub.sock")


def _pipe_listener(endpoint: str):
    from multiprocessing.connection import Listener
    return Listener(endpoint, family="AF_PIPE")


def _pipe_client(endpoint: str):
    from multiprocessing.connection import Client
    return Client(endpoint, family="AF_PIPE")


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
    """Local IPC transport for HubDaemon: Unix socket on POSIX, named pipe on Windows."""

    def __init__(self, daemon: HubDaemon, socket_path: str, transport: Optional[str] = None) -> None:
        self.daemon = daemon
        self.socket_path = Path(socket_path)
        self.endpoint = socket_path
        self.transport = transport or default_transport()
        if self.transport not in {"unix", "named-pipe"}:
            raise ValueError("transport must be unix or named-pipe")
        self._server: Optional[socket.socket] = None
        self._listener = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        if self.transport == "named-pipe":
            if os.name != "nt":
                raise RuntimeError("named-pipe transport requires Windows")
            self._listener = _pipe_listener(self.endpoint)
        else:
            self.socket_path.parent.mkdir(parents=True, exist_ok=True)
            if self.socket_path.exists():
                self.socket_path.unlink()
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(self.socket_path))
            os.chmod(str(self.socket_path), 0o600)
            server.listen(64)
            self._server = server
            self._server.settimeout(0.5)
        self._running = True
        self._thread = threading.Thread(target=self._serve_forever, daemon=True)
        self._thread.start()

    def _serve_forever(self) -> None:
        while self._running:
            try:
                conn = self._listener.accept() if self.transport == "named-pipe" else self._server.accept()[0]
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

    def _handle_conn(self, conn: socket.socket) -> None:
        try:
            if self.transport == "named-pipe":
                raw = conn.recv_bytes().decode("utf-8")
            else:
                with conn:
                    conn.settimeout(5)
                    raw = _recv_line(conn)
                    self._dispatch_socket(conn, raw)
                    return
            response = self._dispatch(raw)
            conn.send_bytes(json.dumps(response).encode("utf-8"))
        except (OSError, EOFError):
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch(self, raw: str) -> Dict[str, Any]:
        try:
            return self.daemon.handle(HubEnvelope.decode(raw))
        except HubError as exc:
            return {"ok": False, "error": str(exc)}

    def _dispatch_socket(self, conn, raw: str) -> None:
        if raw:
            conn.sendall((json.dumps(self._dispatch(raw)) + "\n").encode("utf-8"))

    def shutdown(self) -> None:
        if self._running:
            self._running = False
            if self._server is not None:
                try:
                    self._server.close()
                except OSError:
                    pass
                self._server = None
            if self._listener is not None:
                try:
                    self._listener.close()
                except OSError:
                    pass
                self._listener = None
            if self._thread is not None:
                self._thread.join(timeout=2)
                self._thread = None
        if self.transport == "unix":
            try:
                self.socket_path.unlink()
            except FileNotFoundError:
                pass

    stop = shutdown


class HubSocketClient:
    """Standalone client for HubSocketServer; usable with no daemon in-process."""

    def __init__(self, socket_path: str, timeout: float = 5.0, transport: Optional[str] = None) -> None:
        self.socket_path = socket_path
        self.timeout = timeout
        self.endpoint = socket_path
        self.transport = transport or default_transport()

    def request(self, request_id: str, method: str, **payload: Any) -> Dict[str, Any]:
        return self.request_raw(HubEnvelope(request_id, method, payload).encode())

    def request_raw(self, raw: str) -> Dict[str, Any]:
        if self.transport == "named-pipe":
            conn = _pipe_client(self.endpoint)
            try:
                conn.send_bytes(raw.encode("utf-8"))
                raw = conn.recv_bytes().decode("utf-8")
            finally:
                conn.close()
        else:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(self.socket_path)
                sock.sendall((raw + "\n").encode("utf-8"))
                raw = _recv_line(sock)
        if not raw:
            raise HubError("no response from Hub daemon")
        return json.loads(raw)


def doctor(lock_path: str, socket_path: str, transport: Optional[str] = None) -> Dict[str, Any]:
    """Report whether a live daemon owns the lock and answers on the socket."""
    lock_file = Path(lock_path)
    result: Dict[str, Any] = {
        "lock_exists": lock_file.exists(),
        "lock_pid_alive": False,
        "socket_exists": (transport or default_transport()) == "named-pipe" or Path(socket_path).exists(),
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
            client = HubSocketClient(socket_path, timeout=1.0, transport=transport)
            response = client.request("doctor", "ping")
            result["socket_reachable"] = bool(response.get("ok"))
        except (OSError, HubError, json.JSONDecodeError):
            result["socket_reachable"] = False
    return result


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
    daemon = HubDaemon(args.lock)
    server = HubSocketServer(daemon, args.endpoint, args.transport)
    try:
        daemon.start()
        server.start()
        print(json.dumps({"ready": True, "endpoint": args.endpoint, "transport": args.transport}), flush=True)
        threading.Event().wait()
    except KeyboardInterrupt:
        return 0
    finally:
        server.shutdown()
        daemon.stop()
    return 0
