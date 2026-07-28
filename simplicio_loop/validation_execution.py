"""Fail-closed execution layer for adaptive validation decisions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Dict, Iterable, Mapping, Sequence, Tuple

EXECUTION_SCHEMA = "simplicio.validation-execution/v1"
CACHE_SCHEMA = "simplicio.validation-cache/v1"
REQUIRED_CONTEXT_HASHES = (
    "source_hash",
    "test_hash",
    "dependency_hash",
    "environment_hash",
    "command_hash",
    "config_hash",
    "lockfile_hash",
    "toolchain_hash",
)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ValidationTask:
    name: str
    command: Tuple[str, ...]
    tier: str = "focused"
    independent: bool = True
    resources: Tuple[str, ...] = ()
    timeout_seconds: float = 120.0
    final_gate: bool = False

    def __post_init__(self) -> None:
        if not self.name or not self.command:
            raise ValueError("validation task requires name and command")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True)
class TaskResult:
    name: str
    status: str
    returncode: int | None
    duration_ms: float
    reason_code: str
    cached: bool = False
    stdout: str = ""
    stderr: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "PASSED"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "returncode": self.returncode,
            "duration_ms": round(self.duration_ms, 3),
            "reason_code": self.reason_code,
            "cached": self.cached,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


class ValidationCache:
    """Small crash-safe JSON cache; only successful, fully-bound results are reusable."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    @staticmethod
    def validate_context(context: Mapping[str, str]) -> Tuple[bool, Tuple[str, ...]]:
        missing = tuple(key for key in REQUIRED_CONTEXT_HASHES if not context.get(key))
        return not missing, missing

    @staticmethod
    def key(task: ValidationTask, context: Mapping[str, str]) -> str:
        ready, missing = ValidationCache.validate_context(context)
        if not ready:
            raise ValueError("incomplete cache context: " + ",".join(missing))
        return _digest({
            "task": {
                "name": task.name,
                "command": task.command,
                "tier": task.tier,
                "resources": sorted(task.resources),
                "final_gate": task.final_gate,
            },
            "context": dict(sorted(context.items())),
        })

    def _read(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"schema": CACHE_SCHEMA, "entries": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"schema": CACHE_SCHEMA, "entries": {}}
        if value.get("schema") != CACHE_SCHEMA or not isinstance(value.get("entries"), dict):
            return {"schema": CACHE_SCHEMA, "entries": {}}
        return value

    def get(self, task: ValidationTask, context: Mapping[str, str]) -> TaskResult | None:
        ready, _ = self.validate_context(context)
        if not ready:
            return None
        entry = self._read()["entries"].get(self.key(task, context))
        if not entry or entry.get("status") != "PASSED":
            return None
        return TaskResult(
            name=task.name, status="PASSED", returncode=0, duration_ms=0.0,
            reason_code="CACHE_HIT", cached=True,
        )

    def put(self, task: ValidationTask, context: Mapping[str, str], result: TaskResult) -> bool:
        ready, _ = self.validate_context(context)
        if not ready or not result.passed:
            return False
        with self._lock:
            value = self._read()
            value["entries"][self.key(task, context)] = {
                "status": "PASSED",
                "context": dict(sorted(context.items())),
                "written_at_ns": time.time_ns(),
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(_canonical(value) + "\n", encoding="utf-8")
            temporary.replace(self.path)
        return True


@dataclass
class ValidationExecutor:
    max_workers: int = 4
    cache: ValidationCache | None = None
    _active: set[str] = field(default_factory=set, init=False)
    _active_count: int = field(default=0, init=False)
    _max_observed: int = field(default=0, init=False)
    _condition: threading.Condition = field(default_factory=threading.Condition, init=False)

    def __post_init__(self) -> None:
        if self.max_workers <= 0:
            raise ValueError("max_workers must be positive")

    def _acquire(self, task: ValidationTask) -> None:
        resources = set(task.resources)
        if not task.independent:
            resources.add("__exclusive__")
        with self._condition:
            while (
                "__exclusive__" in self._active
                or ("__exclusive__" in resources and self._active_count)
                or self._active.intersection(resources)
            ):
                self._condition.wait()
            self._active.update(resources)
            self._active_count += 1
            self._max_observed = max(self._max_observed, self._active_count)

    def _release(self, task: ValidationTask) -> None:
        resources = set(task.resources)
        if not task.independent:
            resources.add("__exclusive__")
        with self._condition:
            self._active.difference_update(resources)
            self._active_count -= 1
            self._condition.notify_all()

    def _run_one(
        self, task: ValidationTask, context: Mapping[str, str], cancel_event: threading.Event,
    ) -> TaskResult:
        if cancel_event.is_set():
            return TaskResult(task.name, "CANCELLED", None, 0.0, "CANCEL_REQUESTED")
        if self.cache:
            cached = self.cache.get(task, context)
            if cached:
                return cached
        self._acquire(task)
        started = time.perf_counter()
        try:
            if cancel_event.is_set():
                return TaskResult(task.name, "CANCELLED", None, 0.0, "CANCEL_REQUESTED")
            try:
                completed = subprocess.run(
                    task.command, capture_output=True, text=True, timeout=task.timeout_seconds,
                    check=False,
                )
                status = "PASSED" if completed.returncode == 0 else "FAILED"
                result = TaskResult(
                    task.name, status, completed.returncode,
                    (time.perf_counter() - started) * 1000,
                    "COMMAND_PASSED" if status == "PASSED" else "COMMAND_FAILED",
                    stdout=completed.stdout, stderr=completed.stderr,
                )
            except subprocess.TimeoutExpired as exc:
                result = TaskResult(
                    task.name, "TIMED_OUT", None, (time.perf_counter() - started) * 1000,
                    "COMMAND_TIMEOUT", stdout=str(exc.stdout or ""), stderr=str(exc.stderr or ""),
                )
            except OSError as exc:
                result = TaskResult(
                    task.name, "FAILED", None, (time.perf_counter() - started) * 1000,
                    "COMMAND_START_FAILED", stderr=str(exc),
                )
            if self.cache:
                self.cache.put(task, context, result)
            return result
        finally:
            self._release(task)

    def execute(
        self,
        tasks: Iterable[ValidationTask],
        *,
        context: Mapping[str, str],
        final_gate_required: bool,
        cancel_event: threading.Event | None = None,
    ) -> Dict[str, Any]:
        ordered = tuple(sorted(tasks, key=lambda item: item.name))
        cancellation = cancel_event or threading.Event()
        self._max_observed = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(self._run_one, task, context, cancellation): task
                for task in ordered
            }
            results = [future.result() for future in as_completed(futures)]
        results.sort(key=lambda item: item.name)
        final_results = [
            result for result in results
            if next(task for task in ordered if task.name == result.name).final_gate
        ]
        all_passed = bool(results) and all(result.passed for result in results)
        gate_passed = bool(final_results) and all(result.passed and not result.cached for result in final_results)
        promotable = all_passed and (not final_gate_required or gate_passed)
        reasons = []
        if final_gate_required and not final_results:
            reasons.append("FINAL_GATE_MISSING")
        elif final_gate_required and not gate_passed:
            reasons.append("FINAL_GATE_FAILED_OR_CACHED")
        if any(not result.passed for result in results):
            reasons.append("VALIDATION_FAILED")
        ready, missing = ValidationCache.validate_context(context)
        if not ready:
            reasons.extend("CACHE_HASH_MISSING:" + key for key in missing)
        payload = {
            "schema": EXECUTION_SCHEMA,
            "max_workers": self.max_workers,
            "max_observed_parallelism": self._max_observed,
            "results": [result.as_dict() for result in results],
            "final_gate_required": final_gate_required,
            "final_gate_passed": gate_passed,
            "promotable": promotable,
            "reason_codes": sorted(set(reasons)),
            "context_hash": _digest(dict(sorted(context.items()))),
        }
        payload["receipt_hash"] = _digest(payload)
        return payload


def explain_execution(receipt: Mapping[str, Any]) -> str:
    """Return byte-stable, secret-free explanation of a persisted receipt."""
    return _canonical(receipt)


__all__ = [
    "CACHE_SCHEMA", "EXECUTION_SCHEMA", "REQUIRED_CONTEXT_HASHES", "TaskResult",
    "ValidationCache", "ValidationExecutor", "ValidationTask", "explain_execution",
]
