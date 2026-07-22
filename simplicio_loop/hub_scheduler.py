"""Fair DRR scheduler with hierarchical quotas and aging for Hub jobs."""

from collections import deque
from dataclasses import dataclass, replace
import hashlib
from threading import RLock
from typing import Any, Deque, Dict, List, Optional


PRIORITY_GAIN_MULTIPLIER: Dict[str, float] = {
    "interactive": 8.0,
    "mapping": 4.0,
    "llm": 2.0,
    "test": 1.5,
    "build": 1.0,
    "background": 1.0,
    "maintenance": 0.5,
}
DEFAULT_PRIORITY = "background"
SCHEDULER_MODES = frozenset(("off", "shadow", "canary", "on"))
LEGACY_POLICY_VERSION = "fifo-v1"
FAIR_POLICY_VERSION = "fair-drr-v2"


class SchedulerError(ValueError):
    """Raised for invalid scheduler operations."""


class QuotaExceededError(SchedulerError):
    """Raised on enqueue when a global/workspace/client quota is exceeded.

    Carries structured backpressure detail so the caller can react (retry
    later, shed load, surface a 429) instead of the job being silently
    dropped.
    """

    def __init__(self, scope: str, limit: int, current: int, client_id: str, workspace_id: str) -> None:
        self.scope = scope
        self.limit = limit
        self.current = current
        self.client_id = client_id
        self.workspace_id = workspace_id
        super().__init__(
            f"quota exceeded at scope={scope} limit={limit} current={current} "
            f"client={client_id} workspace={workspace_id}"
        )

    def to_backpressure_signal(self) -> Dict[str, Any]:
        return {
            "schema": "simplicio.hub-scheduler.backpressure/v1",
            "scope": self.scope,
            "limit": self.limit,
            "current": self.current,
            "client_id": self.client_id,
            "workspace_id": self.workspace_id,
        }


# Backward-compatible alias for callers that used the pre-merge name.
PRIORITY_BOOST = PRIORITY_GAIN_MULTIPLIER


@dataclass(frozen=True)
class ScheduledJob:
    task_id: str
    client_id: str
    weight: int = 1
    cost: int = 1
    workspace_id: str = "default"
    priority: str = DEFAULT_PRIORITY
    scheduler_policy: str = FAIR_POLICY_VERSION


@dataclass(frozen=True)
class SchedulerPolicy:
    """Immutable rollout manifest for a single-authority scheduler transition."""

    mode: str = "on"
    version: str = FAIR_POLICY_VERSION
    previous_version: str = LEGACY_POLICY_VERSION
    canary_percent: int = 0

    def __post_init__(self) -> None:
        if self.mode not in SCHEDULER_MODES:
            raise SchedulerError("scheduler policy mode must be off|shadow|canary|on")
        if not self.version or not self.previous_version or not 0 <= self.canary_percent <= 100:
            raise SchedulerError("scheduler policy versions and canary_percent are invalid")
        if self.mode == "canary" and self.canary_percent in (0, 100):
            raise SchedulerError("canary mode requires canary_percent between 1 and 99")

    def policy_for(self, task_id: str) -> str:
        if self.mode in ("off", "shadow"):
            return self.previous_version
        if self.mode == "on":
            return self.version
        bucket = int(hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:8], 16) % 100
        return self.version if bucket < self.canary_percent else self.previous_version

    def to_manifest(self) -> Dict[str, Any]:
        return {
            "schema": "simplicio.hub-scheduler-policy/v1", "mode": self.mode,
            "version": self.version, "previous_version": self.previous_version,
            "canary_percent": self.canary_percent,
        }


