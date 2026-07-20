"""Observability + opt-in enforcement layer for the #514 process-supervisor contract.

Issue #516 (child of the #498 supervisor epic) asks for: detecting Simplicio processes that
bypass the supervisor, a status/top/queue/cancel/drain/reports surface, a circuit breaker, and
rollout shadow/canary with standalone fallback. This module is the first real slice of that --
built on top of ``simplicio_loop.process_supervisor`` (``ProcessSpec``/``ProcessLease``/
``ProcessResult`` + ``PythonProcessAdapter``) from the already-merged #514, not on the Rust/Tokio
backend from #515 (which may not exist yet in a given checkout).

Explicitly NOT the full #498 DoD -- see ``docs/SUPERVISOR_ENFORCEMENT_RUNBOOK.md`` for what is
implemented now versus deferred (quotas, cgroups/Job Objects resource limiting, full
shadow/canary rollout automation, cross-host sync).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from .process_supervisor import ProcessLease, ProcessResult, ProcessSpec, PythonProcessAdapter

REGISTRY_SCHEMA = "simplicio.supervisor-registry/v1"
EVENT_SCHEMA = "simplicio.supervisor-event/v1"
BREAKER_SCHEMA = "simplicio.supervisor-circuit-breaker/v1"

# Substrings that identify a process as part of the Simplicio ecosystem: invoked via one of the
# packaged console scripts (see [project.scripts] in pyproject.toml), as a `-m simplicio_loop.*`
# module, or running a script that lives under this repository's simplicio_loop/ package. This
# is a best-effort *signature* match on argv, not a cryptographic guarantee -- documented as such
# in the runbook. Callers may extend it (e.g. a private fork's own entrypoint names).
SIMPLICIO_SIGNATURES: Sequence[str] = (
    "simplicio_loop",
    "simplicio-loop",
    "simplicio-cli",
    "simplicio-dev-cli",
    "simplicio-mapper",
    "simplicio-remote-worker",
    "simplicio-remote-queue-server",
)

FAILURE_ERROR_CODES = {"spawn_error", "executable_not_found"}


def is_simplicio_cmdline(cmdline: Sequence[str]) -> bool:
    """True when ``cmdline`` looks like a Simplicio-ecosystem entrypoint."""
    joined = " ".join(str(part) for part in cmdline)
    return any(signature in joined for signature in SIMPLICIO_SIGNATURES)


def default_state_dir() -> Path:
    return Path(os.environ.get("SIMPLICIO_SUPERVISOR_STATE_DIR", ".orchestrator/supervisor"))


def default_registry_path() -> Path:
    return default_state_dir() / "registry.json"


def default_events_path() -> Path:
    return default_state_dir() / "events.jsonl"


def default_breaker_path() -> Path:
    return default_state_dir() / "breaker.json"


def enforcement_enabled(*, override: Optional[bool] = None) -> bool:
    """Enforcement is opt-in and OFF by default. Opt in via ``SIMPLICIO_SUPERVISOR_ENFORCE=1``
    or an explicit ``override``. Callers should treat "unset/unparseable" as OFF (fail safe)."""
    if override is not None:
        return override
    return os.environ.get("SIMPLICIO_SUPERVISOR_ENFORCE", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(str(tmp), str(path))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    except OSError:
        return False
    return True


def kill_process_tree(pid: int, *, sig: int = signal.SIGKILL) -> bool:
    """Best-effort kill of ``pid`` and its descendants, from a thread/process that never held
    the live ``Process`` object -- the real in-flight cancellation the #498 epic still lacked
    (see ``docs/SUPERVISOR_ENFORCEMENT_RUNBOOK.md``): a supervised child registered in
    :class:`ProcessRegistry` could previously only be killed by the coroutine that spawned it
    (on its own timeout/cancellation), never by an external ``cancel`` request arriving on a
    different thread while ``execute`` is still blocked awaiting completion.

    POSIX: children spawned via ``PythonProcessAdapter``/``SupervisedProcessAdapter`` use
    ``start_new_session=True`` so they head their own process group; ``os.killpg`` on that
    group reaps the whole tree in one signal. Falls back to killing just ``pid`` when it is not
    a group leader (e.g. the Rust backend's wrapper process, which does not call ``setsid``).
    Windows: ``taskkill /T /F`` kills the process tree rooted at ``pid`` directly.
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=5, check=False,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.killpg(os.getpgid(pid), sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        os.kill(pid, sig)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False


@dataclass(frozen=True)
class ProcessRecord:
    """One observed OS process, as seen by ``scan_host_processes``."""

    pid: int
    cmdline: List[str]


def scan_host_processes() -> List[ProcessRecord]:
    """Enumerate running processes with their argv.

    Linux: reads ``/proc/<pid>/cmdline`` directly (fast, no subprocess). macOS/other POSIX
    without ``/proc``: falls back to ``ps -axo pid=,args=``. Windows is NOT implemented yet --
    returns an empty list there rather than guessing; see the runbook's honest scope note.
    """
    proc_root = Path("/proc")
    if proc_root.is_dir():
        return _scan_proc(proc_root)
    if os.name == "nt":
        return []
    return _scan_ps()


def _scan_proc(proc_root: Path) -> List[ProcessRecord]:
    records: List[ProcessRecord] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        parts = [part for part in raw.decode("utf-8", errors="replace").split("\x00") if part]
        if parts:
            records.append(ProcessRecord(int(entry.name), parts))
    return records


def _scan_ps() -> List[ProcessRecord]:
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,args="],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    records: List[ProcessRecord] = []
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_str, _, rest = line.partition(" ")
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        records.append(ProcessRecord(pid, rest.split()))
    return records


class ProcessRegistry:
    """Cross-process bookkeeping of PIDs launched through the supervisor.

    Persisted to a JSON file so a *separate* CLI invocation (status/top/the detector) can see
    what is currently supervised without sharing Python process memory with the process that
    spawned the child. Registration is keyed by OS pid; a stale entry (the pid was reused by an
    unrelated process after the supervised child exited without unregistering, e.g. a crash of
    the supervisor itself) is pruned on read via ``os.kill(pid, 0)`` liveness probing -- this is
    the "failure of the supervisor must not leave orphans undetected" invariant from #498,
    applied to the bookkeeping data itself.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or default_registry_path()

    def _read(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"schema": REGISTRY_SCHEMA, "processes": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"schema": REGISTRY_SCHEMA, "processes": {}}
        if not isinstance(data, dict) or not isinstance(data.get("processes"), dict):
            return {"schema": REGISTRY_SCHEMA, "processes": {}}
        return data

    def register(self, pid: int, *, lease_id: str, spec_hash: str, argv: Sequence[str]) -> None:
        data = self._read()
        data["processes"][str(pid)] = {
            "pid": pid,
            "lease_id": lease_id,
            "spec_hash": spec_hash,
            "argv": list(argv),
            "registered_at": time.time(),
        }
        data["schema"] = REGISTRY_SCHEMA
        _atomic_write_json(self.path, data)

    def unregister(self, pid: int) -> None:
        data = self._read()
        if str(pid) in data["processes"]:
            data["processes"].pop(str(pid), None)
            data["schema"] = REGISTRY_SCHEMA
            _atomic_write_json(self.path, data)

    def prune_dead(self) -> None:
        data = self._read()
        alive = {
            pid_str: record
            for pid_str, record in data["processes"].items()
            if _pid_alive(int(pid_str))
        }
        if alive != data["processes"]:
            data["processes"] = alive
            data["schema"] = REGISTRY_SCHEMA
            _atomic_write_json(self.path, data)

    def active(self) -> Dict[int, Dict[str, Any]]:
        self.prune_dead()
        data = self._read()
        return {int(pid_str): record for pid_str, record in data["processes"].items()}

    def active_pids(self) -> Set[int]:
        return set(self.active())

    def terminate(self, lease_id: str, *, sig: int = signal.SIGKILL) -> Dict[str, Any]:
        """Kill the live, supervised process registered under ``lease_id``, for real.

        Looks up the pid the registry already tracks for this lease and kills its whole tree
        via :func:`kill_process_tree` -- independent of whichever thread/coroutine is currently
        blocked awaiting that process's completion (e.g. a Hub ``execute`` call in flight on a
        different connection thread). Returns a status dict rather than raising: an unknown or
        already-finished lease is a normal "nothing to cancel" outcome, not an error.
        """
        for pid, record in self.active().items():
            if record.get("lease_id") == lease_id:
                killed = kill_process_tree(pid, sig=sig)
                return {"found": True, "pid": pid, "lease_id": lease_id, "killed": killed}
        return {"found": False, "pid": None, "lease_id": lease_id, "killed": False}


def detect_unsupervised(
    registry: ProcessRegistry, *, exclude_pids: Optional[Set[int]] = None
) -> List[ProcessRecord]:
    """Diff the live host process table against the registry's bookkeeping.

    A process is flagged when its cmdline matches ``is_simplicio_cmdline`` (it looks like part
    of the Simplicio ecosystem) AND its pid is not currently registered as supervised. This is
    the detector required by #516: "process launched via a Simplicio CLI entrypoint but with no
    registered lease/PID tracked by the supervisor's own bookkeeping".
    """
    tracked = registry.active_pids()
    exclude = {os.getpid()} | (exclude_pids or set())
    flagged: List[ProcessRecord] = []
    for record in scan_host_processes():
        if record.pid in exclude or record.pid in tracked:
            continue
        if is_simplicio_cmdline(record.cmdline):
            flagged.append(record)
    return flagged


class SupervisedProcessAdapter:
    """Runs a ``ProcessSpec`` through ``PythonProcessAdapter`` while registering the real OS
    pid in a :class:`ProcessRegistry` for the process's lifetime -- the bookkeeping the detector
    diffs against. This is what "spawned properly through the supervisor" means operationally.
    """

    def __init__(
        self,
        *,
        registry: Optional[ProcessRegistry] = None,
        adapter: Optional[PythonProcessAdapter] = None,
    ) -> None:
        self.registry = registry or ProcessRegistry()
        self.adapter = adapter or PythonProcessAdapter()

    async def run(
        self, spec: ProcessSpec, *, lease: Optional[ProcessLease] = None
    ) -> ProcessResult:
        process_lease = lease or ProcessLease(
            lease_id=spec.idempotency_key or "lease-" + uuid.uuid4().hex,
            spec_hash=spec.spec_hash,
        )
        registered_pid: Dict[str, int] = {}

        def _on_spawned(process: Any) -> None:
            self.registry.register(
                process.pid, lease_id=process_lease.lease_id,
                spec_hash=spec.spec_hash, argv=spec.argv,
            )
            registered_pid["pid"] = process.pid

        try:
            return await self.adapter.run(spec, lease=process_lease, on_spawned=_on_spawned)
        finally:
            if "pid" in registered_pid:
                self.registry.unregister(registered_pid["pid"])


def enforce(
    records: Iterable[ProcessRecord], *, enabled: bool, sig: int = signal.SIGTERM
) -> List[Dict[str, Any]]:
    """Act on flagged (unsupervised) records.

    When ``enabled`` is False (the default -- enforcement is opt-in and OFF by default) this
    ONLY observes: it reports what it *would* do without sending any signal to anything. When
    True, it best-effort signals each flagged pid and reports the real outcome. Never called
    with ``enabled=True`` implicitly -- the CLI requires an explicit ``--enforce`` flag AND
    ``enforcement_enabled()`` to agree before this function is invoked with True.
    """
    actions: List[Dict[str, Any]] = []
    for record in records:
        if not enabled:
            actions.append({"pid": record.pid, "argv": record.cmdline, "action": "observed_only"})
            continue
        try:
            os.kill(record.pid, sig)
            actions.append({
                "pid": record.pid, "argv": record.cmdline,
                "action": "signaled", "signal": int(sig),
            })
        except OSError as exc:
            actions.append({
                "pid": record.pid, "argv": record.cmdline,
                "action": "signal_failed", "error": str(exc),
            })
    return actions


class CircuitBreaker:
    """Trips OPEN after ``failure_threshold`` consecutive supervised-spawn failures, and moves
    to HALF_OPEN after ``cooldown_seconds`` have elapsed since the trip (a subsequent success
    closes it again). Documented trip condition (#516's "circuit breaker triggered by a
    documented condition"): ``failure_threshold`` consecutive results whose ``error_code`` is in
    ``FAILURE_ERROR_CODES`` (``spawn_error`` / ``executable_not_found``) -- i.e. repeated spawn
    failures, not ordinary non-zero exit codes from the user's own command.
    """

    def __init__(self, failure_threshold: int = 3, cooldown_seconds: float = 5.0) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._consecutive_failures = 0
        self._state = "closed"
        self._tripped_at = 0.0
        self.trip_reason = ""

    @property
    def state(self) -> str:
        if self._state == "open" and (time.monotonic() - self._tripped_at) >= self.cooldown_seconds:
            self._state = "half_open"
        return self._state

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._state = "closed"
        self.trip_reason = ""

    def record_failure(self, reason: str) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold and self._state != "open":
            self._state = "open"
            self._tripped_at = time.monotonic()
            self.trip_reason = reason

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": BREAKER_SCHEMA,
            "state": self.state,
            "consecutive_failures": self._consecutive_failures,
            "failure_threshold": self.failure_threshold,
            "cooldown_seconds": self.cooldown_seconds,
            "trip_reason": self.trip_reason,
        }

    def save(self, path: Optional[Path] = None) -> None:
        payload = self.to_dict()
        payload["_consecutive_failures"] = self._consecutive_failures
        payload["_tripped_at"] = self._tripped_at
        _atomic_write_json(path or default_breaker_path(), payload)

    @classmethod
    def load(
        cls, path: Optional[Path] = None, *, failure_threshold: int = 3, cooldown_seconds: float = 5.0
    ) -> "CircuitBreaker":
        breaker = cls(failure_threshold=failure_threshold, cooldown_seconds=cooldown_seconds)
        target = path or default_breaker_path()
        if target.exists():
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return breaker
            breaker._consecutive_failures = int(data.get("_consecutive_failures", 0))
            breaker._state = str(data.get("state", "closed"))
            breaker._tripped_at = float(data.get("_tripped_at", 0.0))
            breaker.trip_reason = str(data.get("trip_reason", ""))
        return breaker


async def run_guarded(
    spec: ProcessSpec,
    *,
    breaker: CircuitBreaker,
    supervised: Optional[SupervisedProcessAdapter] = None,
    standalone: Optional[PythonProcessAdapter] = None,
) -> Dict[str, Any]:
    """Run ``spec`` through the supervisor unless the breaker is OPEN, in which case fall back
    to a plain, unsupervised ``PythonProcessAdapter`` run -- still argv-only and spec-validated
    (never a bare shell string), just not registered/bookkept. This is the "fallback standalone"
    #516 asks the breaker to preserve: a tripped breaker degrades observability, it never stops
    work from completing.
    """
    supervised_adapter = supervised or SupervisedProcessAdapter()
    if breaker.state == "open":
        result = await (standalone or PythonProcessAdapter()).run(spec)
        return {"result": result, "mode": "standalone_fallback", "breaker": breaker.to_dict()}

    result = await supervised_adapter.run(spec)
    if result.error_code in FAILURE_ERROR_CODES:
        breaker.record_failure(result.error_code)
    else:
        breaker.record_success()
    return {"result": result, "mode": "supervised", "breaker": breaker.to_dict()}


def append_event(kind: str, payload: Dict[str, Any], *, path: Optional[Path] = None) -> Dict[str, Any]:
    """Append one JSONL event to the reports log (detector scans, enforcement actions, breaker
    trips). This is what the ``reports`` CLI subcommand reads back."""
    events_path = path or default_events_path()
    events_path.parent.mkdir(parents=True, exist_ok=True)
    event: Dict[str, Any] = {"schema": EVENT_SCHEMA, "kind": kind, "ts": time.time()}
    event.update(payload)
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return event


def read_events(*, path: Optional[Path] = None, limit: int = 50) -> List[Dict[str, Any]]:
    events_path = path or default_events_path()
    if not events_path.exists():
        return []
    lines = [line for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    out: List[Dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


__all__ = [
    "REGISTRY_SCHEMA",
    "EVENT_SCHEMA",
    "BREAKER_SCHEMA",
    "SIMPLICIO_SIGNATURES",
    "FAILURE_ERROR_CODES",
    "is_simplicio_cmdline",
    "default_state_dir",
    "default_registry_path",
    "default_events_path",
    "default_breaker_path",
    "enforcement_enabled",
    "ProcessRecord",
    "scan_host_processes",
    "ProcessRegistry",
    "detect_unsupervised",
    "SupervisedProcessAdapter",
    "enforce",
    "CircuitBreaker",
    "run_guarded",
    "append_event",
    "read_events",
]
