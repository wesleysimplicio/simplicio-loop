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

from .process_supervisor import ProcessSpec, ProcessSpecError
from .process_supervisor_rust import backend_name, run_with_fallback
from .hub_governor import RESOURCE_NAMES, ResourceGovernor, ResourceLimits, ResourceRequest
from .hub_queue_retry import HubRetryQueue
from .hub_scheduler import FairScheduler
from .hub_service import ClaimedJob, HubService
from .map_service import MapServiceRegistry, RepositoryIdentity
from .map_service_single_flight import SingleFlightMapStore
from .map_service_watchers import MapWatcherManager


IPC_SCHEMA = "simplicio.hub-ipc/v1"
IPC_VERSION = 1
METHODS = frozenset(
    (
        "register", "submit", "claim", "heartbeat", "progress", "cancel", "result", "report",
        "execute", "ping",
        # #503/#504/#505/#506 IPC wiring: expose the composed HubService (durable queue +
        # fair scheduler + resource governor) over the same envelope/dispatch contract as
        # the pre-existing in-memory job verbs above, rather than a second protocol.
        "hub_submit", "hub_claim", "hub_complete", "hub_fail", "hub_status",
        # #512/#513 IPC wiring: expose the map service (registry + single-flight store +
        # watcher manager) the same way. Deliberately in-memory only, no persistence
        # layer (unlike hub_submit's durable queue) - a daemon restart starts clean; see
        # test_hub_daemon_map_ipc.py for the honest restart-boundary test.
        "map_register", "map_watch", "map_emit", "map_flush", "map_status", "map_gc",
    )
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

    def __init__(
        self,
        lock_path: str,
        *,
        queue_path: Optional[str] = None,
        resource_limits: Optional[ResourceLimits] = None,
    ) -> None:
        self.lock = HubLock(lock_path)
        self.started = False
        self.clients: Set[str] = set()
        self.jobs: Dict[str, Dict[str, Any]] = {}
        # Every ResourceLimits field defaults to 0, and ResourceGovernor treats a 0 limit
        # as "unbounded" (falsy) - so a HubDaemon constructed the same way as before this
        # change gets a HubService with no resource budget enforced by default, matching
        # prior behavior exactly unless the caller opts in with real limits.
        self._queue_path = queue_path or (lock_path + ".hub-queue.db")
        self._resource_limits = resource_limits or ResourceLimits()
        self.service: Optional[HubService] = None
        self._claims: Dict[str, ClaimedJob] = {}
        # #512/#513: registry/store/watchers are pure in-memory (unlike the durable
        # queue above) - rebuilt fresh every start(), never persisted across stop().
        self.map_registry: Optional[MapServiceRegistry] = None
        self.map_store: Optional[SingleFlightMapStore] = None
        self.map_watchers: Optional[MapWatcherManager] = None
        self._map_watch_tokens: Dict[str, str] = {}

    def start(self) -> None:
        if self.started:
            return
        self.lock.acquire()
        queue = HubRetryQueue(self._queue_path)
        scheduler = FairScheduler()
        governor = ResourceGovernor(self._resource_limits)
        self.service = HubService(queue, scheduler, governor)
        self.map_registry = MapServiceRegistry()
        self.map_store = SingleFlightMapStore(self.map_registry)
        self.map_watchers = MapWatcherManager(self.map_registry, self.map_store)
        self.started = True

    def stop(self) -> None:
        self.jobs.clear()
        self.clients.clear()
        self._claims.clear()
        if self.service is not None:
            self.service.queue.close()
            self.service = None
        if self.map_watchers is not None:
            self.map_watchers.close()
        self.map_registry = None
        self.map_store = None
        self.map_watchers = None
        self._map_watch_tokens.clear()
        self.started = False
        self.lock.release()

    def handle(self, envelope: HubEnvelope) -> Dict[str, Any]:
        if not self.started:
            raise HubError("Hub is not started")
        if envelope.method == "ping":
            return {"ok": True, "started": self.started, "clients": len(self.clients), "jobs": len(self.jobs)}
        if envelope.method == "hub_submit":
            payload = envelope.payload.get("payload")
            if not isinstance(payload, dict):
                raise HubProtocolError("payload must be an object")
            idempotency_key = str(envelope.payload.get("idempotency_key") or "")
            client_id = str(envelope.payload.get("client_id") or "")
            if not idempotency_key or not client_id:
                raise HubProtocolError("idempotency_key and client_id are required")
            task_id = self.service.submit(
                payload,
                idempotency_key=idempotency_key,
                client_id=client_id,
                workspace_id=str(envelope.payload.get("workspace_id") or "default"),
                weight=int(envelope.payload.get("weight", 1)),
                cost=int(envelope.payload.get("cost", 1)),
                max_attempts=int(envelope.payload.get("max_attempts", 3)),
            )
            return {"ok": True, "task_id": task_id}
        if envelope.method == "hub_claim":
            worker_id = str(envelope.payload.get("worker_id") or "")
            if not worker_id:
                raise HubProtocolError("worker_id is required")
            raw_request = envelope.payload.get("request") or {}
            if not isinstance(raw_request, dict) or set(raw_request) - set(RESOURCE_NAMES):
                raise HubProtocolError("request must contain only known resource fields")
            request = ResourceRequest(**{name: int(raw_request.get(name, 0)) for name in RESOURCE_NAMES})
            claimed = self.service.claim(
                worker_id, request,
                ttl=float(envelope.payload.get("ttl", 30.0)),
                max_candidates=int(envelope.payload.get("max_candidates", 8)),
            )
            if claimed is None:
                return {"ok": True, "claimed": None}
            # RetryLease/ResourceLease are kept server-side, keyed by task_id, rather than
            # round-tripped over the wire - the client only needs task_id to complete/fail.
            self._claims[claimed.task_id] = claimed
            return {
                "ok": True,
                "claimed": {
                    "task_id": claimed.task_id, "client_id": claimed.client_id,
                    "workspace_id": claimed.workspace_id, "payload": claimed.payload,
                },
            }
        if envelope.method == "hub_complete":
            task_id = str(envelope.payload.get("task_id") or "")
            claimed = self._claims.pop(task_id, None)
            if claimed is None:
                raise HubProtocolError("no active claim for task_id")
            self.service.complete(claimed)
            return {"ok": True, "task_id": task_id}
        if envelope.method == "hub_fail":
            task_id = str(envelope.payload.get("task_id") or "")
            claimed = self._claims.pop(task_id, None)
            if claimed is None:
                raise HubProtocolError("no active claim for task_id")
            outcome = self.service.fail(
                claimed,
                error_code=str(envelope.payload.get("error_code") or "unknown"),
                backoff=float(envelope.payload.get("backoff", 0.0)),
            )
            return {"ok": True, "task_id": task_id, "outcome": outcome}
        if envelope.method == "hub_status":
            return {"ok": True, "status": self.service.status()}
        if envelope.method == "map_register":
            fields = {
                name: envelope.payload.get(name)
                for name in (
                    "repository", "canonical_root", "default_branch", "worktree_root",
                    "base_sha", "dirty", "dirty_fingerprint", "mapper_config",
                )
                if envelope.payload.get(name) is not None
            }
            try:
                identity = RepositoryIdentity(**fields)
            except TypeError as exc:
                raise HubProtocolError("invalid map_register payload: %s" % exc) from exc
            try:
                identity_key = self.map_registry.register(identity)
            except Exception as exc:  # MapServiceError subclasses
                raise HubProtocolError("map_register failed: %s" % exc) from exc
            return {"ok": True, "identity_key": identity_key}
        if envelope.method == "map_watch":
            identity_key = str(envelope.payload.get("identity_key") or "")
            if not identity_key:
                raise HubProtocolError("identity_key is required")
            try:
                token = self.map_watchers.watch(
                    identity_key, lambda _event: None,
                    debounce_seconds=float(envelope.payload.get("debounce_seconds", 0.05)),
                )
            except Exception as exc:
                raise HubProtocolError("map_watch failed: %s" % exc) from exc
            self._map_watch_tokens[identity_key] = token
            return {"ok": True, "token": token}
        if envelope.method == "map_emit":
            identity_key = str(envelope.payload.get("identity_key") or "")
            paths = envelope.payload.get("paths") or []
            if not identity_key or not isinstance(paths, list):
                raise HubProtocolError("identity_key and a paths list are required")
            try:
                self.map_watchers.emit(identity_key, paths)
            except Exception as exc:
                raise HubProtocolError("map_emit failed: %s" % exc) from exc
            return {"ok": True}
        if envelope.method == "map_flush":
            fired = self.map_watchers.flush(force=bool(envelope.payload.get("force", False)))
            return {"ok": True, "fired": fired}
        if envelope.method == "map_status":
            return {"ok": True, "status": self.map_watchers.status()}
        if envelope.method == "map_gc":
            return {"ok": True, "removed": self.map_watchers.gc()}
        if envelope.method == "register":
            client_id = str(envelope.payload.get("client_id") or "")
            if not client_id:
                raise HubProtocolError("client_id is required")
            self.clients.add(client_id)
            return {"ok": True, "client_id": client_id, "state": "registered"}
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
            try:
                result = run_with_fallback(spec)
            except (OSError, RuntimeError, asyncio.CancelledError) as exc:
                raise HubError(f"supervisor execution failed: {exc}") from exc
            return {"ok": True, "backend": backend_name(), "result": result.to_dict()}
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
            server.listen(8)
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
