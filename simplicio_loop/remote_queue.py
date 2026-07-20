"""Shared queue coordination with durable leases and fencing.

``SQLiteRemoteQueue`` is the development and single-host backend for the
``simplicio.queue/v1`` contract.  A SQLite file on a shared, transactional
volume can be used by multiple processes; deployments that need a network
service should implement :class:`RemoteQueue` with the same atomic methods.
The module intentionally has no fail-open path: an unavailable store raises
and callers must hand off rather than mutate a task.
"""
from __future__ import annotations

import asyncio
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
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Protocol, Sequence

from .agent_contract import validate_identity
from .receipt_verifier import QUEUE_RECEIPT_SCHEMA, canonical_content_hash, verify_receipt
from .secure_transport import SecureTransportError, TrustedEndpoint
from .secure_transport import request_json as _secure_request_json

try:  # pragma: no cover - installed package without scripts namespace
    from scripts.distributed_trust_policy import check_endpoint as _check_endpoint
except ImportError:  # pragma: no cover
    _check_endpoint = None

try:  # pragma: no cover - installed package without scripts namespace
    from scripts.security_audit_log import append_event as _audit_append
except ImportError:  # pragma: no cover
    _audit_append = None


SCHEMA = "simplicio.queue/v1"


class _QueueHTTPServer(ThreadingHTTPServer):
    """Loopback queue server sized for the documented concurrent claim lane."""

    daemon_threads = True
    request_queue_size = 128


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
    cancelled: bool = False


class RemoteQueue(Protocol):
    def pull(self, agent_id: str, *, capabilities: Optional[Sequence[str]] = None,
             limit: int = 20) -> List[Dict[str, Any]]: ...

    def claim(self, task_id: str, agent_id: str, *, idempotency_key: str,
              ttl: float = 60.0, identity: Optional[Mapping[str, Any]] = None,
              capabilities: Optional[Sequence[str]] = None) -> Lease: ...

    def heartbeat(self, lease: Lease, *, ttl: float = 60.0) -> Lease: ...

    def complete(self, lease: Lease, *, receipt_ref: str,
                 receipt: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]: ...

    def assert_active(self, lease: Lease) -> None: ...

    def request_cancel(self, task_id: str, *, reason: str = "cancelled") -> Dict[str, Any]: ...


def _lease_from_json(value: Mapping[str, Any]) -> Lease:
    """Decode the wire representation without trusting client-controlled fields."""
    return Lease(str(value["task_id"]), str(value["agent_id"]), str(value["lease_id"]),
                 int(value["fencing_token"]), float(value["expires_at"]),
                 str(value["idempotency_key"]), value.get("identity"),
                 tuple(value.get("capabilities") or ()), bool(value.get("cancelled", False)))


def _lease_json(lease: Lease) -> Dict[str, Any]:
    return {"task_id": lease.task_id, "agent_id": lease.agent_id, "lease_id": lease.lease_id,
            "fencing_token": lease.fencing_token, "expires_at": lease.expires_at,
            "idempotency_key": lease.idempotency_key, "identity": lease.identity,
            "capabilities": list(lease.capabilities), "cancelled": lease.cancelled}


def build_completion_receipt(*, task_id: str, agent_id: str, fencing_token: int, receipt_ref: str,
                             extra: Optional[Mapping[str, Any]] = None,
                             now: Optional[float] = None) -> Dict[str, Any]:
    """Build the wire receipt a caller passes as ``RemoteQueue.complete(..., receipt=...)``.

    This is what makes server-side verification of issue #286 step 9 real rather than
    aspirational: the queue independently recomputes ``receipt_sha`` over this exact payload
    (:data:`simplicio_loop.receipt_verifier.QUEUE_RECEIPT_SCHEMA`) and cross-checks
    ``task_id``/``agent_id``/``fencing_token`` against the *active* lease before ever marking a
    task ``completed`` -- a forged or stale receipt for the wrong task/attempt/fence is rejected
    even if the presenting client insists it is legitimate.
    """
    body: Dict[str, Any] = {
        "schema": "simplicio.queue-receipt/v1",
        "task_id": str(task_id),
        "agent_id": str(agent_id),
        "fencing_token": int(fencing_token),
        "receipt_ref": str(receipt_ref),
        "measured_at": _now() if now is None else float(now),
    }
    if extra:
        body["detail"] = json.loads(json.dumps(dict(extra), default=str))
    body["receipt_sha"] = canonical_content_hash(body)
    return body


