"""Shared queue coordination with durable leases and fencing.

``SQLiteRemoteQueue`` is the development and single-host backend for the
``simplicio.queue/v1`` contract.  A SQLite file on a shared, transactional
volume can be used by multiple processes; deployments that need a network
service should implement :class:`RemoteQueue` with the same atomic methods.
The module intentionally has no fail-open path: an unavailable store raises
and callers must hand off rather than mutate a task.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import ipaddress
import ssl
import time
import urllib.error
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Mapping, Optional, Protocol, Sequence

from .agent_contract import validate_identity


SCHEMA = "simplicio.queue/v1"


class QueueConflict(RuntimeError):
    """The caller lost a lease or presented an old fencing token."""


class QueueUnavailable(RuntimeError):
    """The queue could not be reached; mutation must pause and hand off."""


@dataclass(frozen=True)
class Lease:
    task_id: str
    agent_id: str
    lease_id: str
    fencing_token: int
    expires_at: float
    idempotency_key: str
    identity: Optional[Dict[str, Any]] = None
    capabilities: tuple[str, ...] = ()


class RemoteQueue(Protocol):
    def claim(self, task_id: str, agent_id: str, *, idempotency_key: str,
              ttl: float = 60.0, identity: Optional[Mapping[str, Any]] = None,
              capabilities: Optional[Sequence[str]] = None) -> Lease: ...

    def heartbeat(self, lease: Lease, *, ttl: float = 60.0) -> Lease: ...

    def complete(self, lease: Lease, *, receipt_ref: str) -> Dict[str, Any]: ...

    def assert_active(self, lease: Lease) -> None: ...


def _lease_from_json(value: Mapping[str, Any]) -> Lease:
    """Decode the wire representation without trusting client-controlled fields."""
    return Lease(str(value["task_id"]), str(value["agent_id"]), str(value["lease_id"]),
                 int(value["fencing_token"]), float(value["expires_at"]),
                 str(value["idempotency_key"]), value.get("identity"),
                 tuple(value.get("capabilities") or ()))


def _lease_json(lease: Lease) -> Dict[str, Any]:
    return {"task_id": lease.task_id, "agent_id": lease.agent_id, "lease_id": lease.lease_id,
            "fencing_token": lease.fencing_token, "expires_at": lease.expires_at,
            "idempotency_key": lease.idempotency_key, "identity": lease.identity,
            "capabilities": list(lease.capabilities)}


class HTTPRemoteQueue:
    """Network client for ``simplicio.queue/v1``.

    The client has no local mutation fallback: DNS, timeout, non-JSON, and 5xx
    failures become :class:`QueueUnavailable`, so callers must pause and hand
    off rather than mutating a checkout while disconnected.
    """

    def __init__(self, base_url: str, *, token: Optional[str] = None, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._require_secure_transport()

    def _require_secure_transport(self) -> None:
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("queue URL must use http or https")
        if not parsed.hostname:
            raise ValueError("queue URL must include a host")
        if parsed.scheme != "https" and not _is_loopback_host(parsed.hostname):
            raise QueueUnavailable("TLS is required for non-loopback queue URLs")

    def _request(self, method: str, path: str, payload: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        self._require_secure_transport()
        body = json.dumps(payload or {}, sort_keys=True).encode("utf-8")
        req = urllib.request.Request(self.base_url + "/v1/queue" + path, data=body,
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": "Bearer " + self.token} if self.token else {})},
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:  # noqa: S310
                raw = response.read()
                result = json.loads(raw.decode("utf-8"))
                if not isinstance(result, dict):
                    raise ValueError("queue response must be an object")
                return result
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8"))
            except Exception:
                detail = {}
            message = str(detail.get("error") or exc.reason or "queue request failed")
            if exc.code == 409:
                raise QueueConflict(message) from exc
            if exc.code in (400, 401, 404):
                raise (KeyError(message) if exc.code == 404 else ValueError(message)) from exc
            raise QueueUnavailable(message) from exc
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise QueueUnavailable("queue unavailable: %s" % exc) from exc

    def enqueue(self, task_id: str, payload: Optional[Dict[str, Any]] = None) -> None:
        self._request("POST", "/enqueue", {"task_id": task_id, "payload": payload or {}})

    def claim(self, task_id: str, agent_id: str, *, idempotency_key: str,
              ttl: float = 60.0, identity: Optional[Mapping[str, Any]] = None,
              capabilities: Optional[Sequence[str]] = None) -> Lease:
        result = self._request("POST", "/claim", {"task_id": task_id, "agent_id": agent_id,
            "idempotency_key": idempotency_key, "ttl": ttl, "identity": identity,
            "capabilities": list(capabilities) if capabilities is not None else None})
        return _lease_from_json(result["lease"])

    def heartbeat(self, lease: Lease, *, ttl: float = 60.0) -> Lease:
        result = self._request("POST", "/heartbeat", {"lease": _lease_json(lease), "ttl": ttl})
        return _lease_from_json(result["lease"])

    def complete(self, lease: Lease, *, receipt_ref: str) -> Dict[str, Any]:
        return self._request("POST", "/complete", {"lease": _lease_json(lease), "receipt_ref": receipt_ref})

    def assert_active(self, lease: Lease) -> None:
        self._request("POST", "/assert-active", {"lease": _lease_json(lease)})

    def release(self, lease: Lease, *, reason: str = "handoff") -> Dict[str, Any]:
        return self._request("POST", "/release", {"lease": _lease_json(lease), "reason": reason})

    def events(self, *, after: int = 0, limit: int = 100) -> List[Dict[str, Any]]:
        return self._request("POST", "/events", {"after": after, "limit": limit})["events"]

    def task(self, task_id: str) -> Dict[str, Any]:
        return self._request("POST", "/task", {"task_id": task_id})


def _now() -> float:
    return time.time()


def _lease_id(task_id: str, agent_id: str, key: str) -> str:
    return hashlib.sha256((task_id + "\0" + agent_id + "\0" + key).encode()).hexdigest()[:32]


class SQLiteRemoteQueue:
    """Atomic queue backend suitable for local development and shared volumes.

    Every write is ``BEGIN IMMEDIATE`` and updates the monotonically increasing
    fencing token before returning a lease.  A stale worker cannot heartbeat,
    complete, or release after another worker has reclaimed the task.
    """

    def __init__(self, path: str, *, busy_timeout: float = 10.0) -> None:
        self.path = path
        self.busy_timeout = busy_timeout
        try:
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._init()
        except sqlite3.Error as exc:
            raise QueueUnavailable("queue unavailable: %s" % exc) from exc

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=self.busy_timeout, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self) -> None:
        with self._connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS queue_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'ready',
                payload TEXT NOT NULL DEFAULT '{}', updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS leases (
                task_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, lease_id TEXT NOT NULL,
                fencing_token INTEGER NOT NULL, idempotency_key TEXT NOT NULL,
                expires_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                receipt_ref TEXT, updated_at REAL NOT NULL,
                identity TEXT, capabilities TEXT,
                FOREIGN KEY(task_id) REFERENCES tasks(task_id)
            );
            CREATE TABLE IF NOT EXISTS idempotency (
                idempotency_key TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                lease_id TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                kind TEXT NOT NULL, agent_id TEXT NOT NULL, fencing_token INTEGER,
                payload TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_task_seq ON events(task_id, seq);
            """)
            columns = {row[1] for row in c.execute("PRAGMA table_info(leases)").fetchall()}
            if "identity" not in columns:
                c.execute("ALTER TABLE leases ADD COLUMN identity TEXT")
            if "capabilities" not in columns:
                c.execute("ALTER TABLE leases ADD COLUMN capabilities TEXT")
            c.execute("INSERT OR IGNORE INTO queue_meta(key, value) VALUES('schema', ?)", (SCHEMA,))

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            c = self._connect()
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.commit()
            except Exception:
                c.rollback()
                raise
            finally:
                c.close()
        except sqlite3.OperationalError as exc:
            raise QueueUnavailable("queue unavailable: %s" % exc) from exc

    @staticmethod
    def _event(c: sqlite3.Connection, task_id: str, kind: str, agent_id: str,
               token: Optional[int], payload: Dict[str, Any]) -> None:
        c.execute("INSERT INTO events(task_id,kind,agent_id,fencing_token,payload,created_at) VALUES(?,?,?,?,?,?)",
                  (task_id, kind, agent_id, token, json.dumps(payload, sort_keys=True), _now()))

    def enqueue(self, task_id: str, payload: Optional[Dict[str, Any]] = None) -> None:
        task_id = str(task_id).strip()
        if not task_id:
            raise ValueError("task_id is required")
        with self._tx() as c:
            c.execute("INSERT OR IGNORE INTO tasks(task_id,status,payload,updated_at) VALUES(?,?,?,?)",
                      (task_id, "ready", json.dumps(payload or {}, sort_keys=True), _now()))
            self._event(c, task_id, "enqueued", "system", None, payload or {})

    def claim(self, task_id: str, agent_id: str, *, idempotency_key: str,
              ttl: float = 60.0, identity: Optional[Mapping[str, Any]] = None,
              capabilities: Optional[Sequence[str]] = None) -> Lease:
        if ttl <= 0 or not agent_id or not idempotency_key:
            raise ValueError("agent_id, idempotency_key and positive ttl are required")
        normalized_identity = validate_identity(identity, capabilities=capabilities) if identity is not None else None
        if normalized_identity is not None and normalized_identity["agent_id"] != agent_id:
            raise QueueConflict("agent_id does not match distributed identity")
        normalized_caps = tuple(normalized_identity["capabilities"] if normalized_identity else ())
        now = _now()
        lid = _lease_id(task_id, agent_id, idempotency_key)
        try:
            with self._tx() as c:
                existing = c.execute("SELECT task_id,lease_id FROM idempotency WHERE idempotency_key=?",
                                     (idempotency_key,)).fetchone()
                if existing and existing["task_id"] != task_id:
                    raise QueueConflict("idempotency key already belongs to another task")
                if existing:
                    row = c.execute("SELECT * FROM leases WHERE task_id=? AND lease_id=?",
                                    (existing["task_id"], existing["lease_id"])).fetchone()
                    if row and row["status"] == "active" and row["expires_at"] > now:
                        stored_identity = json.loads(row["identity"]) if row["identity"] else None
                        if normalized_identity is not None and stored_identity != normalized_identity:
                            raise QueueConflict("idempotency key is bound to another agent identity")
                        return Lease(row["task_id"], row["agent_id"], row["lease_id"], row["fencing_token"],
                                     row["expires_at"], row["idempotency_key"], stored_identity,
                                     tuple(json.loads(row["capabilities"] or "[]")))
                task = c.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                if task is None:
                    raise KeyError("unknown task: %s" % task_id)
                if task["status"] == "completed":
                    raise QueueConflict("task already completed")
                current = c.execute("SELECT * FROM leases WHERE task_id=?", (task_id,)).fetchone()
                if current and current["status"] == "active" and current["expires_at"] > now:
                    raise QueueConflict("task already leased by %s" % current["agent_id"])
                token = int(current["fencing_token"]) + 1 if current else 1
                expires = now + ttl
                c.execute(
                    "INSERT OR REPLACE INTO leases(task_id,agent_id,lease_id,fencing_token,"
                    "idempotency_key,expires_at,status,receipt_ref,updated_at,identity,capabilities) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (task_id, agent_id, lid, token, idempotency_key, expires, "active", None, now,
                           json.dumps(normalized_identity, sort_keys=True) if normalized_identity else None,
                           json.dumps(list(normalized_caps))))
                c.execute(
                    "INSERT OR REPLACE INTO idempotency(idempotency_key,task_id,lease_id,created_at) "
                    "VALUES(?,?,?,?)",
                          (idempotency_key, task_id, lid, now))
                c.execute("UPDATE tasks SET status='claimed',updated_at=? WHERE task_id=?", (now, task_id))
                self._event(c, task_id, "claimed", agent_id, token, {"lease_id": lid, "expires_at": expires})
                return Lease(task_id, agent_id, lid, token, expires, idempotency_key,
                             normalized_identity, normalized_caps)
        except sqlite3.Error as exc:
            raise QueueUnavailable("queue unavailable: %s" % exc) from exc

    def _owned(self, c: sqlite3.Connection, lease: Lease) -> sqlite3.Row:
        row = c.execute("SELECT * FROM leases WHERE task_id=?", (lease.task_id,)).fetchone()
        stored_identity = json.loads(row["identity"]) if row is not None and row["identity"] else None
        if (row is None or row["lease_id"] != lease.lease_id or row["agent_id"] != lease.agent_id or
                int(row["fencing_token"]) != lease.fencing_token or row["status"] != "active" or
                row["expires_at"] <= _now() or
                (lease.identity is not None and stored_identity != lease.identity)):
            raise QueueConflict("stale or expired fencing token")
        return row

    def heartbeat(self, lease: Lease, *, ttl: float = 60.0) -> Lease:
        if ttl <= 0:
            raise ValueError("positive ttl is required")
        with self._tx() as c:
            self._owned(c, lease)
            expires = _now() + ttl
            c.execute("UPDATE leases SET expires_at=?,updated_at=? WHERE task_id=?", (expires, _now(), lease.task_id))
            self._event(c, lease.task_id, "heartbeat", lease.agent_id, lease.fencing_token, {"expires_at": expires})
            return Lease(lease.task_id, lease.agent_id, lease.lease_id, lease.fencing_token,
                         expires, lease.idempotency_key, lease.identity, lease.capabilities)

    def assert_active(self, lease: Lease) -> None:
        """Validate fencing without extending the lease or mutating queue state."""
        with self._tx() as c:
            self._owned(c, lease)

    def complete(self, lease: Lease, *, receipt_ref: str) -> Dict[str, Any]:
        if not receipt_ref:
            raise ValueError("receipt_ref is required")
        with self._tx() as c:
            self._owned(c, lease)
            now = _now()
            c.execute("UPDATE leases SET status='completed',receipt_ref=?,updated_at=? WHERE task_id=?",
                      (receipt_ref, now, lease.task_id))
            c.execute("UPDATE tasks SET status='completed',updated_at=? WHERE task_id=?", (now, lease.task_id))
            self._event(c, lease.task_id, "completed", lease.agent_id, lease.fencing_token,
                        {"receipt_ref": receipt_ref})
            return {"schema": SCHEMA, "task_id": lease.task_id, "status": "completed",
                    "fencing_token": lease.fencing_token, "receipt_ref": receipt_ref,
                    "agent": lease.identity or {"agent_id": lease.agent_id}}

    def release(self, lease: Lease, *, reason: str = "handoff") -> Dict[str, Any]:
        with self._tx() as c:
            self._owned(c, lease)
            now = _now()
            c.execute("UPDATE leases SET status='released',updated_at=? WHERE task_id=?", (now, lease.task_id))
            c.execute("UPDATE tasks SET status='ready',updated_at=? WHERE task_id=?", (now, lease.task_id))
            self._event(c, lease.task_id, "released", lease.agent_id, lease.fencing_token, {"reason": reason})
            return {"task_id": lease.task_id, "status": "ready", "handoff": True, "reason": reason}

    def events(self, *, after: int = 0, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as c:
            rows = c.execute("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (after, limit)).fetchall()
            return [{"seq": r["seq"], "task_id": r["task_id"], "kind": r["kind"],
                     "agent_id": r["agent_id"], "fencing_token": r["fencing_token"],
                     "payload": json.loads(r["payload"]), "created_at": r["created_at"]} for r in rows]

    def task(self, task_id: str) -> Dict[str, Any]:
        with self._connect() as c:
            row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            lease = c.execute("SELECT * FROM leases WHERE task_id=?", (task_id,)).fetchone()
            return {"task_id": task_id, "status": row["status"], "payload": json.loads(row["payload"]),
                    "lease": dict(lease) if lease else None}


def _is_loopback_host(host: str) -> bool:
    value = str(host or "").strip().lower().strip("[]")
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def tls_context_from_files(certfile: str, keyfile: str) -> ssl.SSLContext:
    if not certfile or not keyfile:
        raise ValueError("tls certfile and keyfile are both required")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    return context


def create_http_queue_server(queue: SQLiteRemoteQueue, host: str = "127.0.0.1", port: int = 0,
                             *, token: Optional[str] = None,
                             ssl_context: Optional[ssl.SSLContext] = None) -> ThreadingHTTPServer:
    """Create a small authenticated HTTP facade over a transactional queue.

    The returned server is not started, allowing tests and embedding runtimes to
    choose a thread, process, or service manager. Set ``token`` in any network
    deployment; a missing/incorrect bearer token is rejected before dispatch.
    Non-loopback binds require an explicit TLS context.
    """
    if not _is_loopback_host(host) and ssl_context is None:
        raise ValueError("TLS is required for non-loopback queue binds")
    class Handler(BaseHTTPRequestHandler):
        server_version = "simplicio-queue/1"

        def log_message(self, *_args: Any) -> None:
            return

        def _send(self, status: int, value: Mapping[str, Any]) -> None:
            raw = json.dumps(dict(value), sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            value = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if not isinstance(value, dict):
                raise ValueError("request body must be an object")
            return value

        def do_POST(self) -> None:  # noqa: N802
            if token is not None and self.headers.get("Authorization") != "Bearer " + token:
                self._send(401, {"error": "invalid queue token"})
                return
            if not self.path.startswith("/v1/queue/"):
                self._send(404, {"error": "unknown queue endpoint"})
                return
            try:
                body = self._body()
                op = self.path.rsplit("/", 1)[-1]
                if op == "enqueue":
                    queue.enqueue(body["task_id"], body.get("payload"))
                    result = {}
                elif op == "claim":
                    lease = queue.claim(body["task_id"], body["agent_id"], idempotency_key=body["idempotency_key"],
                                        ttl=float(body.get("ttl", 60.0)), identity=body.get("identity"),
                                        capabilities=body.get("capabilities"))
                    result = {"lease": _lease_json(lease)}
                elif op == "heartbeat":
                    lease = queue.heartbeat(_lease_from_json(body["lease"]), ttl=float(body.get("ttl", 60.0)))
                    result = {"lease": _lease_json(lease)}
                elif op == "complete":
                    result = queue.complete(_lease_from_json(body["lease"]), receipt_ref=body["receipt_ref"])
                elif op == "assert-active":
                    queue.assert_active(_lease_from_json(body["lease"]))
                    result = {"active": True}
                elif op == "release":
                    result = queue.release(_lease_from_json(body["lease"]), reason=body.get("reason", "handoff"))
                elif op == "events":
                    result = {"events": queue.events(after=int(body.get("after", 0)), limit=int(body.get("limit", 100)))}
                elif op == "task":
                    result = queue.task(body["task_id"])
                else:
                    self._send(404, {"error": "unknown queue operation"})
                    return
                self._send(200, result)
            except QueueConflict as exc:
                self._send(409, {"error": str(exc)})
            except QueueUnavailable as exc:
                self._send(503, {"error": str(exc)})
            except KeyError as exc:
                self._send(404, {"error": str(exc)})
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                self._send(400, {"error": str(exc)})

    server = ThreadingHTTPServer((host, port), Handler)
    if ssl_context is not None:
        server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
    return server
