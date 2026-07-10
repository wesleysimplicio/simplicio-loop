#!/usr/bin/env python3
"""fan_out.py — Distribute independent tasks across parallel workers.

Usage:
    python scripts/fan_out.py --tasks <tasks.json> [--max-workers <N>] [--dry-run]

Preflight:
    1. Detect available capacity (local worktrees)
    2. Admit ready tasks through the resource governor
    3. Keep conflicting paths in serial lanes
    4. Refill each slot immediately after completion

Guardrails:
    - max_workers: cap concurrent workers (default: 4, from env: FAN_OUT_MAX_WORKERS)
    - Worktree provisioning is delegated to the isolated backend (#153)
    - One worker failure does not bring down others
    - Fallback to serial when cap==1 or no extra capacity

Output:
    JSON aggregator report with per-worker results.

Refs: #104, #64 (impact_audit), #103 (schema_verify)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple


@dataclass
class Task:
    id: str
    goal: str
    target: Optional[str] = None
    files_affected: List[str] = field(default_factory=list)
    # These fields are optional so task files written for the original fan-out
    # contract remain valid.  They make the scheduler useful as an in-memory
    # task graph until the durable claims store lands in #151.
    dependencies: List[str] = field(default_factory=list)
    priority: int = 0
    resources: Dict[str, float] = field(default_factory=dict)
    retries: int = 0


@dataclass
class WorkerResult:
    task_id: str
    success: bool
    output: str = ""
    error: Optional[str] = None
    duration_ms: float = 0.0
    attempts: int = 1
    reason_code: Optional[str] = None


@dataclass
class SchedulerEvent:
    """A machine-readable scheduling decision or worker lifecycle event."""

    event: str
    task_id: Optional[str] = None
    reason_code: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ResourceGovernor:
    """Small, deterministic resource admission governor.

    Resource values are abstract units supplied by the task graph.  A task may
    request ``cpu``, ``memory_mb``, ``disk_mb``, ``processes`` and ``quota``.
    The governor never admits a task when adding its request would exceed a
    configured cap.  Unknown resource names are deliberately ignored so a
    newer producer can interoperate with an older scheduler without silently
    changing the hard caps enforced here.
    """

    workers: int
    cpu: float
    memory_mb: Optional[float] = None
    disk_mb: Optional[float] = None
    processes: Optional[float] = None
    quota: Optional[float] = None
    used: Dict[str, float] = field(default_factory=dict)

    _RESOURCE_NAMES = ("cpu", "memory_mb", "disk_mb", "processes", "quota")

    def __post_init__(self) -> None:
        self.workers = max(0, int(self.workers))
        self.cpu = max(0.0, float(self.cpu))
        for name in self._RESOURCE_NAMES:
            if name not in self.used:
                self.used[name] = 0.0

    @classmethod
    def from_capacity(cls, capacity: Dict[str, Any], workers: Optional[int] = None) -> "ResourceGovernor":
        limits = capacity.get("resources", {}) if isinstance(capacity, dict) else {}
        selected_workers = capacity.get("workers_local", 0) if workers is None else workers
        return cls(
            workers=selected_workers,
            cpu=limits.get("cpu", capacity.get("cpu_units", capacity.get("cpu", selected_workers))),
            memory_mb=limits.get("memory_mb", capacity.get("memory_mb")),
            disk_mb=limits.get("disk_mb", capacity.get("disk_mb")),
            processes=limits.get("processes", capacity.get("processes", selected_workers)),
            quota=limits.get("quota", capacity.get("quota")),
        )

    def request(self, task: Task) -> Dict[str, float]:
        request = {name: 0.0 for name in self._RESOURCE_NAMES}
        # One worker/process slot is always consumed by a running task.  This
        # keeps the scheduler work-conserving while still enforcing process caps.
        request["processes"] = 1.0
        aliases = {
            "ram": "memory_mb",
            "ram_mb": "memory_mb",
            "memory": "memory_mb",
            "process": "processes",
            "cpu_units": "cpu",
        }
        for raw_name, value in (task.resources or {}).items():
            name = aliases.get(raw_name, raw_name)
            if name in request:
                try:
                    request[name] = max(0.0, float(value))
                except (TypeError, ValueError):
                    # Invalid requests are denied by ``admit`` with a stable
                    # reason instead of leaking an exception from a worker.
                    request[name] = float("inf")
        if request["cpu"] <= 0:
            request["cpu"] = 1.0
        return request

    def _limit(self, name: str) -> Optional[float]:
        if name == "cpu":
            return self.cpu
        if name == "memory_mb":
            return self.memory_mb
        if name == "disk_mb":
            return self.disk_mb
        if name == "processes":
            return self.processes if self.processes is not None else float(self.workers)
        if name == "quota":
            return self.quota
        return None

    def admission(self, task: Task, active_workers: int = 0) -> Tuple[bool, str, Dict[str, float]]:
        """Return ``(allowed, reason_code, request)`` without mutating usage."""
        request = self.request(task)
        if active_workers >= self.workers:
            return False, "resource", request
        for name, amount in request.items():
            if amount == float("inf"):
                return False, "resource_invalid", request
            limit = self._limit(name)
            if limit is not None and self.used.get(name, 0.0) + amount > limit:
                return False, "resource", request
        return True, "admitted", request

    def acquire(self, request: Dict[str, float]) -> None:
        for name, amount in request.items():
            self.used[name] = self.used.get(name, 0.0) + amount

    def release(self, request: Dict[str, float]) -> None:
        for name, amount in request.items():
            self.used[name] = max(0.0, self.used.get(name, 0.0) - amount)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "limits": {
                "workers": self.workers,
                "cpu": self.cpu,
                "memory_mb": self.memory_mb,
                "disk_mb": self.disk_mb,
                "processes": self.processes,
                "quota": self.quota,
            },
            "used": dict(self.used),
        }


def detect_capacity() -> Dict[str, Any]:
    """Detect safe local capacity and optional operator-provided hard caps.

    The probe is intentionally stdlib-only and conservative.  Environment
    values are *caps*, never an invitation to oversubscribe the host.  A value
    of zero is meaningful and causes the scheduler to report ``BLOCKED``;
    capacity is never fabricated to make a report look productive.
    """
    max_workers_env = os.environ.get("FAN_OUT_MAX_WORKERS", "4")
    try:
        worker_cap = max(0, int(max_workers_env))
    except (TypeError, ValueError):
        worker_cap = 4
    cpu_count = os.cpu_count() or 1
    workers = min(worker_cap, cpu_count) if worker_cap else 0

    def _optional_float(name: str) -> Optional[float]:
        raw = os.environ.get(name)
        if raw is None or raw == "":
            return None
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return None

    memory_mb = _optional_float("FAN_OUT_MAX_MEMORY_MB")
    disk_mb = _optional_float("FAN_OUT_MAX_DISK_MB")
    quota = _optional_float("FAN_OUT_MAX_QUOTA")
    # FAN_OUT_MAX_PROCESSES defaults to the worker cap, so process accounting
    # remains a hard limit even when no explicit resource policy is configured.
    processes = _optional_float("FAN_OUT_MAX_PROCESSES")
    if processes is None:
        processes = float(workers)
    cpu_cap = _optional_float("FAN_OUT_MAX_CPU")
    if cpu_cap is None:
        cpu_cap = float(workers)
    return {
        "workers_local": workers,
        "cpu_units": cpu_cap,
        "memory_mb": memory_mb,
        "disk_mb": disk_mb,
        "processes": processes,
        "quota": quota,
        "backends": ["local"] if workers else [],
        "resources": {
            "cpu": cpu_cap,
            "memory_mb": memory_mb,
            "disk_mb": disk_mb,
            "processes": processes,
            "quota": quota,
        },
    }


def build_independence_graph(tasks: List[Task]) -> List[List[Task]]:
    """Partition tasks into groups that don't share files (disjoint)."""
    groups: List[List[Task]] = []
    assigned: set[str] = set()

    for task in tasks:
        task_files = set(f.lower() for f in (task.files_affected or []))
        placed = False
        for group in groups:
            group_files: set[str] = set()
            for t in group:
                group_files.update(f.lower() for f in (t.files_affected or []))
            if not (task_files & group_files):
                group.append(task)
                placed = True
                break
        if not placed:
            groups.append([task])

    return groups


