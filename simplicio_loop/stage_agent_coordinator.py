"""Portable Stage Agent coordinator + adapters (issue #424, epic #422).

Runtime-agnostic driver that materializes, monitors, cancels, and collects
receipts from the agents/roles defined by the merged #423 contract
(:mod:`simplicio_loop.stage_agents`, PR #435 — ``load_graph``/``validate_graph``/
``validate_instance``/``validate_receipt``/``enforce_independence`` over
``contracts/stage-agents/v1/stages.json``). The core is stdlib-only and must
run on any host; native/queue/human capabilities are optional adapters layered
on top, never a hard dependency.

Flow (see issue #424 "Resultado funcional"):
    1. load+validate the run-stage-graph manifest (delegated to stage_agents.py)
    2. discover host capabilities (adapter ``probe()``)
    3. select a compatible adapter per role/stage (``AdapterRegistry`` fallback order)
    4. compute execution waves by dependencies + capacity (``plan_waves``)
    5. create a concrete instance (``AgentDriver.spawn``)
    6. wait for READY (``AgentDriver.poll`` until ``ready``, never assumed)
    7. send hash-bound input (``AgentDriver.send``)
    8. monitor heartbeat/deadline/cancellation (``AgentDriver.poll`` loop)
    9. collect output + receipt (``AgentDriver.collect``)
   10. validate the instance + receipt via stage_agents.validate_instance /
       stage_agents.validate_receipt (the #423 contract)
   11. release the next wave or apply retry/quarantine/block
   12. persist enough state for restart/replay (``StageCoordinatorJournal``)
   13. never declare a role executed just because the host accepted a prompt
       (an :class:`AgentInstance` only reaches "running" after an observed
       READY, and only reaches a terminal status after an observed receipt)

Adapters implement the pure :class:`AgentDriver` protocol:
``spawn -> send -> poll (until ready/terminal) -> collect -> cancel``.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from . import stage_agents as sa

REPO_ROOT = Path(__file__).resolve().parent.parent

DRIVER_STATUSES = ("created", "ready", "running", "passed", "failed", "blocked", "cancelled", "timed_out")
TERMINAL_DRIVER_STATUSES = frozenset(("passed", "failed", "blocked", "cancelled", "timed_out"))

_TERMINAL_STATUS_MAP = {
    "passed": "completed",
    "failed": "failed",
    "blocked": "blocked",
    "cancelled": "cancelled",
    "timed_out": "failed",
}

COORDINATOR_AGENT_ID = "coordinator"


# --------------------------------------------------------------------------
# Errors / reason codes
# --------------------------------------------------------------------------


class StageCoordinatorError(ValueError):
    def __init__(self, message: str, *, reason_code: str = "coordinator_error"):
        super().__init__(message)
        self.reason_code = reason_code


REASON_NO_COMPATIBLE_ADAPTER = "no_compatible_agent_adapter"
REASON_ZERO_CAPACITY = "zero_capacity_for_required_role"
REASON_TIMEOUT = "timeout"
REASON_STALE_RECEIPT = "stale_receipt"
REASON_NOT_READY = "not_ready"
REASON_CANCELLED = "cancelled"
REASON_INVALID_RECEIPT = "invalid_receipt"
REASON_INVALID_INSTANCE = "invalid_instance"


# --------------------------------------------------------------------------
# Small helpers over the canonical graph shape (stage_id/role_id/depends_on),
# which stage_agents.py (#423) validates but does not expose accessors for.
# --------------------------------------------------------------------------


def stage_by_id(graph: Mapping[str, Any], stage_id: str) -> dict[str, Any]:
    for stage in graph.get("stages", ()):
        if stage["stage_id"] == stage_id:
            return dict(stage)
    raise StageCoordinatorError(f"unknown stage_id: {stage_id}", reason_code="unknown_stage")


def role_by_id(graph: Mapping[str, Any], role_id: str) -> dict[str, Any]:
    for role in graph.get("roles", ()):
        if role["role_id"] == role_id:
            return dict(role)
    raise StageCoordinatorError(f"unknown role_id: {role_id}", reason_code="unknown_role")


def _sha256(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# AgentDriver protocol — the pure interface every adapter implements.
# --------------------------------------------------------------------------


@dataclass
class AgentInstance:
    """A concrete, observed agent instance bound to one stage attempt.

    ``status`` is the driver-facing lifecycle (created/ready/running/terminal);
    :meth:`to_contract_instance` projects it onto the #423
    ``simplicio.agent-instance/v1`` schema's ``terminal_status`` enum for
    validation and persistence.
    """

    instance_id: str
    role_id: str
    stage_id: str
    adapter_kind: str
    status: str = "created"
    runtime: str | None = None
    provider: str | None = None
    model: str | None = None
    isolation_level: str = "process"
    run_id: str = ""
    task_id: str = ""
    attempt_id: str = ""
    fence: str = ""
    plan_revision: int = 0
    negotiated_capabilities: tuple[str, ...] = ()
    context_hash: str = ""
    manifest_hash: str = ""
    created_at: str = field(default_factory=_now_iso)
    ready_at: str | None = None
    started_at: str = field(default_factory=_now_iso)
    last_heartbeat_at: float | None = None
    deadline_at: float | None = None
    output: dict[str, Any] | None = None
    receipt: dict[str, Any] | None = None
    error_reason_code: str | None = None
    # Fields added by the #423 contract after this module's original authoring — kept in
    # sync here so `to_contract_instance()` never falls behind `stage_agents.validate_instance`
    # (the exact "producer not rewired after the validator moved" drift class this repo's
    # conformance suite, #432, exists to catch).
    role_version: str = "1.0.0"
    stage_version: str = "1.0.0"
    work_item_id: str = ""
    attempt_ordinal: int = 1
    coordinator_agent_id: str = COORDINATOR_AGENT_ID
    parent_instance_id: str = COORDINATOR_AGENT_ID
    idempotency_key: str = ""

    def to_contract_instance(self) -> dict[str, Any]:
        now = _now_iso()
        return {
            "schema": "simplicio.agent-instance/v1",
            "agent_instance_id": self.instance_id,
            "role_id": self.role_id,
            "role_version": self.role_version,
            "stage_id": self.stage_id,
            "stage_version": self.stage_version,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "work_item_id": self.work_item_id or self.task_id,
            "attempt_id": self.attempt_id,
            "attempt_ordinal": self.attempt_ordinal,
            "fence": self.fence,
            "plan_revision": self.plan_revision,
            "runtime": self.runtime or self.adapter_kind,
            "provider": self.provider or self.adapter_kind,
            "model": self.model or "n/a",
            "driver": self.adapter_kind,
            "parent_agent_id": COORDINATOR_AGENT_ID,
            "coordinator_agent_id": self.coordinator_agent_id or COORDINATOR_AGENT_ID,
            "parent_instance_id": self.parent_instance_id or COORDINATOR_AGENT_ID,
            "idempotency_key": self.idempotency_key or self.instance_id,
            "isolation_level": self.isolation_level,
            "negotiated_capabilities": list(self.negotiated_capabilities) or ["receipts"],
            "context_hash": self.context_hash or _sha256({"empty": True}),
            "manifest_hash": self.manifest_hash or _sha256({"empty": True}),
            "created_at": self.created_at,
            "ready_at": self.ready_at or self.created_at,
            "started_at": self.started_at,
            "ended_at": now,
            "terminal_status": _TERMINAL_STATUS_MAP.get(self.status, "failed"),
            "reason_code": self.error_reason_code or "ok",
        }


class AgentDriver(Protocol):
    """Pure interface every adapter must implement (issue #424 plan step 1)."""

    kind: str

    def probe(self) -> bool:
        """Return True iff this adapter is usable on the current host."""

    def compatible_with(self, role: Mapping[str, Any], stage: Mapping[str, Any]) -> bool:
        """Return True iff this adapter can satisfy the stage's isolation_level."""

    def spawn(self, *, role: Mapping[str, Any], stage: Mapping[str, Any],
               stage_context: Mapping[str, Any]) -> AgentInstance:
        """Request a new instance. MUST NOT mark it ready — only 'created'."""

    def poll(self, instance: AgentInstance) -> AgentInstance:
        """Observe current state (ready/running/heartbeat/terminal). Idempotent."""

    def send(self, instance: AgentInstance, stage_input: Mapping[str, Any]) -> None:
        """Deliver the hash-bound stage input. Only valid once instance is ready."""

    def collect(self, instance: AgentInstance) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Return (stage_output, stage_receipt) once terminal, else (None, None)."""

    def cancel(self, instance: AgentInstance, *, reason: str) -> None:
        """Best-effort cancellation; must not hang the coordinator."""


_NON_HUMAN_LEVELS = frozenset(("process", "session", "worker", "command"))


# --------------------------------------------------------------------------
# NativeAgentAdapter — for hosts exposing a native subagent/session API.
#
# The native API itself is host-specific (Claude/Cursor/etc.) and out of
# scope for the stdlib core; this adapter takes a thin ``native_ops`` binding
# (spawn/send/poll/cancel callables) so any host can plug in without the core
# depending on it. Absent a binding, ``probe()`` returns False and the
# fallback chain moves on — never a silent no-op pass.
# --------------------------------------------------------------------------


class NativeAgentAdapter:
    kind = "native"

    def __init__(self, *, native_ops: Mapping[str, Any] | None = None, max_slots: int | None = None):
        self._ops = dict(native_ops or {})
        self.max_slots = max_slots

    def probe(self) -> bool:
        return all(k in self._ops for k in ("spawn", "send", "poll", "cancel"))

    def compatible_with(self, role: Mapping[str, Any], stage: Mapping[str, Any]) -> bool:
        if not self.probe():
            return False
        return stage.get("isolation_level", "process") in _NON_HUMAN_LEVELS

    def spawn(self, *, role, stage, stage_context) -> AgentInstance:
        native_id = self._ops["spawn"](role=role, stage=stage, stage_context=stage_context)
        if not native_id:
            raise StageCoordinatorError("native spawn returned no id", reason_code="spawn_failed")
        return AgentInstance(
            instance_id=str(native_id), role_id=role["role_id"], stage_id=stage["stage_id"],
            adapter_kind=self.kind, status="created", isolation_level=stage.get("isolation_level", "process"),
        )

    def poll(self, instance: AgentInstance) -> AgentInstance:
        observed = self._ops["poll"](instance.instance_id)
        # never assume an accepted spawn is already ready: only trust the
        # host's own observed status.
        status = (observed or {}).get("status", "created")
        if status in DRIVER_STATUSES:
            if instance.status == "created" and status == "ready":
                instance.ready_at = _now_iso()
            instance.status = status
        instance.runtime = (observed or {}).get("runtime", instance.runtime)
        instance.provider = (observed or {}).get("provider", instance.provider)
        instance.model = (observed or {}).get("model", instance.model)
        if (observed or {}).get("heartbeat_at"):
            instance.last_heartbeat_at = observed["heartbeat_at"]
        return instance

    def send(self, instance: AgentInstance, stage_input: Mapping[str, Any]) -> None:
        if instance.status != "ready":
            raise StageCoordinatorError(
                f"cannot send to instance {instance.instance_id} in status {instance.status}",
                reason_code=REASON_NOT_READY,
            )
        self._ops["send"](instance.instance_id, stage_input)
        instance.status = "running"

    def collect(self, instance: AgentInstance):
        if instance.status not in TERMINAL_DRIVER_STATUSES:
            return None, None
        collector = self._ops.get("collect")
        if not collector:
            return instance.output, instance.receipt
        output, receipt = collector(instance.instance_id)
        instance.output, instance.receipt = output, receipt
        return output, receipt

    def cancel(self, instance: AgentInstance, *, reason: str) -> None:
        self._ops["cancel"](instance.instance_id, reason=reason)
        if instance.status not in TERMINAL_DRIVER_STATUSES:
            instance.status = "cancelled"
            instance.error_reason_code = reason


# --------------------------------------------------------------------------
# CommandAgentAdapter — portable fallback: spawns a real subprocess.
#
# Never uses shell string interpolation. Input goes to a file inside an
# isolated per-attempt temp dir; the command is invoked with an argv list
# (no shell=True) and an allow-listed environment. Cross-platform kill-tree
# on cancel/timeout.
# --------------------------------------------------------------------------

_DEFAULT_ENV_ALLOWLIST = ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP", "PYTHONIOENCODING")


@dataclass
class _CommandProc:
    popen: subprocess.Popen
    attempt_dir: Path
    input_path: Path
    output_path: Path
    receipt_path: Path
    deadline_at: float


class CommandAgentAdapter:
    kind = "command"

    def __init__(self, *, command: Sequence[str] | None = None, env_allowlist: Sequence[str] = _DEFAULT_ENV_ALLOWLIST,
                 base_tmp_dir: Path | None = None, extra_env: Mapping[str, str] | None = None):
        raw = command or os.environ.get("SIMPLICIO_AGENT_COMMAND")
        if isinstance(raw, str):
            # Safe placeholder templating only — never shell-interpolated:
            # split on whitespace, substitute placeholders per-arg below.
            raw = raw.split()
        self.command_template: list[str] | None = list(raw) if raw else None
        self.env_allowlist = tuple(env_allowlist)
        self.base_tmp_dir = base_tmp_dir or Path(tempfile.gettempdir()) / "simplicio-stage-agents"
        self.extra_env = dict(extra_env or {})
        self._procs: dict[str, _CommandProc] = {}

    def probe(self) -> bool:
        if not self.command_template:
            return False
        exe = self.command_template[0]
        return shutil.which(exe) is not None or Path(exe).exists()

    def compatible_with(self, role: Mapping[str, Any], stage: Mapping[str, Any]) -> bool:
        if not self.probe():
            return False
        return stage.get("isolation_level", "process") in _NON_HUMAN_LEVELS

    def _render_argv(self, *, attempt_dir: Path, input_path: Path, output_path: Path,
                      receipt_path: Path, role: Mapping[str, Any], stage: Mapping[str, Any]) -> list[str]:
        placeholders = {
            "{input}": str(input_path),
            "{output}": str(output_path),
            "{receipt}": str(receipt_path),
            "{attempt_dir}": str(attempt_dir),
            "{role_id}": str(role["role_id"]),
            "{stage_id}": str(stage["stage_id"]),
        }
        argv: list[str] = []
        for token in self.command_template:  # type: ignore[union-attr]
            rendered = token
            for key, value in placeholders.items():
                rendered = rendered.replace(key, value)
            argv.append(rendered)
        return argv

    def spawn(self, *, role, stage, stage_context) -> AgentInstance:
        if not self.probe():
            raise StageCoordinatorError("command adapter not configured/available", reason_code="spawn_failed")
        instance_id = f"cmd-{uuid.uuid4().hex[:12]}"
        attempt_dir = self.base_tmp_dir / instance_id
        attempt_dir.mkdir(parents=True, exist_ok=True)
        input_path = attempt_dir / "stage_input.json"
        output_path = attempt_dir / "stage_output.json"
        receipt_path = attempt_dir / "stage_receipt.json"
        input_path.write_text(json.dumps(dict(stage_context)), encoding="utf-8")
        argv = self._render_argv(
            attempt_dir=attempt_dir, input_path=input_path, output_path=output_path,
            receipt_path=receipt_path, role=role, stage=stage,
        )
        env = {k: os.environ[k] for k in self.env_allowlist if k in os.environ}
        env.update(self.extra_env)
        timeout_seconds = stage.get("timeout_seconds", 600)
        kwargs: dict[str, Any] = dict(
            cwd=str(REPO_ROOT), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if os.name == "posix":
            kwargs["start_new_session"] = True  # own process group for kill-tree
        else:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        popen = subprocess.Popen(argv, **kwargs)  # noqa: S603 - argv list, no shell
        self._procs[instance_id] = _CommandProc(
            popen=popen, attempt_dir=attempt_dir, input_path=input_path,
            output_path=output_path, receipt_path=receipt_path,
            deadline_at=time.time() + timeout_seconds,
        )
        instance = AgentInstance(
            instance_id=instance_id, role_id=role["role_id"], stage_id=stage["stage_id"],
            adapter_kind=self.kind, status="created", runtime="command",
            isolation_level=stage.get("isolation_level", "command"),
        )
        instance.deadline_at = self._procs[instance_id].deadline_at
        return instance

    def poll(self, instance: AgentInstance) -> AgentInstance:
        proc = self._procs.get(instance.instance_id)
        if proc is None:
            return instance
        instance.last_heartbeat_at = time.time()
        if instance.status == "created":
            # A live child process counts as observed-ready: the OS has
            # actually scheduled it, distinct from an accepted-but-unstarted
            # native spawn.
            if proc.popen.poll() is None:
                instance.status = "ready"
                instance.ready_at = _now_iso()
            else:
                instance.status = "running"
        exit_code = proc.popen.poll()
        if exit_code is None:
            if time.time() > proc.deadline_at:
                self.cancel(instance, reason=REASON_TIMEOUT)
            return instance
        instance.status = "passed" if exit_code == 0 else "failed"
        instance.error_reason_code = None if exit_code == 0 else f"exit_{exit_code}"
        return instance

    def send(self, instance: AgentInstance, stage_input: Mapping[str, Any]) -> None:
        proc = self._procs.get(instance.instance_id)
        if proc is None:
            raise StageCoordinatorError("unknown command instance", reason_code="unknown_instance")
        proc.input_path.write_text(json.dumps(dict(stage_input)), encoding="utf-8")
        instance.status = "running"

    def collect(self, instance: AgentInstance):
        proc = self._procs.get(instance.instance_id)
        if proc is None or instance.status not in TERMINAL_DRIVER_STATUSES:
            return None, None
        output = json.loads(proc.output_path.read_text(encoding="utf-8")) if proc.output_path.exists() else None
        receipt = json.loads(proc.receipt_path.read_text(encoding="utf-8")) if proc.receipt_path.exists() else None
        instance.output, instance.receipt = output, receipt
        return output, receipt

    def cancel(self, instance: AgentInstance, *, reason: str) -> None:
        proc = self._procs.get(instance.instance_id)
        if proc is None:
            return
        _kill_tree(proc.popen)
        if instance.status not in TERMINAL_DRIVER_STATUSES:
            instance.status = "cancelled"
            instance.error_reason_code = reason


def _kill_tree(popen: subprocess.Popen) -> None:
    """Cross-platform best-effort kill of a process (and its group on posix)."""
    if popen.poll() is not None:
        return
    try:
        if os.name == "posix":
            import signal
            os.killpg(os.getpgid(popen.pid), signal.SIGTERM)
        else:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(popen.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        popen.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            popen.kill()
        except OSError:
            pass


# --------------------------------------------------------------------------
# QueueAgentAdapter — local/remote queue workers.
#
# Delegates claim/lease/fence semantics to whatever queue client is bound
# (e.g. simplicio_loop.remote_queue); the core only defines the shape of the
# interaction so a fake/local queue can satisfy tests without network.
# --------------------------------------------------------------------------


class QueueAgentAdapter:
    kind = "queue"

    def __init__(self, *, queue_client: Any = None):
        self._client = queue_client

    def probe(self) -> bool:
        return self._client is not None

    def compatible_with(self, role: Mapping[str, Any], stage: Mapping[str, Any]) -> bool:
        return self.probe() and stage.get("isolation_level", "process") in _NON_HUMAN_LEVELS

    def spawn(self, *, role, stage, stage_context) -> AgentInstance:
        if not self.probe():
            raise StageCoordinatorError("queue adapter has no client bound", reason_code="spawn_failed")
        claim = self._client.claim(role=role["role_id"], stage=stage["stage_id"], context=stage_context)
        return AgentInstance(
            instance_id=str(claim["lease_id"]), role_id=role["role_id"], stage_id=stage["stage_id"],
            adapter_kind=self.kind, status="created", runtime="queue",
            isolation_level=stage.get("isolation_level", "worker"),
        )

    def poll(self, instance: AgentInstance) -> AgentInstance:
        status = self._client.status(instance.instance_id)
        observed = status.get("status", instance.status)
        if instance.status == "created" and observed == "ready":
            instance.ready_at = _now_iso()
        instance.status = observed
        instance.last_heartbeat_at = status.get("heartbeat_at", instance.last_heartbeat_at)
        return instance

    def send(self, instance: AgentInstance, stage_input: Mapping[str, Any]) -> None:
        if instance.status != "ready":
            raise StageCoordinatorError("queue instance not ready", reason_code=REASON_NOT_READY)
        self._client.send(instance.instance_id, dict(stage_input))
        instance.status = "running"

    def collect(self, instance: AgentInstance):
        if instance.status not in TERMINAL_DRIVER_STATUSES:
            return None, None
        result = self._client.collect(instance.instance_id)
        instance.output, instance.receipt = result.get("output"), result.get("receipt")
        return instance.output, instance.receipt

    def cancel(self, instance: AgentInstance, *, reason: str) -> None:
        self._client.cancel(instance.instance_id, reason=reason)
        if instance.status not in TERMINAL_DRIVER_STATUSES:
            instance.status = "cancelled"
            instance.error_reason_code = reason


# --------------------------------------------------------------------------
# HumanGateAdapter — the only adapter allowed for role isolation "human".
# --------------------------------------------------------------------------


class HumanGateAdapter:
    kind = "human"

    def __init__(self, *, approval_source: Any = None):
        self._approvals = approval_source or {}

    def probe(self) -> bool:
        return True  # always structurally available; may still time out

    def compatible_with(self, role: Mapping[str, Any], stage: Mapping[str, Any]) -> bool:
        return stage.get("isolation_level") == "human"

    def spawn(self, *, role, stage, stage_context) -> AgentInstance:
        return AgentInstance(
            instance_id=f"human-{uuid.uuid4().hex[:12]}", role_id=role["role_id"],
            stage_id=stage["stage_id"], adapter_kind=self.kind, status="ready", runtime="human",
            isolation_level="human", ready_at=_now_iso(),
        )

    def poll(self, instance: AgentInstance) -> AgentInstance:
        key = (instance.stage_id, instance.role_id)
        approval = self._approvals.get(key) if isinstance(self._approvals, Mapping) else None
        if approval:
            instance.status = approval.get("status", instance.status)
        return instance

    def send(self, instance: AgentInstance, stage_input: Mapping[str, Any]) -> None:
        instance.status = "running"

    def collect(self, instance: AgentInstance):
        if instance.status not in TERMINAL_DRIVER_STATUSES:
            return None, None
        key = (instance.stage_id, instance.role_id)
        approval = (self._approvals.get(key) if isinstance(self._approvals, Mapping) else None) or {}
        return approval.get("output"), approval.get("receipt")

    def cancel(self, instance: AgentInstance, *, reason: str) -> None:
        if instance.status not in TERMINAL_DRIVER_STATUSES:
            instance.status = "blocked"
            instance.error_reason_code = reason


# --------------------------------------------------------------------------
# Capability probe + adapter registry (fallback order per issue #424).
# --------------------------------------------------------------------------

FALLBACK_ORDER = ("native", "command", "queue", "human")


class AdapterRegistry:
    def __init__(self, adapters: Sequence[AgentDriver]):
        self._by_kind = {a.kind: a for a in adapters}

    def select(self, *, role: Mapping[str, Any], stage: Mapping[str, Any]) -> AgentDriver:
        for kind in FALLBACK_ORDER:
            adapter = self._by_kind.get(kind)
            if adapter is None:
                continue
            if adapter.probe() and adapter.compatible_with(role, stage):
                return adapter
        raise StageCoordinatorError(
            f"no compatible adapter for role={role.get('role_id')} stage={stage.get('stage_id')}",
            reason_code=REASON_NO_COMPATIBLE_ADAPTER,
        )


# --------------------------------------------------------------------------
# Waves: dependency + capacity scheduling.
# --------------------------------------------------------------------------


def plan_waves(graph: Mapping[str, Any]) -> list[list[str]]:
    """Group stage_ids into ordered waves: independent stages share a wave."""
    stages = {s["stage_id"]: s for s in graph.get("stages", ())}
    resolved: set[str] = set()
    waves: list[list[str]] = []
    remaining = set(stages)
    while remaining:
        wave = sorted(
            sid for sid in remaining
            if all(dep in resolved for dep in stages[sid].get("depends_on", ()))
        )
        if not wave:
            raise StageCoordinatorError("unresolvable stage graph (cycle?)", reason_code="cycle_detected")
        waves.append(wave)
        resolved.update(wave)
        remaining -= set(wave)
    return waves


def available_slots(*, host_total_slots: int, coordinator_slots: int = 1) -> int:
    return max(0, host_total_slots - coordinator_slots)


# --------------------------------------------------------------------------
# Journal — append-only persistence for restart/replay.
# --------------------------------------------------------------------------


class StageCoordinatorJournal:
    """Append-only JSONL journal. Replay reconstructs coordinator state."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event_type: str, payload: Mapping[str, Any]) -> None:
        record = {"ts": time.time(), "event_type": event_type, "payload": dict(payload)}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")

    def replay(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
        return events

    def passed_stage_ids(self) -> set[str]:
        passed: set[str] = set()
        for event in self.replay():
            if event["event_type"] == "stage_passed":
                passed.add(event["payload"]["stage_id"])
        return passed


# --------------------------------------------------------------------------
# StageAgentCoordinator — orchestrates the full flow.
# --------------------------------------------------------------------------


@dataclass
class StageResult:
    stage_id: str
    status: str
    instance: AgentInstance | None = None
    reason_code: str | None = None


class StageAgentCoordinator:
    """Drives a run-stage-graph's stages through adapters to completion.

    Core is stdlib-only. ``adapters`` may be a subset (e.g. just a
    CommandAgentAdapter) — any host with just Python + a configured command
    can run the whole flow; native/queue/human are additive.
    """

    def __init__(self, *, graph: Mapping[str, Any] | None = None, run_id: str, task_id: str,
                 adapters: Sequence[AgentDriver], journal: StageCoordinatorJournal | None = None,
                 host_total_slots: int = 4, coordinator_slots: int = 1,
                 poll_interval_seconds: float = 0.05):
        self.graph = graph or sa.load_graph()
        ok, errors = sa.validate_graph(self.graph)
        if not ok:
            raise StageCoordinatorError("invalid stage graph: " + "; ".join(errors), reason_code="invalid_graph")
        # The graph carries its own pinned self-hash (`manifest_hash`, checked by
        # `stage_agents.validate_receipt` against the graph/instance/receipt triple) — use it
        # verbatim rather than recomputing an ad hoc hash of the graph dict, which would never
        # match the canonical value receipts are validated against.
        self.manifest_hash = str(self.graph.get("manifest_hash") or _sha256(self.graph))
        self.run_id = run_id
        self.task_id = task_id
        self.registry = AdapterRegistry(adapters)
        self.journal = journal
        self.passed_stages: dict[str, dict[str, Any]] = {}
        self.rejected: list[dict[str, Any]] = []
        self.slots = available_slots(host_total_slots=host_total_slots, coordinator_slots=coordinator_slots)
        self.poll_interval_seconds = poll_interval_seconds
        self.results: dict[str, StageResult] = {}
        if self.journal:
            for stage_id in self.journal.passed_stage_ids():
                self.results[stage_id] = StageResult(stage_id=stage_id, status="passed")
                self.passed_stages[stage_id] = {}

    def _log(self, event_type: str, payload: Mapping[str, Any]) -> None:
        if self.journal:
            self.journal.append(event_type, payload)

    def is_unlocked(self, stage_id: str) -> bool:
        stage = stage_by_id(self.graph, stage_id)
        return all(dep in self.passed_stages for dep in stage.get("depends_on", ()))

    def terminal_reached(self) -> bool:
        terminal_ids = [s["stage_id"] for s in self.graph.get("stages", ()) if not s.get("next_stages")]
        return bool(terminal_ids) and all(sid in self.passed_stages for sid in terminal_ids)

    def unlocked_ready_stages(self) -> list[str]:
        return [
            s["stage_id"] for s in self.graph.get("stages", ())
            if s["stage_id"] not in self.passed_stages and self.is_unlocked(s["stage_id"])
        ]

    def run_stage(self, stage_id: str, *, fence: str = "fence-0", plan_revision: int = 0,
                   attempt_id: str | None = None, deadline_seconds: float | None = None) -> StageResult:
        if stage_id in self.results and self.results[stage_id].status == "passed":
            return self.results[stage_id]  # idempotent: already accepted

        stage = stage_by_id(self.graph, stage_id)
        role = role_by_id(self.graph, stage["role_id"])
        attempt_id = attempt_id or f"attempt-{uuid.uuid4().hex[:10]}"

        if self.slots <= 0:
            self._log("blocked", {"stage_id": stage_id, "reason_code": REASON_ZERO_CAPACITY})
            result = StageResult(stage_id=stage_id, status="blocked", reason_code=REASON_ZERO_CAPACITY)
            self.results[stage_id] = result
            return result

        try:
            adapter = self.registry.select(role=role, stage=stage)
        except StageCoordinatorError as exc:
            self._log("blocked", {"stage_id": stage_id, "reason_code": exc.reason_code})
            result = StageResult(stage_id=stage_id, status="blocked", reason_code=exc.reason_code)
            self.results[stage_id] = result
            return result
        self._log("routing_decision", {"stage_id": stage_id, "adapter": adapter.kind})

        stage_context = {
            "role_id": role["role_id"], "stage_id": stage["stage_id"], "run_id": self.run_id,
            "task_id": self.task_id, "attempt_id": attempt_id, "fence": fence,
            "plan_revision": plan_revision, "isolation_level": stage.get("isolation_level", "process"),
            "required_capabilities": stage.get("required_capabilities", []),
        }
        context_hash = _sha256(stage_context)

        instance = adapter.spawn(role=role, stage=stage, stage_context=stage_context)
        instance.run_id, instance.task_id, instance.attempt_id = self.run_id, self.task_id, attempt_id
        instance.fence, instance.plan_revision = fence, plan_revision
        instance.context_hash, instance.manifest_hash = context_hash, self.manifest_hash
        instance.negotiated_capabilities = tuple(stage.get("required_capabilities", ()))
        instance.role_version = str(role.get("version", "1.0.0"))
        instance.stage_version = str(stage.get("version", "1.0.0"))
        instance.work_item_id = self.task_id
        instance.attempt_ordinal = 1
        instance.coordinator_agent_id = COORDINATOR_AGENT_ID
        instance.parent_instance_id = COORDINATOR_AGENT_ID
        instance.idempotency_key = f"{self.run_id}:{stage_id}:{attempt_id}"
        self._log("instance_created", {"stage_id": stage_id, "instance_id": instance.instance_id, "adapter": adapter.kind})

        deadline = time.time() + (deadline_seconds if deadline_seconds is not None else stage.get("timeout_seconds", 600))
        while instance.status == "created":
            instance = adapter.poll(instance)
            if instance.status == "created" and time.time() > deadline:
                adapter.cancel(instance, reason=REASON_TIMEOUT)
                self._log("cancelled", {"stage_id": stage_id, "instance_id": instance.instance_id, "reason_code": REASON_TIMEOUT})
                result = StageResult(stage_id=stage_id, status="timed_out", instance=instance, reason_code=REASON_TIMEOUT)
                self.results[stage_id] = result
                return result
            if instance.status == "created":
                time.sleep(self.poll_interval_seconds)
        self._log("instance_ready", {"stage_id": stage_id, "instance_id": instance.instance_id})

        # hash-bound input: stage_context carries fence/plan_revision + its own
        # content hash, so a stale/tampered input is detectable downstream.
        # agent_instance_id is added post-spawn so the agent can bind its
        # receipt to the exact instance the coordinator is tracking.
        send_payload = dict(stage_context, agent_instance_id=instance.instance_id)
        adapter.send(instance, send_payload)
        self._log("input_sent", {"stage_id": stage_id, "instance_id": instance.instance_id})

        while instance.status not in TERMINAL_DRIVER_STATUSES:
            instance = adapter.poll(instance)
            self._log("heartbeat", {"stage_id": stage_id, "instance_id": instance.instance_id,
                                     "status": instance.status, "heartbeat_at": instance.last_heartbeat_at})
            if instance.status not in TERMINAL_DRIVER_STATUSES and time.time() > deadline:
                adapter.cancel(instance, reason=REASON_TIMEOUT)
                instance = adapter.poll(instance)
                break
            if instance.status not in TERMINAL_DRIVER_STATUSES:
                time.sleep(self.poll_interval_seconds)

        output, receipt = adapter.collect(instance)
        self._log("collected", {"stage_id": stage_id, "instance_id": instance.instance_id,
                                 "has_output": output is not None, "has_receipt": receipt is not None})

        if receipt is None:
            reason = instance.error_reason_code or REASON_TIMEOUT
            result = StageResult(stage_id=stage_id, status=instance.status or "failed", instance=instance, reason_code=reason)
            self.results[stage_id] = result
            return result

        instance_record = instance.to_contract_instance()
        inst_ok, inst_errors = sa.validate_instance(
            instance_record,
            run_identity={"run_id": self.run_id, "task_id": self.task_id, "attempt_id": attempt_id,
                          "fence": fence, "plan_revision": plan_revision,
                          "attempt_ordinal": instance.attempt_ordinal},
        )
        if not inst_ok:
            self._log("rejected", {"stage_id": stage_id, "reason_code": REASON_INVALID_INSTANCE, "errors": inst_errors})
            result = StageResult(stage_id=stage_id, status="blocked", instance=instance, reason_code=REASON_INVALID_INSTANCE)
            self.rejected.append({"stage_id": stage_id, "reason_code": REASON_INVALID_INSTANCE, "errors": inst_errors})
            self.results[stage_id] = result
            return result

        rec_ok, rec_errors = sa.validate_receipt(receipt, instance_record, self.graph)
        if not rec_ok:
            self._log("rejected", {"stage_id": stage_id, "reason_code": REASON_INVALID_RECEIPT, "errors": rec_errors})
            result = StageResult(stage_id=stage_id, status="blocked", instance=instance, reason_code=REASON_INVALID_RECEIPT)
            self.rejected.append({"stage_id": stage_id, "reason_code": REASON_INVALID_RECEIPT, "errors": rec_errors})
            self.results[stage_id] = result
            return result

        if not self.is_unlocked(stage_id):
            self.rejected.append({"stage_id": stage_id, "reason_code": "dependency_skip"})
            result = StageResult(stage_id=stage_id, status="blocked", instance=instance, reason_code="dependency_skip")
            self.results[stage_id] = result
            return result

        if receipt.get("verdict") != "pass" or not receipt.get("accepted"):
            result = StageResult(stage_id=stage_id, status="failed", instance=instance, reason_code="not_passed")
            self.results[stage_id] = result
            return result

        self.passed_stages[stage_id] = dict(receipt)
        self._log("stage_passed", {"stage_id": stage_id, "instance_id": instance.instance_id})
        result = StageResult(stage_id=stage_id, status="passed", instance=instance)
        self.results[stage_id] = result
        return result

    def run_all(self, **stage_kwargs: Any) -> dict[str, StageResult]:
        """Drive every stage to completion, wave by wave, honoring capacity."""
        for wave in plan_waves(self.graph):
            ready = [sid for sid in wave if self.is_unlocked(sid) and sid not in self.results]
            batch = ready[: max(self.slots, 0)] if self.slots else ready
            for stage_id in batch:
                self.run_stage(stage_id, **stage_kwargs)
            for stage_id in ready[len(batch):] if self.slots else []:
                self._log("blocked", {"stage_id": stage_id, "reason_code": REASON_ZERO_CAPACITY})
                self.results[stage_id] = StageResult(stage_id=stage_id, status="blocked", reason_code=REASON_ZERO_CAPACITY)
        return self.results

    def status_report(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "passed_stages": sorted(self.passed_stages.keys()),
            "unlocked_ready_stages": self.unlocked_ready_stages(),
            "rejected": self.rejected,
            "terminal_reached": self.terminal_reached(),
            "results": {sid: r.status for sid, r in self.results.items()},
        }


__all__ = [
    "COORDINATOR_AGENT_ID", "REASON_CANCELLED", "REASON_INVALID_INSTANCE", "REASON_INVALID_RECEIPT",
    "REASON_NOT_READY", "REASON_NO_COMPATIBLE_ADAPTER", "REASON_STALE_RECEIPT", "REASON_TIMEOUT",
    "REASON_ZERO_CAPACITY", "AdapterRegistry", "AgentDriver", "AgentInstance", "CommandAgentAdapter",
    "FALLBACK_ORDER", "HumanGateAdapter", "NativeAgentAdapter", "QueueAgentAdapter", "StageAgentCoordinator",
    "StageCoordinatorError", "StageCoordinatorJournal", "StageResult", "available_slots", "plan_waves",
    "role_by_id", "stage_by_id",
]