class HTTPRemoteQueue:
    """Network client for ``simplicio.queue/v1``.

    The client has no local mutation fallback: DNS, timeout, non-JSON, and 5xx
    failures become :class:`QueueUnavailable`, so callers must pause and hand
    off rather than mutating a checkout while disconnected.
    """

    def __init__(self, base_url: str, *, token: Optional[str] = None, timeout: float = 5.0,
                 environment_id: Optional[str] = None, policy: Optional[Mapping[str, Any]] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._require_secure_transport()
        # #289: when the caller resolved a trust-policy environment for this queue
        # (see `runner._resolve_trusted_queue_url`), every request below is forced
        # through `secure_transport.request_json`, which performs its own DNS
        # resolution/TLS handshake and calls `check_endpoint()` with the
        # *measured* certificate fingerprint before sending anything -- the
        # connect-time enforcement `check_endpoint()` previously lacked.
        self._trusted_endpoint: Optional[TrustedEndpoint] = None
        if environment_id and policy is not None:
            if _check_endpoint is None:
                raise QueueUnavailable(
                    "distributed trust policy module unavailable; cannot enforce connect-time checks"
                )
            self._trusted_endpoint = TrustedEndpoint(
                environment_id=environment_id, policy=policy, check_endpoint=_check_endpoint,
            )

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
        headers = {"Content-Type": "application/json",
                   **({"Authorization": "Bearer " + self.token} if self.token else {})}
        url = self.base_url + "/v1/queue" + path

        if self._trusted_endpoint is not None:
            try:
                result = _secure_request_json(
                    method, url, body=body, headers=headers, timeout=self.timeout,
                    endpoint=self._trusted_endpoint,
                )
            except SecureTransportError as exc:
                raise QueueUnavailable("connect-time trust check failed: %s" % exc) from exc
            status = result.pop("_status", 200)
            if status == 200:
                return result
            message = str(result.get("error") or "queue request failed")
            if status == 409:
                raise QueueConflict(message)
            if status == 404:
                raise KeyError(message)
            if status in (400, 401):
                raise ValueError(message)
            raise QueueUnavailable(message)

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
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

    async def _request_async(self, method: str, path: str, payload: Optional[Mapping[str, Any]] = None,
                             *, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Run the blocking ``urlopen``/secure-transport call in a worker thread.

        This is the async escape hatch for the hot polling loop (``pull``/``claim``/
        ``heartbeat``/``complete``) so a remote worker's asyncio event loop is never
        stalled for the duration of a real network round trip. ``timeout`` bounds the
        *async* wait independently of ``self.timeout`` (the socket-level timeout inside
        ``_request``): if the deadline elapses first, the awaiting caller gets control
        back immediately with :class:`QueueUnavailable` -- the underlying OS thread is
        asked to cancel and, since ``urlopen`` does not cooperatively check for
        cancellation, keeps running until its own socket timeout independently, exactly
        like a detached child process a caller stopped waiting on.
        """
        loop = asyncio.get_running_loop()
        deadline = self.timeout if timeout is None else timeout
        future = loop.run_in_executor(None, self._request, method, path, payload)
        try:
            return await asyncio.wait_for(future, timeout=deadline)
        except asyncio.TimeoutError as exc:
            future.cancel()
            raise QueueUnavailable(
                "queue request exceeded async deadline of %ss" % deadline
            ) from exc
        except asyncio.CancelledError:
            future.cancel()
            raise

    def enqueue(self, task_id: str, payload: Optional[Dict[str, Any]] = None) -> None:
        self._request("POST", "/enqueue", {"task_id": task_id, "payload": payload or {}})

    async def enqueue_async(self, task_id: str, payload: Optional[Dict[str, Any]] = None,
                            *, timeout: Optional[float] = None) -> None:
        await self._request_async("POST", "/enqueue", {"task_id": task_id, "payload": payload or {}},
                                  timeout=timeout)

    def claim(self, task_id: str, agent_id: str, *, idempotency_key: str,
              ttl: float = 60.0, identity: Optional[Mapping[str, Any]] = None,
              capabilities: Optional[Sequence[str]] = None) -> Lease:
        result = self._request("POST", "/claim", {"task_id": task_id, "agent_id": agent_id,
            "idempotency_key": idempotency_key, "ttl": ttl, "identity": identity,
            "capabilities": list(capabilities) if capabilities is not None else None})
        return _lease_from_json(result["lease"])

    async def claim_async(self, task_id: str, agent_id: str, *, idempotency_key: str,
                          ttl: float = 60.0, identity: Optional[Mapping[str, Any]] = None,
                          capabilities: Optional[Sequence[str]] = None,
                          timeout: Optional[float] = None) -> Lease:
        result = await self._request_async("POST", "/claim", {"task_id": task_id, "agent_id": agent_id,
            "idempotency_key": idempotency_key, "ttl": ttl, "identity": identity,
            "capabilities": list(capabilities) if capabilities is not None else None}, timeout=timeout)
        return _lease_from_json(result["lease"])

    def heartbeat(self, lease: Lease, *, ttl: float = 60.0) -> Lease:
        result = self._request("POST", "/heartbeat", {"lease": _lease_json(lease), "ttl": ttl})
        return _lease_from_json(result["lease"])

    async def heartbeat_async(self, lease: Lease, *, ttl: float = 60.0,
                              timeout: Optional[float] = None) -> Lease:
        result = await self._request_async("POST", "/heartbeat", {"lease": _lease_json(lease), "ttl": ttl},
                                           timeout=timeout)
        return _lease_from_json(result["lease"])

    def complete(self, lease: Lease, *, receipt_ref: str,
                receipt: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"lease": _lease_json(lease), "receipt_ref": receipt_ref}
        if receipt is not None:
            payload["receipt"] = dict(receipt)
        return self._request("POST", "/complete", payload)

    async def complete_async(self, lease: Lease, *, receipt_ref: str,
                             receipt: Optional[Mapping[str, Any]] = None,
                             timeout: Optional[float] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"lease": _lease_json(lease), "receipt_ref": receipt_ref}
        if receipt is not None:
            payload["receipt"] = dict(receipt)
        return await self._request_async("POST", "/complete", payload, timeout=timeout)

    def pull(self, agent_id: str, *, capabilities: Optional[Sequence[str]] = None,
             limit: int = 20) -> List[Dict[str, Any]]:
        """Discover ready, capability-matching work without claiming it.

        Only summaries of tasks this worker is eligible for are returned; the
        server never serializes the full payload/context of a task this
        worker cannot or should not see.
        """
        result = self._request("POST", "/pull", {"agent_id": agent_id,
            "capabilities": list(capabilities or ()), "limit": int(limit)})
        return list(result["tasks"])

    async def pull_async(self, agent_id: str, *, capabilities: Optional[Sequence[str]] = None,
                         limit: int = 20, timeout: Optional[float] = None) -> List[Dict[str, Any]]:
        """Async counterpart of :meth:`pull` -- see :meth:`_request_async`."""
        result = await self._request_async("POST", "/pull", {"agent_id": agent_id,
            "capabilities": list(capabilities or ()), "limit": int(limit)}, timeout=timeout)
        return list(result["tasks"])

    def assert_active(self, lease: Lease) -> None:
        self._request("POST", "/assert-active", {"lease": _lease_json(lease)})

    def request_cancel(self, task_id: str, *, reason: str = "cancelled") -> Dict[str, Any]:
        """Ask the current claimant to stop cooperatively (checked on its next heartbeat)."""
        return self._request("POST", "/cancel", {"task_id": task_id, "reason": reason})

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

    def __init__(self, path: str, *, busy_timeout: float = 10.0,
                receipt_max_age_seconds: Optional[float] = None) -> None:
        self.path = path
        self.busy_timeout = busy_timeout
        # issue #286 step 9: how old a *verified* completion receipt's own ``measured_at``
        # may be before the server itself rejects it as stale. ``None`` (the default) skips
        # the freshness check but the schema/hash/task-agent-fence binding checks below
        # still run whenever a caller supplies ``receipt=`` to ``complete()``.
        self.receipt_max_age_seconds = receipt_max_age_seconds
        try:
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._init()
        except sqlite3.Error as exc:
            raise QueueUnavailable("queue unavailable: %s" % exc) from exc

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=self.busy_timeout, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            # Setting journal_mode is a write to the database header.  Two fresh
            # workers may both reach this point before either has created the
            # queue, and SQLite does not consistently apply the connection's busy
            # handler to that PRAGMA.  Avoid the write once WAL is active and bound
            # retries for the first-start race by the public busy_timeout.
            deadline = time.monotonic() + max(0.0, self.busy_timeout)
            while True:
                try:
                    row = conn.execute("PRAGMA journal_mode").fetchone()
                    if row is None or str(row[0]).lower() != "wal":
                        conn.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    detail = str(exc).lower()
                    remaining = deadline - time.monotonic()
                    if ("locked" not in detail and "busy" not in detail) or remaining <= 0:
                        raise
                    time.sleep(min(0.01, remaining))
            conn.execute("PRAGMA foreign_keys=ON")
            return conn
        except BaseException:
            conn.close()
            raise

    def _init(self) -> None:
        with self._connect() as c:
            # Schema discovery plus ALTER must be one serialized transaction.  If
            # two first-start workers inspect table_info concurrently, both can
            # otherwise decide that the same column is missing and one loses with
            # ``duplicate column name``.  BEGIN IMMEDIATE waits no longer than the
            # configured SQLite busy timeout and makes that check-and-migrate step
            # atomic across processes.
            c.execute("BEGIN IMMEDIATE")
            try:
                c.execute(
                    "CREATE TABLE IF NOT EXISTS queue_meta "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                c.execute(
                    "CREATE TABLE IF NOT EXISTS tasks ("
                    "task_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'ready', "
                    "payload TEXT NOT NULL DEFAULT '{}', updated_at REAL NOT NULL)"
                )
                c.execute(
                    "CREATE TABLE IF NOT EXISTS leases ("
                    "task_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, lease_id TEXT NOT NULL, "
                    "fencing_token INTEGER NOT NULL, idempotency_key TEXT NOT NULL, "
                    "expires_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'active', "
                    "receipt_ref TEXT, updated_at REAL NOT NULL, identity TEXT, capabilities TEXT, "
                    "FOREIGN KEY(task_id) REFERENCES tasks(task_id))"
                )
                c.execute(
                    "CREATE TABLE IF NOT EXISTS idempotency ("
                    "idempotency_key TEXT PRIMARY KEY, task_id TEXT NOT NULL, "
                    "lease_id TEXT NOT NULL, created_at REAL NOT NULL)"
                )
                c.execute(
                    "CREATE TABLE IF NOT EXISTS events ("
                    "seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, "
                    "kind TEXT NOT NULL, agent_id TEXT NOT NULL, fencing_token INTEGER, "
                    "payload TEXT NOT NULL, created_at REAL NOT NULL)"
                )
                c.execute("CREATE INDEX IF NOT EXISTS events_task_seq ON events(task_id, seq)")
                columns = {row[1] for row in c.execute("PRAGMA table_info(leases)").fetchall()}
                if "identity" not in columns:
                    c.execute("ALTER TABLE leases ADD COLUMN identity TEXT")
                if "capabilities" not in columns:
                    c.execute("ALTER TABLE leases ADD COLUMN capabilities TEXT")
                if "cancel_requested" not in columns:
                    c.execute("ALTER TABLE leases ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0")
                if "receipt_sha" not in columns:
                    c.execute("ALTER TABLE leases ADD COLUMN receipt_sha TEXT")
                if "receipt_verdict" not in columns:
                    c.execute("ALTER TABLE leases ADD COLUMN receipt_verdict TEXT")
                c.execute("INSERT OR IGNORE INTO queue_meta(key, value) VALUES('schema', ?)", (SCHEMA,))
                c.commit()
            except Exception:
                c.rollback()
                raise

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

    def pull(self, agent_id: str, *, capabilities: Optional[Sequence[str]] = None,
             limit: int = 20) -> List[Dict[str, Any]]:
        """Return ready-to-claim task summaries eligible for ``agent_id``.

        A task is eligible when it is still ``ready`` (unclaimed), every
        ``depends_on`` entry in its payload is ``completed``, and its declared
        ``required_capabilities`` (if any) are a subset of the caller's
        ``capabilities``. Only a summary (task_id, required_capabilities,
        depends_on) is returned for eligible tasks -- the full payload/context
        of any task, including ineligible ones, is never serialized here.
        Independent workers use this instead of ``task()``/``events()`` (which
        would otherwise be the only way to discover work) to avoid a
        "list everything" call that leaks unrelated task context.
        """
        if not str(agent_id or "").strip():
            raise ValueError("agent_id is required")
        limit = max(1, int(limit))
        caps = {str(cap).strip() for cap in (capabilities or ()) if str(cap).strip()}
        try:
            with self._connect() as c:
                statuses = {row["task_id"]: row["status"]
                           for row in c.execute("SELECT task_id, status FROM tasks").fetchall()}
                rows = c.execute(
                    "SELECT task_id, payload, updated_at FROM tasks WHERE status='ready' "
                    "ORDER BY updated_at, task_id"
                ).fetchall()
                eligible: List[Dict[str, Any]] = []
                for row in rows:
                    payload = json.loads(row["payload"] or "{}")
                    required = sorted({str(cap).strip() for cap in payload.get("required_capabilities", ())
                                       if str(cap).strip()})
                    depends_on = sorted({str(dep).strip() for dep in payload.get("depends_on", ())
                                        if str(dep).strip()})
                    if required and not set(required).issubset(caps):
                        continue
                    unmet = [dep for dep in depends_on if statuses.get(dep) != "completed"]
                    if unmet:
                        continue
                    eligible.append({"task_id": row["task_id"], "status": "ready",
                                     "required_capabilities": required, "depends_on": depends_on,
                                     "updated_at": row["updated_at"]})
                    if len(eligible) >= limit:
                        break
                return eligible
        except sqlite3.Error as exc:
            raise QueueUnavailable("queue unavailable: %s" % exc) from exc

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
                                     tuple(json.loads(row["capabilities"] or "[]")),
                                     bool(row["cancel_requested"]))
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
                    "idempotency_key,expires_at,status,receipt_ref,updated_at,identity,capabilities,"
                    "cancel_requested) VALUES(?,?,?,?,?,?,?,?,?,?,?,0)",
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
                             normalized_identity, normalized_caps, False)
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
            row = self._owned(c, lease)
            expires = _now() + ttl
            c.execute("UPDATE leases SET expires_at=?,updated_at=? WHERE task_id=?", (expires, _now(), lease.task_id))
            self._event(c, lease.task_id, "heartbeat", lease.agent_id, lease.fencing_token, {"expires_at": expires})
            return Lease(lease.task_id, lease.agent_id, lease.lease_id, lease.fencing_token,
                         expires, lease.idempotency_key, lease.identity, lease.capabilities,
                         bool(row["cancel_requested"]))

    def assert_active(self, lease: Lease) -> None:
        """Validate fencing without extending the lease or mutating queue state."""
        with self._tx() as c:
            self._owned(c, lease)

    def request_cancel(self, task_id: str, *, reason: str = "cancelled") -> Dict[str, Any]:
        """Ask the current claimant to stop cooperatively at its next heartbeat/assert.

        This never kills the claimant's process directly -- cancellation here is
        cooperative: the flag is durably recorded against the *current* fencing
        token, and the claimant discovers it (and must itself release/abort) the
        next time it heartbeats or checks ``assert_active``. A claimant that never
        calls back in still loses the task the ordinary way, once its lease TTL
        expires.
        """
        task_id = str(task_id).strip()
        if not task_id:
            raise ValueError("task_id is required")
        with self._tx() as c:
            row = c.execute("SELECT * FROM leases WHERE task_id=?", (task_id,)).fetchone()
            if row is None or row["status"] != "active" or row["expires_at"] <= _now():
                raise QueueConflict("no active lease to cancel for task %s" % task_id)
            c.execute("UPDATE leases SET cancel_requested=1,updated_at=? WHERE task_id=?", (_now(), task_id))
            self._event(c, task_id, "cancel_requested", row["agent_id"], row["fencing_token"], {"reason": reason})
            return {"task_id": task_id, "cancel_requested": True, "fencing_token": row["fencing_token"],
                    "reason": reason}

    def complete(self, lease: Lease, *, receipt_ref: str,
                receipt: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """Transition a task to ``completed`` -- the queue is the *server-side* authority.

        When ``receipt`` is supplied (issue #286 step 9), it is independently verified here,
        never merely trusted because a client asserts it:

        1. schema/hash/provenance/freshness via ``receipt_verifier.verify_receipt`` against
           :data:`~simplicio_loop.receipt_verifier.QUEUE_RECEIPT_SCHEMA` -- a mismatched
           ``receipt_sha`` (tampering) or missing field is rejected before any state changes;
        2. the receipt's declared ``task_id``/``agent_id``/``fencing_token`` must match the
           *active* lease presenting it -- a genuinely-signed receipt for a different task,
           agent, or a superseded (stale) fence is rejected exactly like a forged one.

        A rejected receipt raises :class:`QueueConflict` and leaves the lease/task untouched
        (fail closed) so a corrected receipt or a fresh claim can still follow. ``receipt=None``
        preserves the legacy existence-only contract for callers that have not adopted the
        wire receipt yet (e.g. tests exercising only the lease/fencing mechanics).
        """
        if not receipt_ref:
            raise ValueError("receipt_ref is required")
        verdict = None
        if receipt is not None:
            verdict = verify_receipt(receipt, schema=QUEUE_RECEIPT_SCHEMA,
                                     max_age_seconds=self.receipt_max_age_seconds)
            if not verdict.verified:
                raise QueueConflict("receipt rejected: %s - %s" % (verdict.status, verdict.reason))
            if str(receipt.get("task_id") or "") != lease.task_id:
                raise QueueConflict("receipt task_id does not match the active lease")
            if str(receipt.get("agent_id") or "") != lease.agent_id:
                raise QueueConflict("receipt agent_id does not match the active lease")
            try:
                receipt_fence = int(receipt.get("fencing_token"))
            except (TypeError, ValueError):
                raise QueueConflict("receipt fencing_token is missing or not an integer")
            if receipt_fence != lease.fencing_token:
                raise QueueConflict("receipt fencing_token does not match the active lease (stale receipt)")
        with self._tx() as c:
            self._owned(c, lease)
            now = _now()
            receipt_sha = str(receipt.get("receipt_sha") or "") if receipt is not None else None
            c.execute("UPDATE leases SET status='completed',receipt_ref=?,receipt_sha=?,"
                      "receipt_verdict=?,updated_at=? WHERE task_id=?",
                      (receipt_ref, receipt_sha, verdict.status if verdict is not None else None,
                       now, lease.task_id))
            c.execute("UPDATE tasks SET status='completed',updated_at=? WHERE task_id=?", (now, lease.task_id))
            self._event(c, lease.task_id, "completed", lease.agent_id, lease.fencing_token,
                        {"receipt_ref": receipt_ref, "receipt_verified": verdict.verified if verdict else False})
            return {"schema": SCHEMA, "task_id": lease.task_id, "status": "completed",
                    "fencing_token": lease.fencing_token, "receipt_ref": receipt_ref,
                    "receipt_verified": verdict.verified if verdict is not None else False,
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
                             token_secret: Optional[str] = None,
                             token_scope: Optional[str] = None,
                             revocation_store: Optional[Path] = None,
                             ssl_context: Optional[ssl.SSLContext] = None,
                             audit_log_path: Optional[Path] = None) -> ThreadingHTTPServer:
    """Create a small authenticated HTTP facade over a transactional queue.

    The returned server is not started, allowing tests and embedding runtimes to
    choose a thread, process, or service manager. Non-loopback binds require an
    explicit TLS context.

    Two mutually exclusive auth modes (#289):

    * ``token`` -- legacy static bearer secret, compared verbatim. Never
      expires and cannot be individually revoked; kept only for local/dev use
      and backward compatibility.
    * ``token_secret`` (+ optional ``token_scope``/``revocation_store``) --
      short-lived credential mode (:mod:`scripts.short_lived_credentials`).
      Every request must present a token signed with ``token_secret`` that has
      not expired, is not before its ``nbf``, matches ``token_scope`` if given,
      and whose ``jti`` is not present in the revocation store. This is what
      closes "credential exchange is a bare static secret" without needing an
      OIDC broker.

      Operation-level scoping (#289): if the presented token carries an
      ``ops`` claim (see :func:`scripts.short_lived_credentials.issue_token`),
      it is checked against the specific queue operation in the request path
      (``pull``, ``claim``, ``complete``, ...) -- a token minted with
      ``operations=["pull"]`` is rejected on ``/claim`` or ``/complete`` even
      though its coarser ``scope`` claim matches. Tokens without an ``ops``
      claim are unaffected (legacy/unrestricted shape).

    A missing/invalid/expired/revoked bearer token is rejected (401) before
    any queue operation runs. Every accept/reject is appended to the #289
    audit log (:mod:`scripts.security_audit_log`) with the operation and
    auth mode, never the token itself.
    """
    if not _is_loopback_host(host) and ssl_context is None:
        raise ValueError("TLS is required for non-loopback queue binds")
    if token and token_secret:
        raise ValueError("token and token_secret are mutually exclusive auth modes")
    _verify_short_lived = None
    if token_secret:
        try:
            from scripts.short_lived_credentials import CredentialError, verify_token
        except ImportError as exc:  # pragma: no cover - installed package without scripts namespace
            raise RuntimeError("short-lived credential module unavailable") from exc

        def _verify_short_lived(presented: str, operation: str) -> bool:
            try:
                verify_token(token_secret, presented, expected_scope=token_scope,
                            expected_operation=operation,
                            revocation_store=revocation_store, audit_log_path=audit_log_path)
                return True
            except CredentialError:
                return False

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
            operation = self.path.rsplit("/", 1)[-1] if "/" in self.path else self.path
            auth_header = self.headers.get("Authorization", "")
            presented = auth_header[len("Bearer "):] if auth_header.startswith("Bearer ") else ""
            if token is not None and auth_header != "Bearer " + token:
                if _audit_append is not None:
                    _audit_append(audit_log_path, event="remote_queue.auth", decision="reject",
                                  operation=operation, reason="invalid static queue token")
                self._send(401, {"error": "invalid queue token"})
                return
            if _verify_short_lived is not None and not _verify_short_lived(presented, operation):
                # verify_token() already appended the detailed accept/reject
                # line (subject, jti, scope, operation, reason); nothing
                # further to log here since we only have a boolean.
                self._send(401, {"error": "invalid, expired, or revoked queue credential"})
                return
            if token is not None and _audit_append is not None:
                _audit_append(audit_log_path, event="remote_queue.auth", decision="accept",
                              operation=operation, reason="static queue token matched")
            if not self.path.startswith("/v1/queue/"):
                self._send(404, {"error": "unknown queue endpoint"})
                return
            try:
                body = self._body()
                op = self.path.rsplit("/", 1)[-1]
                if op == "enqueue":
                    queue.enqueue(body["task_id"], body.get("payload"))
                    result = {}
                elif op == "pull":
                    tasks = queue.pull(body["agent_id"], capabilities=body.get("capabilities"),
                                      limit=int(body.get("limit", 20)))
                    result = {"tasks": tasks}
                elif op == "claim":
                    lease = queue.claim(body["task_id"], body["agent_id"], idempotency_key=body["idempotency_key"],
                                        ttl=float(body.get("ttl", 60.0)), identity=body.get("identity"),
                                        capabilities=body.get("capabilities"))
                    result = {"lease": _lease_json(lease)}
                elif op == "heartbeat":
                    lease = queue.heartbeat(_lease_from_json(body["lease"]), ttl=float(body.get("ttl", 60.0)))
                    result = {"lease": _lease_json(lease)}
                elif op == "complete":
                    result = queue.complete(_lease_from_json(body["lease"]), receipt_ref=body["receipt_ref"],
                                            receipt=body.get("receipt"))
                elif op == "assert-active":
                    queue.assert_active(_lease_from_json(body["lease"]))
                    result = {"active": True}
                elif op == "cancel":
                    result = queue.request_cancel(body["task_id"], reason=body.get("reason", "cancelled"))
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

    server = _QueueHTTPServer((host, port), Handler)
    if ssl_context is not None:
        server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
    return server