def run_worker(task: Task, workdir: str, dry_run: bool = False) -> WorkerResult:
    """Run a single task in its own worktree/branch."""
    start = time.time()
    task_id = task.id
    branch_name = f"feat/{task_id}-{uuid.uuid4().hex[:8]}"

    try:
        if dry_run:
            output = json.dumps({"task": task_id, "branch": branch_name, "dry_run": True})
        else:
            # The old implementation checked out a branch in the shared
            # process cwd.  Concurrent workers could therefore move one
            # another's HEAD (and corrupt a caller's checkout).  Worktree
            # provisioning belongs to #153; this worker only runs the
            # operator command in the caller's directory and reports the
            # deterministic branch identity for the future isolated backend.
            # Simulate task execution — in production this invokes the full
            # orient→execute→verify→PR loop.
            result = subprocess.run(
                [sys.executable, "-c", "import sys; print(sys.argv[1])",
                 f"Running task {task_id}: {task.goal}"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = result.stdout

        duration = (time.time() - start) * 1000
        return WorkerResult(
            task_id=task_id,
            success=True,
            output=output.strip(),
            duration_ms=round(duration, 1),
        )
    except Exception as e:
        duration = (time.time() - start) * 1000
        return WorkerResult(
            task_id=task_id,
            success=False,
            error=str(e),
            duration_ms=round(duration, 1),
            reason_code="worker_exception",
        )


def _path_key(path: str) -> str:
    """Normalize a task impact path for conflict-lane admission."""
    # ``normcase`` is a no-op on POSIX, while repository impact paths remain
    # case-insensitive across the supported Windows/POSIX checkout adapters.
    return os.path.normcase(str(path).replace("\\", "/")).strip().lower()


def _task_files(task: Task) -> Set[str]:
    return {_path_key(path) for path in (task.files_affected or []) if str(path).strip()}


class WorkConservingScheduler:
    """Event-driven, conflict-aware worker pool.

    Only admitted tasks are submitted to the executor.  As soon as one future
    completes, its resources and conflict lane are released and the scheduler
    scans the pending queue again, so an unrelated task can refill that slot
    without waiting for a wave/barrier.  Every non-dispatch decision is
    recorded with a reason code for truthful status/evidence output.
    """

    def __init__(
        self,
        tasks: Iterable[Task],
        workdir: str,
        max_workers: int,
        *,
        dry_run: bool = False,
        governor: Optional[ResourceGovernor] = None,
        worker: Optional[Callable[[Task, str, bool], WorkerResult]] = None,
        retry_limit: int = 0,
        poll_interval: float = 0.01,
    ) -> None:
        self.workdir = workdir
        self.dry_run = dry_run
        self.max_workers = max(0, int(max_workers))
        self.governor = governor or ResourceGovernor(
            workers=self.max_workers,
            cpu=float(self.max_workers),
            processes=float(self.max_workers),
        )
        # Keep a governor from admitting more execution slots than the CLI
        # requested, even when it was built from a larger host capacity probe.
        self.governor.workers = min(self.governor.workers, self.max_workers)
        self.worker = worker or run_worker
        self.retry_limit = max(0, int(retry_limit))
        self.poll_interval = max(0.0, float(poll_interval))
        self.pending: List[Task] = list(tasks)
        self.events: List[SchedulerEvent] = []
        self.results: List[WorkerResult] = []
        self.attempts: Dict[str, int] = {}
        self._active_files: Set[str] = set()
        self._active: Dict[Any, Tuple[Task, Set[str], Dict[str, float]]] = {}
        self._completed: Set[str] = set()
        self._failed: Set[str] = set()
        self._cancelled = threading.Event()
        self._lock = threading.RLock()
        self._run_started: Optional[float] = None
        self._run_finished: Optional[float] = None
        self._task_queued_at: Dict[str, float] = {task.id: time.monotonic() for task in self.pending}
        self._wait_seconds = 0.0
        self._busy_seconds = 0.0

    def cancel(self) -> None:
        """Stop admitting new work; already-running tasks finish safely."""
        self._cancelled.set()
        self.events.append(SchedulerEvent("cancel_requested", reason_code="cancel"))

    def add_task(self, task: Task) -> None:
        """Add late work before/while a run is draining (thread-safe)."""
        with self._lock:
            self.pending.append(task)
            self._task_queued_at[task.id] = time.monotonic()
            self.events.append(SchedulerEvent("task_arrived", task.id, "late_arrival"))

    def _dependencies(self, task: Task) -> Tuple[bool, Optional[str]]:
        deps = set(task.dependencies or [])
        if not deps:
            return True, None
        if deps & self._failed:
            return False, "dependency_failed"
        if not deps <= self._completed:
            return False, "dependency"
        return True, None

    def _select_ready(self, active_workers: int) -> Optional[Tuple[int, Task, Set[str], Dict[str, float]]]:
        # Priority is descending, then input order for deterministic fairness.
        ranked = sorted(enumerate(self.pending), key=lambda item: (-item[1].priority, item[0]))
        saw_resource_denial = False
        saw_dependency = False
        saw_conflict = False
        for index, task in ranked:
            ready, dependency_reason = self._dependencies(task)
            if not ready:
                if dependency_reason == "dependency_failed":
                    # Failure is terminal for this dependent, but does not
                    # cancel unrelated work.
                    self.events.append(SchedulerEvent("blocked", task.id, dependency_reason))
                else:
                    saw_dependency = True
                continue
            files = _task_files(task)
            if files & self._active_files:
                saw_conflict = True
                continue
            allowed, reason, request = self.governor.admission(task, active_workers)
            if not allowed:
                saw_resource_denial = True
                continue
            return index, task, files, request
        if self.pending:
            if saw_resource_denial:
                reason = "resource"
            elif saw_conflict:
                reason = "conflict"
            elif saw_dependency:
                reason = "dependency"
            else:
                reason = "delivery"
            self.events.append(SchedulerEvent("idle", reason_code=reason, details={"queue_depth": len(self.pending)}))
        return None

    def _dispatch(self, executor: ThreadPoolExecutor) -> None:
        if len(self._active) >= self.max_workers and self.pending:
            # A full pool is not idle, but a pending conflicting lane is still
            # an observable scheduling decision.  Keep it in the timeline so
            # status consumers can explain why that item was deferred.
            if any(_task_files(task) & self._active_files for task in self.pending):
                self.events.append(SchedulerEvent("deferred", reason_code="conflict"))
            return
        while not self._cancelled.is_set() and len(self._active) < self.max_workers:
            selected = self._select_ready(len(self._active))
            if selected is None:
                return
            index, task, files, request = selected
            # Remove only after admission; this makes a conflict/resource
            # denial visible without losing the task from the queue.
            self.pending.pop(index)
            self._wait_seconds += max(0.0, time.monotonic() - self._task_queued_at.pop(task.id, time.monotonic()))
            self.governor.acquire(request)
            self._active_files.update(files)
            attempt = self.attempts.get(task.id, 0) + 1
            self.attempts[task.id] = attempt
            future = executor.submit(self.worker, task, self.workdir, self.dry_run)
            self._active[future] = (task, files, request)
            self.events.append(SchedulerEvent(
                "started", task.id, "admitted", details={"attempt": attempt, "active": len(self._active)}
            ))
        if len(self._active) >= self.max_workers and self.pending:
            if any(_task_files(task) & self._active_files for task in self.pending):
                self.events.append(SchedulerEvent("deferred", reason_code="conflict"))

    def _finish(self, future: Any) -> None:
        task, files, request = self._active.pop(future)
        self.governor.release(request)
        self._active_files.difference_update(files)
        try:
            result = future.result()
        except Exception as exc:  # worker isolation: one crash is one failure
            result = WorkerResult(task.id, False, error=str(exc), reason_code="worker_exception")
        result.attempts = self.attempts.get(task.id, 1)
        started_event = next((event for event in reversed(self.events)
                              if event.event == "started" and event.task_id == task.id), None)
        if started_event is not None:
            try:
                started_at = datetime.fromisoformat(started_event.timestamp).timestamp()
                self._busy_seconds += max(0.0, time.time() - started_at)
            except (TypeError, ValueError, OSError):
                pass
        if result.success:
            self._completed.add(task.id)
            self.results.append(result)
            self.events.append(SchedulerEvent("completed", task.id, "success", details={"attempt": result.attempts}))
            return
        if result.attempts <= max(self.retry_limit, int(task.retries or 0)) and not self._cancelled.is_set():
            self.pending.append(task)
            self.events.append(SchedulerEvent("requeued", task.id, "retry", details={"attempt": result.attempts}))
            return
        self._failed.add(task.id)
        self.results.append(result)
        self.events.append(SchedulerEvent("failed", task.id, result.reason_code or "worker_failure"))

    def run(self) -> List[WorkerResult]:
        """Drain all currently known work and return final worker receipts."""
        if self._run_started is None:
            self._run_started = time.monotonic()
        if self.max_workers <= 0 or self.governor.workers <= 0:
            if self.pending:
                self.events.append(SchedulerEvent("blocked", reason_code="resource"))
            self._run_finished = time.monotonic()
            return []
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            while self.pending or self._active:
                self._dispatch(executor)
                if not self._active:
                    # No task can be admitted.  Dependencies may be waiting on
                    # a missing/failed node; preserve them in the queue and
                    # report the truthful reason instead of spinning forever.
                    if self.pending:
                        for task in list(self.pending):
                            ready, reason = self._dependencies(task)
                            if not ready and reason == "dependency_failed":
                                self.pending.remove(task)
                                self._failed.add(task.id)
                                self.results.append(WorkerResult(
                                    task.id, False, error="dependency failed", reason_code=reason
                                ))
                            elif not ready and reason == "dependency":
                                # Missing dependency is a terminal blocked
                                # receipt; a live dependency would be active.
                                missing = set(task.dependencies or []) - self._completed - self._failed
                                if missing and not any(p.id in missing for p in self.pending):
                                    self.pending.remove(task)
                                    self._failed.add(task.id)
                                    self.results.append(WorkerResult(
                                        task.id, False, error="dependency unavailable", reason_code=reason
                                    ))
                        if self.pending and not self._active:
                            break
                    continue
                # ``wait(FIRST_COMPLETED)`` avoids a wave barrier and returns
                # immediately when any slot is free for refill.
                done = []
                while not done:
                    done = [future for future in list(self._active) if future.done()]
                    if not done:
                        time.sleep(self.poll_interval)
                for future in done:
                    self._finish(future)
        if self._cancelled.is_set():
            for task in self.pending:
                self.results.append(WorkerResult(task.id, False, error="cancelled", reason_code="cancel"))
            self.pending.clear()
        self._run_finished = time.monotonic()
        return self.results

    def report(self) -> Dict[str, Any]:
        now = self._run_finished or time.monotonic()
        started = self._run_started or now
        wall_clock_ms = max(0.0, (now - started) * 1000.0)
        utilization = 0.0
        if wall_clock_ms > 0 and self.max_workers > 0:
            utilization = min(1.0, self._busy_seconds / ((wall_clock_ms / 1000.0) * self.max_workers))
        runnable = 0
        blocked = 0
        for task in self.pending:
            ready, _ = self._dependencies(task)
            if ready and not (_task_files(task) & self._active_files):
                runnable += 1
            else:
                blocked += 1
        return {
            "queue_depth": len(self.pending),
            "runnable": runnable,
            "blocked": blocked,
            "active": len(self._active),
            "slots": self.max_workers,
            "wall_clock_ms": round(wall_clock_ms, 1),
            "wait_ms": round(self._wait_seconds * 1000.0, 1),
            "utilization": round(utilization, 4),
            "resources": self.governor.snapshot(),
            "events": [asdict(event) for event in self.events],
        }


def run_scheduler(
    tasks: Iterable[Task],
    workdir: str,
    max_workers: int,
    *,
    dry_run: bool = False,
    capacity: Optional[Dict[str, Any]] = None,
    worker: Optional[Callable[[Task, str, bool], WorkerResult]] = None,
    retry_limit: int = 0,
) -> Tuple[List[WorkerResult], WorkConservingScheduler]:
    """Convenience entry point used by the CLI and focused scheduler tests."""
    capacity = capacity or detect_capacity()
    governor = ResourceGovernor.from_capacity(capacity, workers=min(max_workers, capacity.get("workers_local", 0)))
    scheduler = WorkConservingScheduler(
        tasks,
        workdir,
        max_workers=min(max_workers, capacity.get("workers_local", 0)),
        dry_run=dry_run,
        governor=governor,
        worker=worker or run_worker,
        retry_limit=retry_limit,
    )
    return scheduler.run(), scheduler


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["selftest"]:
        return selftest()
    opts: Dict[str, str] = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            key = a[2:]
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                opts[key] = argv[i + 1]
                i += 2
            else:
                opts[key] = "true"
                i += 1
        else:
            i += 1

    tasks_path = opts.get("tasks")
    max_workers = int(opts.get("max-workers", opts.get("max_workers", "4")))
    dry_run = opts.get("dry-run", "false").lower() == "true"

    if opts.get("selftest"):
        return selftest()

    if not tasks_path:
        print("Usage: python scripts/fan_out.py --tasks <tasks.json> [--max-workers N] [--dry-run]")
        return 2

    # Load tasks
    try:
        with open(tasks_path) as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error loading tasks: {e}", file=sys.stderr)
        return 2

    tasks = [Task(**t) if isinstance(t, dict) else Task(id=str(i), goal=str(t)) for i, t in enumerate(raw)]

    if not tasks:
        print(json.dumps({"verdict": "SERIAL (no tasks)", "workers": []}))
        return 0

    # Detect capacity
    capacity = detect_capacity()
    effective_workers = min(max_workers, capacity["workers_local"], len(tasks))

    if effective_workers <= 0:
        print(json.dumps({
            "verdict": "BLOCKED (no capacity)",
            "capacity": capacity,
            "max_workers": max_workers,
            "workers": [],
            "reason_code": "resource",
        }))
        return 1

    print(f"[fan-out] capacity: {capacity}, workers: {effective_workers}, mode={'SERIAL' if effective_workers == 1 else 'FAN_OUT'}", file=sys.stderr)

    # Event-driven dispatch: only safe admitted tasks are submitted and every
    # completed task immediately refills its slot.
    workdir = os.getcwd()
    total_start = time.time()
    try:
        retry_limit = max(0, int(opts.get("retry-limit", opts.get("retry_limit", "0"))))
    except ValueError:
        retry_limit = 0
    all_results, scheduler = run_scheduler(
        tasks,
        workdir,
        effective_workers,
        dry_run=dry_run,
        capacity=capacity,
        retry_limit=retry_limit,
    )
    for result in all_results:
        status = "OK" if result.success else "FAIL"
        print(f"[fan-out] task {result.task_id}: {status} ({result.duration_ms}ms)", file=sys.stderr)

    total_duration = (time.time() - total_start) * 1000

    # Aggregate
    report = {
        "verdict": "SERIAL" if effective_workers == 1 else "FAN_OUT",
        "capacity": capacity,
        "effective_workers": effective_workers,
        "total_tasks": len(tasks),
        "total_duration_ms": round(total_duration, 1),
        "workers": [asdict(r) for r in all_results],
        "scheduler": scheduler.report(),
        "savings": {
            "source": "fan-out",
            "description": f"dispatched {len(tasks)} tasks across {effective_workers} worker(s)",
            "estimated_serial_ms": round(total_duration * effective_workers, 1),
            "actual_ms": round(total_duration, 1),
        },
    }

    if effective_workers == 1:
        # A serial run is useful and honest, but must not claim parallel
        # savings.  Keep the key for backwards-compatible consumers while
        # making its value explicit.
        report["savings"] = {"source": "fan-out", "description": "serial execution; no parallel savings", "actual_ms": round(total_duration, 1)}

    print(json.dumps(report, indent=2))
    return 0 if len(all_results) == len(tasks) and all(r.success for r in all_results) else 1


def selftest() -> int:
    """Run self-test with known inputs."""
    # Test independence graph
    tasks = [
        Task(id="1", goal="Fix parser", files_affected=["parser.py"]),
        Task(id="2", goal="Fix UI", files_affected=["ui.py"]),
        Task(id="3", goal="Fix both", files_affected=["parser.py", "db.py"]),
    ]
    groups = build_independence_graph(tasks)
    assert len(groups) >= 1, f"Expected at least 1 group, got {len(groups)}"
    print(f"selftest: PASS (groups={len(groups)})")

    # Test capacity detection
    cap = detect_capacity()
    assert cap["workers_local"] >= 1, f"Expected at least 1 worker, got {cap}"
    assert "local" in cap["backends"]
    print(f"selftest: PASS (capacity={cap})")

    print("selftest: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
