"""Bounded fair admission and authority-scoped single-flight inference.

Only side-effect-free inference may coalesce. Tools, mutations, approvals, and
requests crossing authority/privacy/tool-registry boundaries never share work.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Tuple

PRIORITY_WEIGHT = {"interactive": 8.0, "mapping": 4.0, "inference": 2.0, "background": 1.0}

@dataclass(frozen=True)
class CapacityLimits:
    max_runnable: int = 32
    max_active_workers: int = 8
    max_inference_requests: int = 4
    max_backend_slots: int = 4
    max_memory_bytes: int = 0
    max_queue: int = 256
    def __post_init__(self) -> None:
        if any(value < 1 for value in (self.max_runnable, self.max_active_workers, self.max_inference_requests, self.max_backend_slots, self.max_queue)) or self.max_memory_bytes < 0:
            raise ValueError("capacity limits must be positive; memory must be non-negative")

@dataclass(frozen=True)
class AdmissionJob:
    job_id: str
    client_id: str
    session_id: str
    priority: str = "background"
    runnable: int = 1
    active_workers: int = 1
    inference_requests: int = 1
    backend_slots: int = 1
    memory_bytes: int = 0
    enqueued_at: int = 0
    def __post_init__(self) -> None:
        if not self.job_id or not self.client_id or not self.session_id or self.priority not in PRIORITY_WEIGHT:
            raise ValueError("job identity and priority are required")
        if any(value < 1 for value in (self.runnable, self.active_workers, self.inference_requests, self.backend_slots)) or self.memory_bytes < 0:
            raise ValueError("job resource costs are invalid")

@dataclass(frozen=True)
class AdmissionDecision:
    state: str
    reason: str
    job_id: str
    receipt: Mapping[str, Any]

class AdmissionRejected(RuntimeError):
    def __init__(self, decision: AdmissionDecision) -> None:
        self.decision = decision
        super().__init__(decision.reason)

class FairAdmissionController:
    """Independent resource caps, bounded pending work, priority and aging."""
    def __init__(self, limits: CapacityLimits = CapacityLimits(), *, aging_ticks: int = 20) -> None:
        if aging_ticks < 1:
            raise ValueError("aging_ticks must be positive")
        self.limits, self.aging_ticks = limits, aging_ticks
        self._pending: Dict[str, Deque[AdmissionJob]] = defaultdict(deque)
        self._pending_by_id: Dict[str, AdmissionJob] = {}
        self._active: Dict[str, AdmissionJob] = {}
        self._served: Dict[str, int] = defaultdict(int)
        self._tick = 0
        self._starvation_preventions = 0
    @property
    def queued(self) -> int:
        return len(self._pending_by_id)
    def _usage(self) -> Dict[str, int]:
        jobs = tuple(self._active.values())
        return {name: sum(getattr(job, name) for job in jobs) for name in ("runnable", "active_workers", "inference_requests", "backend_slots", "memory_bytes")}
    def _fits(self, job: AdmissionJob) -> bool:
        usage = self._usage()
        return all(usage[name] + getattr(job, name) <= getattr(self.limits, "max_" + name) for name in ("runnable", "active_workers", "inference_requests", "backend_slots")) and (not self.limits.max_memory_bytes or usage["memory_bytes"] + job.memory_bytes <= self.limits.max_memory_bytes)
    def _too_large(self, job: AdmissionJob) -> bool:
        return any(getattr(job, name) > getattr(self.limits, "max_" + name) for name in ("runnable", "active_workers", "inference_requests", "backend_slots")) or bool(self.limits.max_memory_bytes and job.memory_bytes > self.limits.max_memory_bytes)
    def _receipt(self, state: str, reason: str, job: AdmissionJob, position: Optional[int] = None) -> Dict[str, Any]:
        result = {"schema": "simplicio.inference-admission/v1", "state": state, "reason": reason, "job_id": job.job_id, "client_id": job.client_id, "session_id": job.session_id, "queued": self.queued, "active": len(self._active), "usage": self._usage(), "limits": self.limits.__dict__.copy()}
        if position is not None:
            result["queue_position"] = position
        return result
    def submit(self, job: AdmissionJob) -> AdmissionDecision:
        if job.job_id in self._active or job.job_id in self._pending_by_id:
            reason = "duplicate_job"
            return AdmissionDecision("rejected", reason, job.job_id, self._receipt("rejected", reason, job))
        if self._too_large(job):
            reason = "job_exceeds_limit"
            return AdmissionDecision("rejected", reason, job.job_id, self._receipt("rejected", reason, job))
        self._tick += 1
        if self._fits(job):
            self._active[job.job_id] = job
            return AdmissionDecision("admitted", "capacity_available", job.job_id, self._receipt("admitted", "capacity_available", job))
        if self.queued >= self.limits.max_queue:
            return AdmissionDecision("rejected", "queue_saturated", job.job_id, self._receipt("rejected", "queue_saturated", job))
        queued = replace(job, enqueued_at=self._tick)
        self._pending[queued.client_id].append(queued)
        self._pending_by_id[queued.job_id] = queued
        return AdmissionDecision("deferred", "capacity_unavailable", job.job_id, self._receipt("deferred", "capacity_unavailable", queued, self.queued))
    def _score(self, job: AdmissionJob) -> Tuple[float, float, int]:
        waited = self._tick - job.enqueued_at
        if waited >= self.aging_ticks:
            self._starvation_preventions += 1
        return (PRIORITY_WEIGHT[job.priority] + max(0, waited) * 4.0 / self.aging_ticks, -float(self._served[job.client_id]), -job.enqueued_at)
    def next(self) -> Optional[AdmissionJob]:
        self._tick += 1
        choices = [(self._score(queue[0]), client, queue[0]) for client, queue in self._pending.items() if queue and self._fits(queue[0])]
        if not choices:
            return None
        _, client, job = max(choices, key=lambda item: item[0])
        self._pending[client].popleft()
        self._pending_by_id.pop(job.job_id, None)
        self._active[job.job_id] = job
        self._served[client] += 1
        return job
    def release(self, job_id: str) -> bool:
        return self._active.pop(job_id, None) is not None
    def cancel(self, job_id: str) -> bool:
        if job_id in self._active:
            return self.release(job_id)
        job = self._pending_by_id.pop(job_id, None)
        if job is None:
            return False
        self._pending[job.client_id] = deque(item for item in self._pending[job.client_id] if item.job_id != job_id)
        return True
    def status(self) -> Dict[str, Any]:
        return {"schema": "simplicio.inference-admission/v1", "queued": self.queued, "active": len(self._active), "usage": self._usage(), "limits": self.limits.__dict__.copy(), "served": dict(self._served), "starvation_preventions": self._starvation_preventions}

@dataclass(frozen=True)
class InferenceRequest:
    model_identity: str
    backend_identity: str
    stable_prefix_generation: str
    canonical_request_hash: str
    tool_registry_generation: str
    authority_scope: str
    privacy_scope: str
    side_effect_free: bool = False
    operation_kind: str = "inference"
    def equivalence_key(self) -> Optional[str]:
        if not self.side_effect_free or self.operation_kind != "inference" or not self.authority_scope or not self.privacy_scope:
            return None
        fields = {"model": self.model_identity, "backend": self.backend_identity, "stable_prefix": self.stable_prefix_generation, "request": self.canonical_request_hash, "tool_registry": self.tool_registry_generation, "authority": self.authority_scope, "privacy": self.privacy_scope}
        encoded = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

@dataclass(frozen=True)
class InferenceReceipt:
    correlation_id: str
    shared_execution_id: str
    equivalence_key: Optional[str]
    deduplicated: bool
    result_digest: str
    result: Any = field(compare=False)

@dataclass
class _SharedExecution:
    execution_id: str
    key: Optional[str]
    task: asyncio.Task
    future: asyncio.Future
    waiters: Dict[str, None] = field(default_factory=dict)
    admission_job_id: str = ""

class InferenceCoordinator:
    """Coalesce only safe in-flight inference; preserve waiter cancellation."""
    def __init__(self, admission: Optional[FairAdmissionController] = None) -> None:
        self.admission = admission or FairAdmissionController()
        self._lock = asyncio.Lock()
        self._inflight: Dict[str, _SharedExecution] = {}
        self._shared: Dict[str, _SharedExecution] = {}
    async def run(self, request: InferenceRequest, executor: Callable[[InferenceRequest], Any], *, client_id: str = "inference", session_id: str = "inference", priority: str = "inference", correlation_id: Optional[str] = None) -> InferenceReceipt:
        correlation_id = correlation_id or uuid.uuid4().hex
        key = request.equivalence_key()
        deduplicated = False
        async with self._lock:
            shared = self._inflight.get(key) if key is not None else None
            if shared is None:
                execution_id = uuid.uuid4().hex
                admission_id = "inference:" + execution_id
                decision = self.admission.submit(AdmissionJob(admission_id, client_id, session_id, priority=priority))
                if decision.state != "admitted":
                    raise AdmissionRejected(decision)
                future = asyncio.get_running_loop().create_future()
                task = asyncio.create_task(self._execute(execution_id, key, request, executor, future, admission_id))
                shared = _SharedExecution(execution_id, key, task, future, {correlation_id: None}, admission_id)
                self._shared[execution_id] = shared
                if key is not None:
                    self._inflight[key] = shared
            else:
                deduplicated = True
                shared.waiters[correlation_id] = None
        try:
            result = await asyncio.shield(shared.future)
        except asyncio.CancelledError:
            async with self._lock:
                shared.waiters.pop(correlation_id, None)
                if not shared.waiters and not shared.future.done():
                    shared.task.cancel()
            raise
        digest = hashlib.sha256(repr(result).encode("utf-8", errors="replace")).hexdigest()
        return InferenceReceipt(correlation_id, shared.execution_id, key, deduplicated, digest, copy.deepcopy(result))
    async def _execute(self, execution_id: str, key: Optional[str], request: InferenceRequest, executor: Callable[[InferenceRequest], Any], future: asyncio.Future, admission_id: str) -> None:
        try:
            result = executor(request)
            if inspect.isawaitable(result):
                result = await result
            if not future.done():
                future.set_result(result)
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            raise
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)
        finally:
            self.admission.release(admission_id)
            async with self._lock:
                if key is not None and self._inflight.get(key) is self._shared.get(execution_id):
                    self._inflight.pop(key, None)
                self._shared.pop(execution_id, None)
    def status(self) -> Dict[str, Any]:
        return {"schema": "simplicio.inference-coordinator/v1", "inflight": len(self._inflight), "shared_executions": len(self._shared), "admission": self.admission.status()}

__all__ = ["AdmissionDecision", "AdmissionJob", "AdmissionRejected", "CapacityLimits", "FairAdmissionController", "InferenceCoordinator", "InferenceReceipt", "InferenceRequest"]