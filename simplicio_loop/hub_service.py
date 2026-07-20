"""Composes the Hub's durable queue, fair scheduler and resource governor into one
service — the piece #503-#506 were each individually missing: real wiring between the
three isolated, well-tested primitives, not just each working alone.

    submit()  -> persists durably (HubRetryQueue) AND registers for fair ordering
                 (FairScheduler) in one call.
    claim()   -> fairness (FairScheduler.next()) picks the next candidate; the governor
                 (ResourceGovernor.admit()) must approve its declared resource cost
                 before the durable queue (HubRetryQueue.claim_specific()) actually
                 hands out a lease. A governor refusal re-queues the candidate (not
                 lost) and tries the NEXT fairness-eligible candidate, so one
                 over-budget client cannot block every other client's job.
    complete()/fail() -> release the resource lease and the scheduler slot together
                 with the durable outcome, so nothing leaks across the three.

Deliberately additive: does not modify HubDaemon/HubRetryQueue/FairScheduler/
ResourceGovernor's existing tested behavior (aside from the two small additive methods
on HubRetryQueue — `claim_specific`/`get_payload` — needed to let an external fairness
decision drive which task the durable queue claims). Wiring this service behind
HubDaemon's IPC/socket dispatch (so remote clients see it, not just in-process callers)
is an explicit, separate follow-up — out of scope here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from simplicio_loop.hub_governor import (
    ResourceGovernor,
    ResourceLease,
    ResourceRequest,
    ResourceThrottled,
)
from simplicio_loop.hub_queue_retry import HubRetryQueue, RetryLease
from simplicio_loop.hub_scheduler import FairScheduler, ScheduledJob, SchedulerError


class HubServiceError(RuntimeError):
    """Base error for the composed Hub service."""


@dataclass(frozen=True)
class ClaimedJob:
    task_id: str
    client_id: str
    workspace_id: str
    weight: int
    cost: int
    payload: Dict[str, Any]
    retry_lease: RetryLease
    resource_lease: ResourceLease


class HubService:
    """The composed submit/claim/complete/fail flow across queue + scheduler + governor."""

    def __init__(
        self, queue: HubRetryQueue, scheduler: FairScheduler, governor: ResourceGovernor
    ) -> None:
        self.queue = queue
        self.scheduler = scheduler
        self.governor = governor

    def submit(
        self,
        payload: Dict[str, Any],
        *,
        idempotency_key: str,
        client_id: str,
        workspace_id: str = "default",
        weight: int = 1,
        cost: int = 1,
        max_attempts: int = 3,
    ) -> str:
        task_id = self.queue.submit(
            payload, idempotency_key=idempotency_key, max_attempts=max_attempts,
            client_id=client_id, workspace_id=workspace_id, weight=weight, cost=cost,
        )
        try:
            self.scheduler.enqueue(
                ScheduledJob(
                    task_id=task_id, client_id=client_id, weight=weight, cost=cost,
                    workspace_id=workspace_id,
                )
            )
        except SchedulerError as exc:
            if "duplicate task_id" not in str(exc):
                raise
            # submit() is idempotent: a retried call with the same idempotency_key
            # returns the SAME task_id, which is already scheduled from the first
            # call — not a real scheduling error.
        return task_id

    def _capacity_snapshot(self, client_id: str, workspace_id: str) -> Dict[str, Any]:
        """Sanitized observation only: no reservation and no third-party identities."""
        scheduler = self.scheduler.status()
        governor = self.governor.status()
        circuit = governor.get("circuit") or {}
        zero_resources = {name: 0 for name in governor["used"]}
        return {
            "schema": "simplicio.hub-capacity-observation/v1",
            "reservation": False,
            "fresh_snapshot_required_at_activation": True,
            "scheduler": {
                "limits": dict(scheduler.get("limits") or {}),
                "global": {
                    "queued": int(scheduler.get("queued", 0)),
                    "global_total": int(scheduler.get("global_total", 0)),
                    "clients": int(scheduler.get("clients", 0)),
                },
                "target_client": {
                    "total": int((scheduler.get("client_total") or {}).get(client_id, 0)),
                    "inflight": int((scheduler.get("inflight") or {}).get(client_id, 0)),
                },
                "target_workspace": {
                    "total": int((scheduler.get("workspace_total") or {}).get(workspace_id, 0)),
                },
            },
            "governor": {
                "limits": dict(governor.get("limits") or {}),
                "used": dict(governor.get("used") or {}),
                "target_client_used": dict(
                    (governor.get("client_used") or {}).get(client_id, zero_resources)
                ),
                "draining": bool(governor.get("draining")),
                "circuit": {
                    "state": str(circuit.get("state") or "closed"),
                    "failures": int(circuit.get("failures", 0)),
                    "threshold": int(circuit.get("threshold", 1)),
                    "cooldown_seconds": float(circuit.get("cooldown_seconds", 0.0)),
                },
            },
        }

    def admit_held(
        self,
        job: Dict[str, Any],
        *,
        idempotency_key: str,
        input_digest: str,
        client_id: str,
        workspace_id: str = "default",
        weight: int = 1,
        cost: int = 1,
    ) -> Dict[str, Any]:
        snapshot = self._capacity_snapshot(client_id, workspace_id)
        return self.queue.admit_held(
            job, idempotency_key=idempotency_key, input_digest=input_digest,
            client_id=client_id, workspace_id=workspace_id, weight=weight, cost=cost,
            capacity_snapshot=snapshot,
        )

    def admission(self, *, task_id: str = "", idempotency_key: str = "") -> Dict[str, Any]:
        return self.queue.admission(task_id=task_id, idempotency_key=idempotency_key)

    def claim(
        self,
        worker_id: str,
        request: ResourceRequest,
        *,
        ttl: float = 30.0,
        max_candidates: int = 8,
    ) -> Optional[ClaimedJob]:
        tried = 0
        while tried < max_candidates:
            job = self.scheduler.next()
            if job is None:
                return None
            tried += 1
            try:
                resource_lease = self.governor.admit(
                    job.client_id, job.task_id, request, queue=job.workspace_id
                )
            except ResourceThrottled:
                # Not lost: release the scheduler's "inflight" slot and re-queue so a
                # LATER call can retry it, then try the next fairness-eligible
                # candidate this call so other clients still make progress now.
                self.scheduler.complete(job.task_id)
                self.scheduler.enqueue(job)
                continue
            retry_lease = self.queue.claim_specific(job.task_id, worker_id, ttl=ttl)
            if retry_lease is None:
                # Raced with something else between submit() and here (or the task
                # was already terminal) - release cleanly, try the next candidate.
                self.governor.release(resource_lease)
                self.scheduler.complete(job.task_id)
                continue
            payload = self.queue.get_payload(job.task_id)
            return ClaimedJob(
                task_id=job.task_id, client_id=job.client_id, workspace_id=job.workspace_id,
                weight=job.weight, cost=job.cost, payload=payload,
                retry_lease=retry_lease, resource_lease=resource_lease,
            )
        return None

    def complete(self, claimed: ClaimedJob) -> None:
        self.queue.complete(claimed.retry_lease)
        self.governor.release(claimed.resource_lease)
        self.scheduler.complete(claimed.task_id)

    def fail(self, claimed: ClaimedJob, *, error_code: str, backoff: float = 0.0) -> str:
        outcome = self.queue.fail(claimed.retry_lease, error_code=error_code, backoff=backoff)
        self.governor.release(claimed.resource_lease)
        self.scheduler.complete(claimed.task_id)
        if outcome == "retry":
            self.scheduler.enqueue(
                ScheduledJob(
                    task_id=claimed.task_id, client_id=claimed.client_id,
                    weight=claimed.weight, cost=claimed.cost,
                    workspace_id=claimed.workspace_id,
                )
            )
        return outcome

    def status(self) -> Dict[str, Any]:
        return {
            "schema": "simplicio.hub-service/v1",
            "scheduler": self.scheduler.status(),
            "governor": self.governor.status(),
        }

    def rehydrate_scheduler(self) -> int:
        """#503-506 restart persistence: re-enqueue every still-durably-queued job's
        real scheduling metadata (client_id/workspace_id/weight/cost, persisted by
        submit()) into a freshly-constructed FairScheduler after a daemon restart.
        The durable queue itself never lost these jobs; only the in-memory fairness
        bookkeeping did, until this runs. Returns how many jobs were rehydrated."""
        rehydrated = 0
        for entry in self.queue.list_queued_scheduling_metadata():
            try:
                self.scheduler.enqueue(ScheduledJob(
                    task_id=entry["task_id"], client_id=entry["client_id"] or "unknown",
                    weight=entry["weight"], cost=entry["cost"],
                    workspace_id=entry["workspace_id"],
                ))
                rehydrated += 1
            except SchedulerError:
                # Already present (idempotent re-run) or a quota now refuses it - skip
                # rather than crash a restart over one job's scheduling metadata.
                continue
        return rehydrated