class FairScheduler:
    """Deficit round-robin scheduler with hierarchical quotas and aging."""

    def __init__(
        self,
        *,
        max_inflight_per_client: int = 4,
        quantum: int = 1,
        max_queue_per_client: Optional[int] = None,
        max_queue_per_workspace: Optional[int] = None,
        max_global_queue: Optional[int] = None,
        aging_ticks: int = 20,
        aging_boost: int = 4,
        policy: Optional[SchedulerPolicy] = None,
    ) -> None:
        if max_inflight_per_client < 1 or quantum < 1:
            raise SchedulerError("scheduler limits must be positive")
        if aging_ticks < 1 or aging_boost < 1:
            raise SchedulerError("aging parameters must be positive")
        for limit in (max_queue_per_client, max_queue_per_workspace, max_global_queue):
            if limit is not None and limit < 1:
                raise SchedulerError("quota limits must be positive")
        self.max_inflight_per_client = max_inflight_per_client
        self.quantum = quantum
        self.max_queue_per_client = max_queue_per_client
        self.max_queue_per_workspace = max_queue_per_workspace
        self.max_global_queue = max_global_queue
        self.aging_ticks = aging_ticks
        self.aging_boost = aging_boost
        self.policy = policy or SchedulerPolicy()
        self._decision_receipts: Deque[Dict[str, Any]] = deque(maxlen=100)
        self._queues: Dict[str, Deque[ScheduledJob]] = {}
        self._weights: Dict[str, int] = {}
        self._deficit: Dict[str, float] = {}
        self._inflight: Dict[str, int] = {}
        self._order: List[str] = []
        self._cursor = 0
        self._jobs: Dict[str, ScheduledJob] = {}
        self._lock = RLock()
        self._starvation_preventions = 0
        self._client_total: Dict[str, int] = {}
        self._workspace_total: Dict[str, int] = {}
        self._global_total = 0
        self._tick = 0
        self._last_served_tick: Dict[str, int] = {}
        self._served_total: Dict[str, int] = {}

    def _check_quotas(self, job: ScheduledJob) -> None:
        if self.max_global_queue is not None and self._global_total >= self.max_global_queue:
            raise QuotaExceededError(
                "global", self.max_global_queue, self._global_total, job.client_id, job.workspace_id
            )
        if self.max_queue_per_workspace is not None:
            current = self._workspace_total.get(job.workspace_id, 0)
            if current >= self.max_queue_per_workspace:
                raise QuotaExceededError(
                    "workspace", self.max_queue_per_workspace, current, job.client_id, job.workspace_id
                )
        if self.max_queue_per_client is not None:
            current = self._client_total.get(job.client_id, 0)
            if current >= self.max_queue_per_client:
                raise QuotaExceededError(
                    "client", self.max_queue_per_client, current, job.client_id, job.workspace_id
                )

    def enqueue(self, job: ScheduledJob) -> None:
        if not job.task_id or not job.client_id or job.weight < 1 or job.cost < 1:
            raise SchedulerError("job identity, weight and cost must be positive")
        if not job.workspace_id:
            raise SchedulerError("workspace_id must be non-empty")
        if job.priority not in PRIORITY_GAIN_MULTIPLIER:
            raise SchedulerError(
                f"unknown priority class {job.priority!r}; must be one of "
                + ", ".join(sorted(PRIORITY_GAIN_MULTIPLIER))
            )
        with self._lock:
            if job.scheduler_policy == FAIR_POLICY_VERSION:
                job = replace(job, scheduler_policy=self.policy.policy_for(job.task_id))
            if job.task_id in self._jobs:
                raise SchedulerError("duplicate task_id")
            self._check_quotas(job)
            self._jobs[job.task_id] = job
            if job.client_id not in self._queues:
                self._queues[job.client_id] = deque()
                self._order.append(job.client_id)
                self._deficit[job.client_id] = 0
                self._inflight[job.client_id] = 0
                self._last_served_tick[job.client_id] = self._tick
            self._weights[job.client_id] = max(self._weights.get(job.client_id, 1), job.weight)
            self._queues[job.client_id].append(job)
            self._client_total[job.client_id] = self._client_total.get(job.client_id, 0) + 1
            self._workspace_total[job.workspace_id] = self._workspace_total.get(job.workspace_id, 0) + 1
            self._global_total += 1

    def _release_slot(self, job: ScheduledJob) -> None:
        self._client_total[job.client_id] = max(0, self._client_total.get(job.client_id, 0) - 1)
        self._workspace_total[job.workspace_id] = max(0, self._workspace_total.get(job.workspace_id, 0) - 1)
        self._global_total = max(0, self._global_total - 1)

    def next(self) -> Optional[ScheduledJob]:
        with self._lock:
            if not self._order:
                return None
            if self.policy.mode in ("off", "shadow"):
                candidate = self._fair_candidate() if self.policy.mode == "shadow" else None
                authority = self._next_fifo()
                if self.policy.mode == "shadow":
                    self._decision_receipts.append({
                        "schema": "simplicio.hub-scheduler-shadow-receipt/v1",
                        "authority": authority.task_id if authority else None,
                        "candidate": candidate.task_id if candidate else None,
                        "dispatched": 1 if authority else 0,
                    })
                return authority
            self._tick += 1
            attempts = 0
            while attempts < len(self._order) * 4:
                client = self._order[self._cursor % len(self._order)]
                self._cursor = (self._cursor + 1) % len(self._order)
                attempts += 1
                queue = self._queues[client]
                if not queue:
                    continue
                job = queue[0]
                gain = self.quantum * self._weights.get(client, 1) * PRIORITY_GAIN_MULTIPLIER[job.priority]
                waited = self._tick - self._last_served_tick.get(client, self._tick)
                if waited > self.aging_ticks:
                    gain *= self.aging_boost
                    self._starvation_preventions += 1
                self._deficit[client] += gain
                if self._inflight[client] >= self.max_inflight_per_client:
                    continue
                if self._deficit[client] < job.cost:
                    continue
                queue.popleft()
                self._deficit[client] -= job.cost
                self._inflight[client] += 1
                self._last_served_tick[client] = self._tick
                self._served_total[client] = self._served_total.get(client, 0) + 1
                return job
            return None

    def _next_fifo(self) -> Optional[ScheduledJob]:
        """Legacy authority used by off/shadow; consumes exactly one job."""
        self._tick += 1
        for offset in range(len(self._order)):
            index = (self._cursor + offset) % len(self._order)
            client = self._order[index]
            if not self._queues[client] or self._inflight[client] >= self.max_inflight_per_client:
                continue
            job = self._queues[client].popleft()
            self._cursor = (index + 1) % len(self._order)
            self._inflight[client] += 1
            self._last_served_tick[client] = self._tick
            self._served_total[client] = self._served_total.get(client, 0) + 1
            return job
        return None

    def _fair_candidate(self) -> Optional[ScheduledJob]:
        """Non-mutating shadow projection; it can never become a dispatch authority."""
        eligible = []
        for order, client in enumerate(self._order):
            if not self._queues[client] or self._inflight[client] >= self.max_inflight_per_client:
                continue
            job = self._queues[client][0]
            gain = self.quantum * self._weights.get(client, 1) * PRIORITY_GAIN_MULTIPLIER[job.priority]
            eligible.append((self._deficit.get(client, 0) + gain - job.cost, -order, job))
        return max(eligible, key=lambda item: (item[0], item[1]))[2] if eligible else None

    def preview_next(self) -> Optional[ScheduledJob]:
        """Return the next decision without dispatching or changing scheduler state."""
        with self._lock:
            queues = {key: deque(value) for key, value in self._queues.items()}
            deficit, inflight = dict(self._deficit), dict(self._inflight)
            cursor, tick = self._cursor, self._tick
            last, served = dict(self._last_served_tick), dict(self._served_total)
            preventions = self._starvation_preventions
            try:
                return self.next()
            finally:
                self._queues, self._deficit, self._inflight = queues, deficit, inflight
                self._cursor, self._tick = cursor, tick
                self._last_served_tick, self._served_total = last, served
                self._starvation_preventions = preventions

    def complete(self, task_id: str) -> None:
        with self._lock:
            job = self._jobs.get(task_id)
            if job is None:
                raise SchedulerError("unknown task")
            self._inflight[job.client_id] = max(0, self._inflight[job.client_id] - 1)
            self._release_slot(job)
            self._jobs.pop(task_id, None)

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(task_id)
            if job is None:
                return False
            queue = self._queues[job.client_id]
            self._release_slot(job)
            self._queues[job.client_id] = deque(item for item in queue if item.task_id != task_id)
            self._jobs.pop(task_id, None)
            return True

    def _jains_fairness_index(self) -> float:
        served = [self._served_total.get(client, 0) for client in self._order]
        if not served:
            return 1.0
        total = sum(served)
        total_sq = sum(value * value for value in served)
        if total_sq == 0:
            return 1.0
        return (total * total) / (len(served) * total_sq)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "schema": "simplicio.hub-scheduler/v2",
                "clients": len(self._order),
                "queued": sum(len(queue) for queue in self._queues.values()),
                "inflight": dict(self._inflight),
                "deficit": dict(self._deficit),
                "starvation_preventions": self._starvation_preventions,
                "max_inflight_per_client": self.max_inflight_per_client,
                "limits": {
                    "max_inflight_per_client": self.max_inflight_per_client,
                    "max_queue_per_client": self.max_queue_per_client,
                    "max_queue_per_workspace": self.max_queue_per_workspace,
                    "max_global_queue": self.max_global_queue,
                    "quantum": self.quantum,
                    "aging_ticks": self.aging_ticks,
                    "aging_boost": self.aging_boost,
                },
                "client_total": dict(self._client_total),
                "workspace_total": dict(self._workspace_total),
                "global_total": self._global_total,
                "tick": self._tick,
                "served_total": dict(self._served_total),
                "jains_fairness_index": self._jains_fairness_index(),
                "policy": self.policy.to_manifest(),
                "decision_receipts": list(self._decision_receipts),
            }

    def configure_policy(self, policy: SchedulerPolicy) -> Dict[str, Any]:
        """Atomically roll forward/back; already queued jobs retain their version pin."""
        with self._lock:
            previous = self.policy
            self.policy = policy
            receipt = {
                "schema": "simplicio.hub-scheduler-rollout-receipt/v1",
                "from": previous.to_manifest(), "to": policy.to_manifest(),
                "queued_jobs_rewritten": 0, "dispatch_authorities": 1,
            }
            self._decision_receipts.append(receipt)
            return receipt
