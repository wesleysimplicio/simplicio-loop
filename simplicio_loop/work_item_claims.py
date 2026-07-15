"""Bounded WorkItem attempts backed by the shared queue lease contract.

The runner owns planning and the queue owns exclusivity.  This small bridge keeps those
boundaries explicit: a worker receives one scoped context pack, every accepted receipt is
fenced by the current lease, and a retry gets a new attempt/idempotency key.  It is deliberately
transport agnostic (SQLite and HTTP queues expose the same ``assert_active`` operation).
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .agent_contract import IDENTITY_FIELDS, bind_receipt, build_context_pack, validate_identity
from .receipt_verifier import ReceiptSchema, ReceiptVerdict, verify_receipt
from .remote_queue import Lease, RemoteQueue

SCHEMA = "simplicio.work-item-attempt/v1"
EVENT_SCHEMA = "simplicio.work-item-attempt-event/v1"


class ReceiptVerificationFailed(RuntimeError):
    """A receipt offered for completion failed schema/hash/freshness/provenance checks.

    Closes the #286 gap where the remote-queue completion path (``AttemptCoordinator``)
    accepted any receipt shape without running it through ``receipt_verifier.verify_receipt``
    (issue #288) -- unlike ``runner.py``, which already gates on it. Only a
    :class:`~simplicio_loop.receipt_verifier.ReceiptStatus.VERIFIED` verdict may complete a
    lease through :meth:`AttemptCoordinator.verify_and_complete`; every other verdict raises
    here instead of silently completing the task.
    """

    def __init__(self, verdict: ReceiptVerdict) -> None:
        super().__init__("receipt verification failed: %s (%s)" % (verdict.status, verdict.reason))
        self.verdict = verdict


class LeaseLostDuringExecution(RuntimeError):
    """A heartbeat during a mutating subprocess found the lease no longer active.

    The guarded subprocess is killed immediately when this is detected so a stale
    worker never keeps mutating a checkout after losing its fence (#183 DoD gap:
    "não há heartbeat/assert-active durante a mutação").
    """

    def __init__(self, work_item_id: str, attempt_id: str, cause: BaseException) -> None:
        super().__init__(
            "lease lost mid-execution for work_item_id=%r attempt_id=%r: %s"
            % (work_item_id, attempt_id, cause)
        )
        self.work_item_id = work_item_id
        self.attempt_id = attempt_id
        self.cause = cause


@dataclass(frozen=True)
class WorkItemAttempt:
    run_id: str
    work_item_id: str
    attempt_id: str
    lease: Lease
    context: Dict[str, Any]
    events: tuple[Dict[str, Any], ...] = field(default_factory=tuple)


class AttemptCoordinator:
    """Claim one WorkItem and reject writes after lease loss or reassignment."""

    def __init__(self, queue: RemoteQueue, *, run_id: str, receipt_dir: str | Path | None = None) -> None:
        if not str(run_id).strip():
            raise ValueError("run_id is required")
        self.queue = queue
        self.run_id = str(run_id).strip()
        self.receipt_dir = Path(receipt_dir) if receipt_dir else None

    def claim(self, *, work_item_id: str, identity: Mapping[str, Any], goal: str,
              acs: Sequence[str] = (), depends_on: Sequence[str] = (),
              source_refs: Sequence[str] = (), allowed_paths: Sequence[str] = (),
              issue_ref: str = "", issue_url: str = "",
              ttl: float = 60.0) -> WorkItemAttempt:
        normalized = validate_identity(identity)
        item = str(work_item_id).strip()
        if not item:
            raise ValueError("work_item_id is required")
        # The queue is the source of truth for exclusivity.  Enqueue is idempotent for the
        # supplied backends, allowing a runner to resume without a preflight mutation.
        enqueue = getattr(self.queue, "enqueue", None)
        if enqueue is not None:
            enqueue(item, {"run_id": self.run_id, "goal": str(goal).strip(), "acs": list(acs)})
        key = "%s:%s:%s" % (self.run_id, item, normalized["session_id"])
        lease = self.queue.claim(item, normalized["agent_id"], idempotency_key=key, ttl=ttl,
                                 identity=normalized, capabilities=normalized["capabilities"])
        context = build_context_pack(task_id=item, goal=goal, identity=normalized, acs=acs,
                                     source_refs=source_refs, depends_on=depends_on,
                                     allowed_paths=allowed_paths, issue_ref=issue_ref,
                                     issue_url=issue_url)
        attempt_id = "%s-%d" % (item, lease.fencing_token)
        attempt = WorkItemAttempt(self.run_id, item, attempt_id, lease, context)
        self._append(attempt, "claimed", {"fencing_token": lease.fencing_token})
        return attempt

    def assert_active(self, attempt: WorkItemAttempt) -> None:
        checker = getattr(self.queue, "assert_active", None)
        if checker is not None:
            checker(attempt.lease)
            return
        # Compatibility for old transports: heartbeat performs the same fencing check.
        self.queue.heartbeat(attempt.lease, ttl=max(1.0, attempt.lease.expires_at - time.time()))

    def record_event(self, attempt: WorkItemAttempt, kind: str, payload: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        self.assert_active(attempt)
        if not str(kind).strip():
            raise ValueError("event kind is required")
        identity = {field: str((attempt.lease.identity or {}).get(field) or "") for field in IDENTITY_FIELDS}
        if not all(identity.values()):
            identity = {field: attempt.context["assigned_to"][field] for field in IDENTITY_FIELDS}
        event = {
            "schema": EVENT_SCHEMA, "run_id": self.run_id, "work_item_id": attempt.work_item_id,
            "attempt_id": attempt.attempt_id, "agent_id": attempt.lease.agent_id, "agent": identity,
            "fencing_token": attempt.lease.fencing_token, "kind": str(kind).strip(),
            "payload": dict(payload or {}), "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if attempt.context.get("issue_ref"):
            event["issue_ref"] = attempt.context["issue_ref"]
            event["issue_url"] = attempt.context["issue_url"]
        self._append_json(attempt, event)
        return event

    def accept_receipt(self, attempt: WorkItemAttempt, receipt: Mapping[str, Any], *,
                        schema: Optional[ReceiptSchema] = None,
                        max_age_seconds: Optional[float] = None) -> Dict[str, Any]:
        """Accept a worker result only while its lease is current.

        When ``schema`` is given the bound receipt is additionally run through
        ``receipt_verifier.verify_receipt`` (schema/hash/freshness/provenance -- issue #288)
        before being recorded; the verdict is attached under ``verification`` and a
        non-``VERIFIED`` result raises :class:`ReceiptVerificationFailed` instead of being
        recorded as if it were trustworthy. ``schema=None`` preserves the previous
        existence-only behavior for callers not yet passing a schema.
        """
        self.assert_active(attempt)
        result = bind_receipt(receipt, attempt.lease.identity or {"agent_id": attempt.lease.agent_id},
                              context_pack=attempt.context)
        result.update({"schema": result.get("schema") or SCHEMA, "run_id": self.run_id,
                       "work_item_id": attempt.work_item_id, "attempt_id": attempt.attempt_id,
                       "fencing_token": attempt.lease.fencing_token, "lease_id": attempt.lease.lease_id})
        if schema is not None:
            verdict = verify_receipt(result, schema=schema, max_age_seconds=max_age_seconds)
            result["verification"] = verdict.to_dict()
            if not verdict.verified:
                raise ReceiptVerificationFailed(verdict)
        self._append_json(attempt, result, name="receipt")
        return result

    def complete(self, attempt: WorkItemAttempt, *, receipt_ref: str) -> Dict[str, Any]:
        self.assert_active(attempt)
        result = self.queue.complete(attempt.lease, receipt_ref=receipt_ref)
        # The queue transition makes the lease terminal; record the child event after it
        # without re-checking a lease that is intentionally no longer active.
        self._append_terminal(attempt, "completed", {"receipt_ref": receipt_ref})
        return {**result, "run_id": self.run_id, "work_item_id": attempt.work_item_id,
                "attempt_id": attempt.attempt_id}

    def verify_and_complete(self, attempt: WorkItemAttempt, receipt: Mapping[str, Any], *,
                            receipt_ref: str, schema: ReceiptSchema,
                            max_age_seconds: Optional[float] = None) -> Dict[str, Any]:
        """Gate ``complete`` on a passing ``receipt_verifier`` verdict, not just file existence.

        This is the wiring the #286 remote-worker flow was missing: a worker cannot
        transition a lease to ``completed`` in the shared queue merely by presenting a
        ``receipt_ref`` path -- the receipt content itself must pass schema, hash,
        freshness and provenance checks first (:class:`ReceiptVerificationFailed` is raised
        otherwise, and the lease is left active so a corrected receipt/retry can follow).
        """
        accepted = self.accept_receipt(attempt, receipt, schema=schema, max_age_seconds=max_age_seconds)
        completed = self.complete(attempt, receipt_ref=receipt_ref)
        return {**completed, "verification": accepted["verification"]}

    def retry(self, attempt: WorkItemAttempt, *, reason: str = "retry") -> WorkItemAttempt:
        """Release a bounded attempt; the next claim receives a new fence and attempt id."""
        self.assert_active(attempt)
        self.queue.release(attempt.lease, reason=reason)
        self._append_terminal(attempt, "released", {"reason": reason})
        identity = attempt.lease.identity or {"agent_id": attempt.lease.agent_id}
        return self.claim(work_item_id=attempt.work_item_id, identity=identity,
                          goal=attempt.context["goal"], acs=attempt.context.get("acs", ()),
                          depends_on=attempt.context.get("depends_on", ()),
                          source_refs=attempt.context.get("source_refs", ()),
                          allowed_paths=attempt.context.get("source_refs", ()),
                          issue_ref=str(attempt.context.get("issue_ref") or ""),
                          issue_url=str(attempt.context.get("issue_url") or ""))

    def run_guarded(self, attempt: WorkItemAttempt, argv: Sequence[str], *, cwd: str | Path,
                     timeout: float = 180.0, heartbeat_interval: float = 5.0,
                     ttl: float = 60.0, env: Optional[Mapping[str, str]] = None) -> subprocess.CompletedProcess:
        """Run a mutating subprocess while heartbeating the lease in the background.

        Fixes the epic-183 gap where a long-running operator invocation (e.g.
        `simplicio-dev-cli`) held no lease/fencing awareness once started: a worker
        could lose its lease mid-mutation and keep writing to the checkout. Here a
        background thread calls ``assert_active`` (via ``heartbeat``) every
        ``heartbeat_interval`` seconds for the life of the subprocess; the moment the
        lease is no longer current, the subprocess is killed and
        :class:`LeaseLostDuringExecution` is raised instead of returning a result that
        looks successful. On graceful completion the final exit is still fenced by a
        last :meth:`assert_active` check before the caller sees the result.

        ``env`` lets a caller (e.g. ``runner.py::execute_operator``) pass its own operator
        environment through the guard instead of silently falling back to this process's
        environment -- a guarded dispatch must see the exact env an unguarded dispatch would.
        """
        self.assert_active(attempt)
        proc = subprocess.Popen(
            list(argv), cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            stdin=subprocess.DEVNULL, env=dict(env) if env is not None else None,
        )
        lease_lost: List[BaseException] = []
        stop = threading.Event()

        def _watch() -> None:
            while not stop.wait(heartbeat_interval):
                try:
                    self.queue.heartbeat(attempt.lease, ttl=ttl)
                except Exception as exc:  # noqa: BLE001 - any lease failure kills the child
                    lease_lost.append(exc)
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    return

        watcher = threading.Thread(target=_watch, name="lease-heartbeat", daemon=True)
        watcher.start()
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            stop.set()
            watcher.join(timeout=heartbeat_interval + 1)
            if lease_lost:
                raise LeaseLostDuringExecution(attempt.work_item_id, attempt.attempt_id, lease_lost[0]) from lease_lost[0]
            raise
        stop.set()
        watcher.join(timeout=heartbeat_interval + 1)
        if lease_lost:
            raise LeaseLostDuringExecution(attempt.work_item_id, attempt.attempt_id, lease_lost[0]) from lease_lost[0]
        # Final fence check: reject a result produced after the lease already expired
        # even if the watcher hadn't ticked yet (short-lived subprocess race).
        self.assert_active(attempt)
        return subprocess.CompletedProcess(list(argv), proc.returncode, stdout, stderr)

    def _path(self, attempt: WorkItemAttempt, name: str = "events") -> Path | None:
        if self.receipt_dir is None:
            return None
        path = self.receipt_dir / self.run_id / attempt.work_item_id / attempt.attempt_id
        path.mkdir(parents=True, exist_ok=True)
        return path / (name + ".jsonl")

    def _append_json(self, attempt: WorkItemAttempt, value: Mapping[str, Any], *, name: str = "events") -> None:
        path = self._path(attempt, name)
        if path is not None:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(dict(value), ensure_ascii=False, sort_keys=True) + "\n")

    def _append(self, attempt: WorkItemAttempt, kind: str, payload: Mapping[str, Any]) -> None:
        self.record_event(attempt, kind, payload) if kind != "claimed" else self._append_json(
            attempt, {"schema": EVENT_SCHEMA, "kind": kind, "run_id": self.run_id,
                      "work_item_id": attempt.work_item_id, "attempt_id": attempt.attempt_id,
                      "agent_id": attempt.lease.agent_id,
                      "agent": {field: attempt.context["assigned_to"][field] for field in IDENTITY_FIELDS},
                      "fencing_token": attempt.lease.fencing_token, "payload": dict(payload),
                      **({"issue_ref": attempt.context["issue_ref"], "issue_url": attempt.context["issue_url"]}
                         if attempt.context.get("issue_ref") else {})})

    def _append_terminal(self, attempt: WorkItemAttempt, kind: str, payload: Mapping[str, Any]) -> None:
        self._append_json(attempt, {"schema": EVENT_SCHEMA, "kind": kind, "run_id": self.run_id,
                                    "work_item_id": attempt.work_item_id, "attempt_id": attempt.attempt_id,
                                    "agent_id": attempt.lease.agent_id,
                                    "agent": {field: attempt.context["assigned_to"][field] for field in IDENTITY_FIELDS},
                                    "fencing_token": attempt.lease.fencing_token, "payload": dict(payload),
                                    **({"issue_ref": attempt.context["issue_ref"], "issue_url": attempt.context["issue_url"]}
                                       if attempt.context.get("issue_ref") else {})})


__all__ = ["AttemptCoordinator", "EVENT_SCHEMA", "LeaseLostDuringExecution", "ReceiptVerificationFailed",
           "SCHEMA", "WorkItemAttempt"]
