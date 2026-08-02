"""MapperStore-backed retry/dead-letter layer for the Hub queue.

MapperStore owns task, lease and fencing authority.  Hub-specific retry,
dead-letter, held-admission and scheduler metadata are reconstructible journal
projections and never create Hub-owned SQLite tables.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .mapper_remote_queue import MapperRemoteQueue
from .remote_queue import Lease, QueueConflict, QueueUnavailable, _lease_from_json, _lease_json


QUEUE_SCHEMA = "simplicio.hub-queue/v1"
ADMISSION_RECEIPT_SCHEMA = "simplicio.hub-admission-receipt/v1"
EVENT_SCHEMA = "simplicio.loop.hub-retry-event/v1"
DEFAULT_SCHEDULER_POLICY = "fair-drr-v2"


class QueueRetryError(RuntimeError):
    """Base durable retry error."""


class QueueLeaseError(QueueRetryError):
    """Raised for stale or missing task leases."""


class QueueCorruptionError(QueueRetryError):
    """Raised when the Mapper journal cannot be replayed safely."""

    def __init__(self, message: str, *, preserved_path: str) -> None:
        super().__init__(message)
        self.preserved_path = preserved_path


@dataclass(frozen=True)
class RetryLease:
    task_id: str
    lease_id: str
    fence: int | str
    expires_at: float
    mapper_attempt_id: str = ""
    mapper_fence: str = ""


class HubRetryQueue:
    """Hub retry facade backed by MapperStore operations and an append-only journal."""

    _SLOT_ID = "hub-retry-queue"
    _SLOT_CAPACITY = 1024
    _path_locks: dict[str, threading.RLock] = {}
    _path_locks_guard = threading.Lock()

    def __init__(self, path: str) -> None:
        self.path = str(Path(path).expanduser().absolute())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._path_locks_guard:
            self._process_lock = self._path_locks.setdefault(self.path, threading.RLock())
        self._mapper = MapperRemoteQueue(self.path, auto_create=True, slot_id=self._SLOT_ID)
        try:
            self._mapper.initialize()
            self._mapper.operations.register_slot(self._SLOT_ID, self._SLOT_CAPACITY)
        except Exception as exc:
            preserved = self._preserve_corrupt_file()
            if preserved:
                raise QueueCorruptionError(
                    "hub retry MapperStore file could not be opened; preserved at %s" % preserved,
                    preserved_path=preserved,
                ) from exc
            if isinstance(exc, (QueueRetryError, QueueConflict, QueueUnavailable)):
                raise
            raise QueueRetryError("MapperStore queue unavailable: %s" % exc) from exc
        self._run_id = "simplicio.loop.hub-retry:" + self.path
        self._lock = threading.RLock()

    def _preserve_corrupt_file(self) -> str | None:
        source = Path(self.path)
        if not source.is_file() or source.stat().st_size == 0:
            return None
        preserved = Path("%s.corrupt-%d" % (self.path, int(time.time() * 1000)))
        try:
            shutil.copy2(source, preserved)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(self.path + suffix)
                if sidecar.exists():
                    shutil.copy2(sidecar, str(preserved) + suffix)
        except OSError:
            return None
        return str(preserved)

    @staticmethod
    def _initial_state() -> dict[str, Any]:
        return {
            "schema": QUEUE_SCHEMA,
            "jobs": {},
            "dead_letters": {},
            "admissions": {},
            "scheduler_manifest": None,
        }

    @staticmethod
    def _clone(value: Any) -> Any:
        return json.loads(json.dumps(value, sort_keys=True, default=str))

    @staticmethod
    def _last_seq(replay: Dict[str, Any]) -> int:
        events = replay.get("events") or []
        if events:
            return int(events[-1]["seq"])
        compaction = replay.get("compaction")
        return int(compaction["through_seq"]) if compaction else 0

    def _state(self) -> dict[str, Any]:
        try:
            replay = self._mapper.operations.replay(self._run_id)
        except Exception as exc:
            raise QueueCorruptionError(
                "hub retry journal unavailable: %s" % exc, preserved_path=self.path
            ) from exc
        if not replay.get("valid", False):
            raise QueueCorruptionError(
                "hub retry journal failed validation", preserved_path=self.path
            )
        state = self._initial_state()
        for event in replay.get("events") or []:
            payload = event.get("payload") or {}
            if payload.get("schema") != EVENT_SCHEMA or payload.get("operation") != "snapshot":
                raise QueueCorruptionError(
                    "hub retry journal contains an unknown event", preserved_path=self.path
                )
            state = self._clone(payload.get("state") or state)
        return state

    def _commit(self, replay: Dict[str, Any], state: Dict[str, Any]) -> None:
        try:
            self._mapper.operations.append_event(
                self._run_id,
                "hub-retry.snapshot",
                {"schema": EVENT_SCHEMA, "operation": "snapshot", "state": self._clone(state)},
                expected_seq=self._last_seq(replay),
            )
        except Exception as exc:
            raise QueueRetryError("hub retry journal append failed: %s" % exc) from exc

    def _save(self, state: Dict[str, Any]) -> None:
        replay = self._mapper.operations.replay(self._run_id)
        self._commit(replay, state)

    @classmethod
    def _canonical_json(cls, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _value_digest(cls, value: Any) -> str:
        return hashlib.sha256(cls._canonical_json(value).encode()).hexdigest()

    def close(self) -> None:
        return

    def submit(
        self,
        payload: Dict[str, Any],
        *,
        idempotency_key: str,
        max_attempts: int = 3,
        client_id: Any = "",
        workspace_id: str = "default",
        weight: int = 1,
        cost: int = 1,
        scheduler_policy: str = DEFAULT_SCHEDULER_POLICY,
    ) -> str:
        if not idempotency_key or max_attempts < 1:
            raise QueueRetryError("idempotency_key and positive max_attempts required")
        if not isinstance(scheduler_policy, str) or not scheduler_policy:
            raise QueueRetryError("scheduler_policy must be non-empty")
        with self._process_lock, self._lock:
            state = self._state()
            for job in state["jobs"].values():
                if job["idempotency_key"] == idempotency_key:
                    if job["state"] == "admitted_held":
                        raise QueueRetryError("held admission cannot be submitted")
                    return str(job["task_id"])
            task_id = "hub-" + hashlib.sha256(str(idempotency_key).encode("utf-8")).hexdigest()[:32]
            effective_client = self._effective_client_id(payload, client_id)
            self._mapper.enqueue(
                task_id, dict(payload), idempotency_key="hub:" + str(idempotency_key)
            )
            now = time.time()
            state["jobs"][task_id] = {
                "task_id": task_id, "idempotency_key": str(idempotency_key),
                "payload": self._clone(payload), "max_attempts": int(max_attempts),
                "attempts": 0, "state": "queued", "next_attempt_at": now,
                "lease_id": None, "fence": 0, "lease_expires_at": None,
                "error_code": None, "updated_at": now, "client_id": effective_client,
                "workspace_id": str(workspace_id), "weight": int(weight), "cost": int(cost),
                "scheduler_policy": scheduler_policy, "mapper_lease": None,
            }
            self._save(state)
            return task_id

    @staticmethod
    def _effective_client_id(payload: Any, explicit_client_id: Any) -> str:
        if isinstance(explicit_client_id, str) and explicit_client_id:
            return explicit_client_id
        if isinstance(payload, dict) and isinstance(payload.get("client_id"), str):
            return str(payload["client_id"])
        return ""

    @staticmethod
    def _valid_nonnegative_counts(value: Any, keys: Set[str]) -> bool:
        return (
            isinstance(value, dict) and set(value) == keys
            and all(isinstance(item, int) and not isinstance(item, bool) and item >= 0
                    for item in value.values())
        )

    def _validate_capacity_snapshot(self, snapshot: Dict[str, Any]) -> None:
        from .hub_governor import RESOURCE_NAMES
        scheduler = snapshot.get("scheduler") if isinstance(snapshot, dict) else None
        governor = snapshot.get("governor") if isinstance(snapshot, dict) else None
        scheduler_limit_keys = {
            "max_inflight_per_client", "max_queue_per_client", "max_queue_per_workspace",
            "max_global_queue", "quantum", "aging_ticks", "aging_boost",
        }
        circuit_keys = {"state", "failures", "threshold", "cooldown_seconds"}
        limits = scheduler.get("limits") if isinstance(scheduler, dict) else None
        valid_limits = (
            isinstance(limits, dict) and set(limits) == scheduler_limit_keys
            and all(isinstance(limits[name], int) and not isinstance(limits[name], bool)
                    and limits[name] >= 1
                    for name in {"max_inflight_per_client", "quantum", "aging_ticks", "aging_boost"})
            and all(limits[name] is None or (
                isinstance(limits[name], int) and not isinstance(limits[name], bool)
                and limits[name] >= 1
            ) for name in {"max_queue_per_client", "max_queue_per_workspace", "max_global_queue"})
        )
        circuit = governor.get("circuit") if isinstance(governor, dict) else None
        valid_circuit = (
            isinstance(circuit, dict) and set(circuit) == circuit_keys
            and circuit.get("state") in {"closed", "open", "half_open"}
            and isinstance(circuit.get("failures"), int) and not isinstance(circuit.get("failures"), bool)
            and circuit.get("failures") >= 0
            and isinstance(circuit.get("threshold"), int) and not isinstance(circuit.get("threshold"), bool)
            and circuit.get("threshold") >= 1
            and isinstance(circuit.get("cooldown_seconds"), (int, float))
            and not isinstance(circuit.get("cooldown_seconds"), bool)
            and circuit.get("cooldown_seconds") >= 0
        )
        valid_governor = (
            isinstance(governor, dict)
            and set(governor) == {"limits", "used", "target_client_used", "draining", "circuit"}
            and self._valid_nonnegative_counts(governor.get("limits"), set(RESOURCE_NAMES))
            and self._valid_nonnegative_counts(governor.get("used"), set(RESOURCE_NAMES))
            and self._valid_nonnegative_counts(governor.get("target_client_used"), set(RESOURCE_NAMES))
            and isinstance(governor.get("draining"), bool) and valid_circuit
        )
        if not (
            isinstance(snapshot, dict) and set(snapshot) == {
                "schema", "reservation", "fresh_snapshot_required_at_activation", "scheduler", "governor"
            }
            and snapshot.get("schema") == "simplicio.hub-capacity-observation/v1"
            and snapshot.get("reservation") is False
            and snapshot.get("fresh_snapshot_required_at_activation") is True
            and isinstance(scheduler, dict)
            and set(scheduler) == {"limits", "global", "target_client", "target_workspace"}
            and valid_limits
            and self._valid_nonnegative_counts(scheduler.get("global"), {"queued", "global_total", "clients"})
            and self._valid_nonnegative_counts(scheduler.get("target_client"), {"total", "inflight"})
            and self._valid_nonnegative_counts(scheduler.get("target_workspace"), {"total"})
            and valid_governor
        ):
            raise QueueRetryError("capacity snapshot is invalid or unsanitized")

    def _validate_held_input(
        self, job: Dict[str, Any], *, idempotency_key: str, input_digest: str,
        client_id: str, workspace_id: str, weight: int, cost: int,
    ) -> None:
        from .github_drain_admission import (
            DrainAdmissionProjectionError, admission_idempotency_key, admission_input_digest,
            validate_admission_metadata, validate_projected_job,
        )
        if not isinstance(job, dict):
            raise QueueRetryError("held admission job must be an object")
        try:
            validate_projected_job(job)
        except (DrainAdmissionProjectionError, TypeError, ValueError) as exc:
            raise QueueRetryError("held admission job projection is invalid") from exc
        try:
            validate_admission_metadata(
                client_id=client_id, workspace_id=workspace_id, weight=weight, cost=cost,
            )
            expected_key = admission_idempotency_key(job)
            expected_digest = admission_input_digest(
                job, client_id=client_id, workspace_id=workspace_id, weight=weight, cost=cost,
            )
        except (DrainAdmissionProjectionError, TypeError, ValueError) as exc:
            raise QueueRetryError("held admission identity/metadata is invalid") from exc
        if idempotency_key != expected_key or input_digest != expected_digest:
            raise QueueRetryError("held admission identity/input is invalid")

    def _build_admission_receipt(
        self, task_id: str, job: Dict[str, Any], *, idempotency_key: str,
        input_digest: str, capacity_snapshot: Dict[str, Any], now: float,
    ) -> Dict[str, Any]:
        receipt: Dict[str, Any] = {
            "schema": ADMISSION_RECEIPT_SCHEMA, "task_id": task_id,
            "idempotency_key": idempotency_key, "input_digest": input_digest,
            "state": "admitted_held", "recovery": "ADMITTED_NOT_DISPATCHED",
            "execution_authorized": False, "capacity_snapshot": self._clone(capacity_snapshot),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        }
        receipt["receipt_hash"] = self._value_digest(receipt)
        return receipt

    def _check_admission(self, receipt: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(receipt, dict):
            raise QueueRetryError("stored admission receipt is invalid")
        receipt_keys = {
            "schema", "task_id", "idempotency_key", "input_digest", "state", "recovery",
            "execution_authorized", "capacity_snapshot", "created_at", "receipt_hash",
        }
        payload = {key: value for key, value in receipt.items() if key != "receipt_hash"}
        if (
            set(receipt) != receipt_keys or receipt.get("schema") != ADMISSION_RECEIPT_SCHEMA
            or receipt.get("state") != "admitted_held"
            or receipt.get("recovery") != "ADMITTED_NOT_DISPATCHED"
            or receipt.get("execution_authorized") is not False
            or not isinstance(receipt.get("created_at"), str)
            or len(receipt["created_at"]) != 20
            or receipt.get("receipt_hash") != self._value_digest(payload)
        ):
            raise QueueRetryError("stored admission receipt failed validation")
        try:
            if time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.strptime(receipt["created_at"], "%Y-%m-%dT%H:%M:%SZ")
            ) != receipt["created_at"]:
                raise ValueError("non-canonical timestamp")
        except (TypeError, ValueError, OverflowError, OSError) as exc:
            raise QueueRetryError("stored admission receipt failed validation") from exc
        try:
            from .github_drain_admission import (
                admission_idempotency_key, admission_input_digest, validate_projected_job,
            )
            state = self._state()
            admission = next(
                (item for item in state["admissions"].values()
                 if item["receipt"].get("task_id") == receipt.get("task_id")), None
            )
            if admission is None:
                raise QueueRetryError("stored admission receipt is invalid")
            if receipt.get("created_at") != admission["receipt"].get("created_at"):
                raise QueueRetryError("stored admission receipt failed validation")
            validate_projected_job(json.loads(admission["job"]))
            job = json.loads(admission["job"])
            if receipt["idempotency_key"] != admission_idempotency_key(job) or receipt["input_digest"] != admission_input_digest(
                job, client_id=state["jobs"][receipt["task_id"]]["client_id"],
                workspace_id=state["jobs"][receipt["task_id"]]["workspace_id"],
                weight=state["jobs"][receipt["task_id"]]["weight"],
                cost=state["jobs"][receipt["task_id"]]["cost"],
            ):
                raise QueueRetryError("stored admission identity is invalid")
            self._validate_capacity_snapshot(receipt.get("capacity_snapshot"))
        except QueueRetryError:
            raise
        except Exception as exc:
            raise QueueRetryError("stored admission identity is invalid") from exc
        return receipt

    def _after_held_job_insert(self, task_id: str) -> None:
        return

    def admit_held(
        self, job: Dict[str, Any], *, idempotency_key: str, input_digest: str,
        client_id: str, workspace_id: str = "default", weight: int = 1, cost: int = 1,
        capacity_snapshot: Dict[str, Any],
    ) -> Dict[str, Any]:
        self._validate_held_input(
            job, idempotency_key=idempotency_key, input_digest=input_digest,
            client_id=client_id, workspace_id=workspace_id, weight=weight, cost=cost,
        )
        self._validate_capacity_snapshot(capacity_snapshot)
        with self._process_lock, self._lock:
            state = self._state()
            queued = next(
                (item for item in state["jobs"].values()
                 if item["idempotency_key"] == idempotency_key), None
            )
            if queued is not None and idempotency_key not in state["admissions"]:
                raise QueueRetryError("idempotency key collides with a non-admission job")
            existing = state["admissions"].get(idempotency_key)
            if existing:
                current = state["jobs"].get(existing["task_id"])
                if (
                    existing["job"] != self._canonical_json(job)
                    or not current
                    or current["client_id"] != client_id
                    or current["workspace_id"] != workspace_id
                    or int(current["weight"]) != int(weight)
                    or int(current["cost"]) != int(cost)
                    or existing["receipt"].get("input_digest") != input_digest
                ):
                    raise QueueRetryError("idempotency key conflicts with different held input")
                return self._check_admission(existing["receipt"])
            task_id = "hub-held-" + hashlib.sha256(str(idempotency_key).encode("utf-8")).hexdigest()[:32]
            now = time.time()
            self._after_held_job_insert(task_id)
            self._mapper.enqueue(task_id, dict(job), idempotency_key="hub-held:" + idempotency_key)
            receipt = self._build_admission_receipt(
                task_id, job, idempotency_key=idempotency_key, input_digest=input_digest,
                capacity_snapshot=capacity_snapshot, now=now,
            )
            state["jobs"][task_id] = {
                "task_id": task_id, "idempotency_key": idempotency_key,
                "payload": self._clone(job), "max_attempts": 1, "attempts": 0,
                "state": "admitted_held", "next_attempt_at": now, "lease_id": None,
                "fence": 0, "lease_expires_at": None, "error_code": None,
                "updated_at": now, "client_id": client_id, "workspace_id": workspace_id,
                "weight": int(weight), "cost": int(cost), "scheduler_policy": DEFAULT_SCHEDULER_POLICY,
                "mapper_lease": None,
            }
            state["admissions"][idempotency_key] = {
                "task_id": task_id, "job": self._canonical_json(job), "receipt": receipt,
            }
            self._save(state)
            return receipt

    def admission(self, *, task_id: str = "", idempotency_key: str = "") -> Dict[str, Any]:
        if bool(task_id) == bool(idempotency_key):
            raise QueueRetryError("exactly one of task_id or idempotency_key is required")
        with self._process_lock, self._lock:
            state = self._state()
            key = idempotency_key or next(
                (key for key, value in state["admissions"].items() if value["task_id"] == task_id), ""
            )
            value = state["admissions"].get(key)
            if value is None:
                raise QueueRetryError("unknown held admission")
            return self._check_admission(value["receipt"])

    def _job(self, state: Dict[str, Any], task_id: str) -> Dict[str, Any]:
        job = state["jobs"].get(task_id)
        if job is None:
            raise QueueRetryError("unknown task")
        return job

    @staticmethod
    def _lease_from_job(job: Dict[str, Any]) -> RetryLease:
        mapper_lease = job.get("mapper_lease") or {}
        return RetryLease(
            str(job["task_id"]), str(job["lease_id"]), job["fence"],
            float(job["lease_expires_at"]), str(mapper_lease.get("attempt_id") or ""),
            str(mapper_lease.get("fencing_token") or ""),
        )

    @staticmethod
    def _mapper_lease(job: Dict[str, Any]) -> Lease:
        return _lease_from_json(job["mapper_lease"])

    def _owned(self, state: Dict[str, Any], lease: RetryLease) -> Dict[str, Any]:
        job = self._job(state, lease.task_id)
        if (
            job["state"] != "leased" or str(job.get("lease_id")) != lease.lease_id
            or str(job.get("fence")) != str(lease.fence)
            or float(job.get("lease_expires_at") or 0) <= time.time()
        ):
            raise QueueLeaseError("lease is stale, expired, or missing")
        try:
            self._mapper.assert_active(self._mapper_lease(job))
        except (QueueConflict, KeyError) as exc:
            raise QueueLeaseError("lease is stale, expired, or missing") from exc
        return job

    def claim(self, worker_id: str, *, ttl: float = 30.0) -> Optional[RetryLease]:
        if not worker_id or ttl <= 0:
            raise QueueRetryError("worker_id and positive ttl required")
        with self._process_lock, self._lock:
            state = self._state()
            now = time.time()
            candidates = [
                job for job in state["jobs"].values()
                if job["state"] == "queued" and job["next_attempt_at"] <= now
            ]
            expired = [
                job for job in state["jobs"].values()
                if job["state"] == "leased" and float(job["lease_expires_at"] or 0) <= now
            ]
            row = sorted(candidates + expired, key=lambda job: (job["updated_at"], job["task_id"]))
            for job in row:
                lease = self.claim_specific(str(job["task_id"]), worker_id, ttl=ttl)
                if lease is not None:
                    return lease
            return None

    def claim_specific(self, task_id: str, worker_id: str, *, ttl: float = 30.0) -> Optional[RetryLease]:
        if not task_id or not worker_id or ttl <= 0:
            raise QueueRetryError("task_id, worker_id and positive ttl required")
        with self._process_lock, self._lock:
            state = self._state()
            job = self._job(state, task_id)
            now = time.time()
            if job["state"] == "admitted_held" or (
                job["state"] not in {"queued", "leased"} or job["next_attempt_at"] > now
            ):
                return None
            if job["state"] == "leased" and float(job.get("lease_expires_at") or 0) > now:
                return None
            self._mapper.operations.reclaim_expired()
            try:
                mapper_lease = self._mapper.claim(
                    task_id, worker_id, idempotency_key="hub-claim:" + task_id,
                    ttl=ttl, identity={"agent_id": worker_id},
                )
            except QueueConflict:
                return None
            old_fence = int(job.get("fence") or 0)
            now = time.time()
            job.update({
                "state": "leased", "attempts": int(job["attempts"]) + 1,
                "lease_id": mapper_lease.lease_id, "fence": old_fence + 1,
                "lease_expires_at": mapper_lease.expires_at, "updated_at": now,
                "mapper_lease": _lease_json(mapper_lease),
            })
            self._save(state)
            return self._lease_from_job(job)

    def get_payload(self, task_id: str) -> Dict[str, Any]:
        with self._process_lock, self._lock:
            return self._clone(self._job(self._state(), task_id)["payload"])

    def list_queued_scheduling_metadata(self) -> List[Dict[str, Any]]:
        now = time.time()
        with self._process_lock, self._lock:
            state = self._state()
            return [
                {key: job[key] for key in (
                    "task_id", "client_id", "workspace_id", "weight", "cost", "scheduler_policy"
                )}
                for job in sorted(state["jobs"].values(), key=lambda item: (item["updated_at"], item["task_id"]))
                if (job["state"] == "queued" and job["next_attempt_at"] <= now)
                or (job["state"] == "leased" and float(job.get("lease_expires_at") or 0) <= now)
            ]

    def scheduler_manifest(self) -> Optional[Dict[str, Any]]:
        with self._process_lock, self._lock:
            value = self._state().get("scheduler_manifest")
            return self._clone(value) if value is not None else None

    def set_scheduler_manifest(self, manifest: Dict[str, Any]) -> None:
        if manifest.get("schema") != "simplicio.hub-scheduler-policy/v1":
            raise QueueRetryError("invalid scheduler manifest schema")
        with self._process_lock, self._lock:
            state = self._state()
            state["scheduler_manifest"] = self._clone(manifest)
            self._save(state)

    def heartbeat(self, lease: RetryLease, *, ttl: float = 30.0) -> RetryLease:
        if ttl <= 0:
            raise QueueRetryError("ttl must be positive")
        with self._process_lock, self._lock:
            state = self._state()
            job = self._owned(state, lease)
            refreshed = self._mapper.heartbeat(self._mapper_lease(job), ttl=ttl)
            job["lease_expires_at"] = refreshed.expires_at
            job["mapper_lease"] = _lease_json(refreshed)
            job["updated_at"] = time.time()
            self._save(state)
            return self._lease_from_job(job)

    def complete(self, lease: RetryLease) -> None:
        with self._process_lock, self._lock:
            state = self._state()
            job = self._owned(state, lease)
            mapper_lease = self._mapper_lease(job)
            receipt = self._mapper.build_completion_receipt(
                task_id=mapper_lease.task_id, agent_id=mapper_lease.agent_id,
                fencing_token=str(mapper_lease.fencing_token),
                receipt_ref="hub-complete:" + lease.task_id,
            )
            self._mapper.complete(mapper_lease, receipt_ref=receipt["receipt_ref"], receipt=receipt)
            job.update({"state": "completed", "lease_expires_at": None, "updated_at": time.time()})
            self._save(state)

    def fail(self, lease: RetryLease, *, error_code: str, backoff: float = 0.0) -> str:
        if not error_code:
            raise QueueRetryError("error_code is required")
        with self._process_lock, self._lock:
            state = self._state()
            job = self._owned(state, lease)
            now = time.time()
            if int(job["attempts"]) >= int(job["max_attempts"]):
                mapper_lease = self._mapper_lease(job)
                failure_receipt = self._mapper.build_completion_receipt(
                    task_id=mapper_lease.task_id, agent_id=mapper_lease.agent_id,
                    fencing_token=str(mapper_lease.fencing_token),
                    receipt_ref="hub-dead-letter:" + lease.task_id,
                    extra={"error_code": error_code, "attempts": int(job["attempts"])},
                )
                self._mapper.complete(
                    mapper_lease, receipt_ref=failure_receipt["receipt_ref"],
                    receipt=failure_receipt, status="failed",
                )
                job.update({"state": "dead_letter", "error_code": error_code,
                            "lease_expires_at": None, "mapper_lease": None, "updated_at": now})
                state["dead_letters"][lease.task_id] = {
                    "task_id": lease.task_id, "payload": self._clone(job["payload"]),
                    "attempts": int(job["attempts"]), "error_code": error_code, "moved_at": now,
                }
                self._save(state)
                return "dead_letter"
            self._mapper.release(self._mapper_lease(job), reason="hub-failure")
            job.update({"state": "queued", "next_attempt_at": now + max(0.0, backoff),
                        "error_code": error_code, "lease_id": None,
                        "lease_expires_at": None, "mapper_lease": None, "updated_at": now})
            self._save(state)
            return "retry"

    def dead_letters(self) -> List[Dict[str, Any]]:
        with self._process_lock, self._lock:
            state = self._state()
            return [self._clone(item) for item in sorted(
                state["dead_letters"].values(), key=lambda item: (item["moved_at"], item["task_id"])
            )]

    def requeue(self, task_id: str) -> None:
        with self._process_lock, self._lock:
            state = self._state()
            job = self._job(state, task_id)
            if job["state"] != "dead_letter":
                raise QueueRetryError("only dead-letter tasks can be requeued")
            self._mapper.requeue(task_id)
            job.update({"state": "queued", "next_attempt_at": time.time(), "error_code": None,
                        "lease_id": None, "lease_expires_at": None, "mapper_lease": None,
                        "updated_at": time.time()})
            state["dead_letters"].pop(task_id, None)
            self._save(state)

    def state(self, task_id: str) -> str:
        with self._process_lock, self._lock:
            return str(self._job(self._state(), task_id)["state"])

    def find_task_id(self, idempotency_key: str) -> Optional[str]:
        with self._process_lock, self._lock:
            return next(
                (str(job["task_id"]) for job in self._state()["jobs"].values()
                 if job["idempotency_key"] == idempotency_key),
                None,
            )

    def get_row(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._process_lock, self._lock:
            state = self._state()
            job = state["jobs"].get(task_id)
            if job is None:
                return None
            value = self._clone(job)
            value.pop("mapper_lease", None)
            return value

    def update_payload(self, task_id: str, payload: Dict[str, Any]) -> None:
        with self._process_lock, self._lock:
            state = self._state()
            job = self._job(state, task_id)
            if job["state"] == "admitted_held":
                raise QueueRetryError("held admission payload is immutable")
            job["payload"] = self._clone(payload)
            job["updated_at"] = time.time()
            self._save(state)

    def count(self) -> int:
        with self._process_lock, self._lock:
            return len(self._state()["jobs"])

    def payload_of(self, task_id: str) -> Dict[str, Any]:
        return self.get_payload(task_id)

    def sync_fair_scheduler(self, scheduler: Any) -> None:
        from .hub_scheduler import ScheduledJob, SchedulerError
        for entry in self.list_queued_scheduling_metadata():
            try:
                scheduler.enqueue(ScheduledJob(
                    task_id=entry["task_id"], client_id=entry["client_id"] or "default",
                    weight=entry["weight"], cost=entry["cost"],
                    workspace_id=entry["workspace_id"],
                ))
            except SchedulerError:
                continue

    def claim_fair(self, scheduler: Any, worker_id: str, *, ttl: float = 30.0,
                   max_attempts: int = 256) -> Optional[RetryLease]:
        self.sync_fair_scheduler(scheduler)
        for _ in range(max_attempts):
            scheduled = scheduler.next()
            if scheduled is None:
                return None
            lease = self.claim_specific(scheduled.task_id, worker_id, ttl=ttl)
            try:
                scheduler.complete(scheduled.task_id)
            except Exception:
                pass
            if lease is not None:
                return lease
        return None
