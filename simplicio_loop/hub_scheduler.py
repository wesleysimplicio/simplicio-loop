"""Fair DRR scheduler with per-client quotas for Hub jobs."""

from collections import deque
from dataclasses import dataclass
from threading import RLock
from typing import Any, Deque, Dict, List, Optional


class SchedulerError(ValueError):
    """Raised for invalid scheduler operations."""


@dataclass(frozen=True)
class ScheduledJob:
    task_id: str
    client_id: str
    weight: int = 1
    cost: int = 1


class FairScheduler:
    """Deficit round-robin scheduler with bounded client inflight quotas."""

    def __init__(self, *, max_inflight_per_client: int = 4, quantum: int = 1) -> None:
        if max_inflight_per_client < 1 or quantum < 1:
            raise SchedulerError("scheduler limits must be positive")
        self.max_inflight_per_client = max_inflight_per_client
        self.quantum = quantum
        self._queues: Dict[str, Deque[ScheduledJob]] = {}
        self._weights: Dict[str, int] = {}
        self._deficit: Dict[str, int] = {}
        self._inflight: Dict[str, int] = {}
        self._order: List[str] = []
        self._cursor = 0
        self._jobs: Dict[str, ScheduledJob] = {}
        self._lock = RLock()
        self._starvation_preventions = 0

    def enqueue(self, job: ScheduledJob) -> None:
        if not job.task_id or not job.client_id or job.weight < 1 or job.cost < 1:
            raise SchedulerError("job identity, weight and cost must be positive")
        with self._lock:
            if job.task_id in self._jobs:
                raise SchedulerError("duplicate task_id")
            self._jobs[job.task_id] = job
            if job.client_id not in self._queues:
                self._queues[job.client_id] = deque()
                self._order.append(job.client_id)
                self._deficit[job.client_id] = 0
                self._inflight[job.client_id] = 0
            self._weights[job.client_id] = max(self._weights.get(job.client_id, 1), job.weight)
            self._queues[job.client_id].append(job)

    def next(self) -> Optional[ScheduledJob]:
        with self._lock:
            if not self._order:
                return None
            attempts = 0
            while attempts < len(self._order) * 4:
                client = self._order[self._cursor % len(self._order)]
                self._cursor = (self._cursor + 1) % len(self._order)
                attempts += 1
                queue = self._queues[client]
                if not queue:
                    continue
                self._deficit[client] += self.quantum * self._weights.get(client, 1)
                if self._inflight[client] >= self.max_inflight_per_client:
                    continue
                job = queue[0]
                if self._deficit[client] < job.cost:
                    continue
                queue.popleft()
                self._deficit[client] -= job.cost
                self._inflight[client] += 1
                if attempts > len(self._order):
                    self._starvation_preventions += 1
                return job
            return None

    def complete(self, task_id: str) -> None:
        with self._lock:
            job = self._jobs.get(task_id)
            if job is None:
                raise SchedulerError("unknown task")
            self._inflight[job.client_id] = max(0, self._inflight[job.client_id] - 1)
            self._jobs.pop(task_id, None)

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(task_id)
            if job is None:
                return False
            queue = self._queues[job.client_id]
            self._queues[job.client_id] = deque(item for item in queue if item.task_id != task_id)
            self._jobs.pop(task_id, None)
            return True

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "schema": "simplicio.hub-scheduler/v1",
                "clients": len(self._order),
                "queued": sum(len(queue) for queue in self._queues.values()),
                "inflight": dict(self._inflight),
                "deficit": dict(self._deficit),
                "starvation_preventions": self._starvation_preventions,
                "max_inflight_per_client": self.max_inflight_per_client,
            }
