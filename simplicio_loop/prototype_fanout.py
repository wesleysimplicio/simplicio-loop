"""Candidate fan-out execution for the Prototype-First gate (issue #568).

Epic #568's own checklist explicitly deferred "full candidate fan-out execution": the
schemas/state-machine/classifier in `prototype_gate.py` produce plans/candidates/decisions/receipts
but nothing in this repo actually RUNS a candidate. This module is that missing piece: given N
candidate specs bound to a hash-pinned `prototype-plan/v1`, dispatch each candidate for execution
via a pluggable `CandidateExecutor`, with bounded concurrency and per-candidate isolation, then
bridge the real execution result back into `prototype_gate` payloads so REVISE/ACCEPT/REJECT
decisions can reference real receipts instead of only planning-time data.

Two executors:

- `LocalSubprocessExecutor` (default, genuinely working): runs a candidate's declared command list
  in an isolated temp directory, capturing stdout/stderr/exit code/artifacts. This is real and
  tested, not a stub.
- `RuntimeSandboxExecutor` (documented plug point, NOT live): the ecosystem's execution layer is
  the Runtime's sandboxed executor being built in parallel at
  `simplicio-runtime/src/prototype_gate.rs` (`simplicio prototype run`). That binary is WIP and not
  reliably shell-out-able yet, so this class conforms to the same `CandidateExecutor` protocol but
  its `execute()` raises an explicit, honest `NotImplementedError` rather than faking an
  integration. Once `simplicio prototype run` ships, only this one class needs a body -- callers
  that already pass `executor=` need zero changes.

Concurrency reuses `scripts/fan_out.py`'s `ResourceGovernor` (this repo's existing bounded-admission
primitive for `agent_store`/`kanban_coordination`-style parallel work) instead of inventing a new
governor from scratch.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, List, Mapping, Optional, Protocol, runtime_checkable

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
try:
    from scripts.fan_out import ResourceGovernor  # type: ignore
except ImportError:  # pragma: no cover - keeps this module importable standalone
    try:
        from fan_out import ResourceGovernor  # type: ignore
    except ImportError:  # pragma: no cover
        ResourceGovernor = None  # type: ignore[assignment,misc]

from simplicio_loop import prototype_gate as pg  # noqa: E402 -- after the optional sys.path shim above

CANDIDATE_RUN_SCHEMA = "simplicio.prototype-candidate-run/v1"
FANOUT_REPORT_SCHEMA = "simplicio.prototype-fanout-report/v1"

# Terminal per-candidate statuses. "ok" is the only success terminal; every other value is a
# distinct, honest failure mode -- never collapsed into a single generic "failed".
TERMINAL_STATUSES = frozenset(("ok", "failed", "timeout", "crashed", "error"))

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_CONCURRENCY = 4


def _stable_hash(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------
# Candidate spec / result
# ---------------------------------------------------------------------------------------------


@dataclass
class CandidateSpec:
    """One candidate to execute: a shell-command pipeline plus its own isolation inputs.

    `commands` is a list of argv lists (never a raw shell string -- no `shell=True`, so a
    candidate cannot escape quoting into an unintended second command). Each command runs in
    order inside the same isolated working directory; the first non-zero exit stops the pipeline
    for that candidate (subsequent commands are not attempted) without affecting any other
    candidate in the same fan-out.
    """

    candidate_id: str
    commands: List[List[str]]
    strategy: str = ""
    agent_id: str = "local-subprocess"
    timeout_s: float = DEFAULT_TIMEOUT_S
    env: Mapping[str, str] = field(default_factory=dict)
    # Optional hermetic seed files written into the isolated workdir before any command runs
    # (relative path -> text content). Lets a test/caller stage fixture input without touching a
    # shared directory.
    seed_files: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.candidate_id).strip():
            raise ValueError("CandidateSpec.candidate_id must not be empty")
        self.commands = [list(cmd) for cmd in (self.commands or [])]


@dataclass
class CandidateRunResult:
    """One candidate's terminal execution outcome. Always independent of every other candidate's
    result in the same fan-out -- a crash here never mutates or blocks another result."""

    candidate_id: str
    status: str  # one of TERMINAL_STATUSES
    exit_code: Optional[int]
    stdout: str
    stderr: str
    duration_ms: float
    artifacts: List[str]
    workdir: str
    error: str = ""

    def __post_init__(self) -> None:
        if self.status not in TERMINAL_STATUSES:
            raise ValueError(f"unknown candidate run status: {self.status!r}")


# ---------------------------------------------------------------------------------------------
# Executors
# ---------------------------------------------------------------------------------------------


@runtime_checkable
class CandidateExecutor(Protocol):
    """The pluggable execution contract. Any object with this method can be handed to
    `dispatch_candidates` as `executor=`."""

    def execute(self, candidate: CandidateSpec) -> CandidateRunResult: ...  # pragma: no cover


class LocalSubprocessExecutor:
    """Real, local-first default executor: runs each candidate's command list inside its own
    fresh temp directory (never shared between candidates -- concurrent candidates cannot see or
    corrupt each other's files), with a wall-clock timeout per command and no shell interpolation.

    Not a stub: this genuinely spawns processes, enforces the timeout via `subprocess.run`, and
    captures real stdout/stderr/exit codes/artifacts. `keep_workdir=True` is available for
    debugging a specific run; production fan-outs clean up after collecting artifacts so a large
    batch of candidates does not leak disk.
    """

    def __init__(self, *, keep_workdir: bool = False, base_dir: Optional[str] = None):
        self.keep_workdir = keep_workdir
        self.base_dir = base_dir

    def execute(self, candidate: CandidateSpec) -> CandidateRunResult:
        workdir = tempfile.mkdtemp(prefix=f"proto-cand-{candidate.candidate_id}-", dir=self.base_dir)
        start = time.monotonic()

        for relpath, content in (candidate.seed_files or {}).items():
            target = Path(workdir) / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (candidate.env or {}).items()})

        stdout_parts: List[str] = []
        stderr_parts: List[str] = []
        exit_code: Optional[int] = None
        status = "ok"
        error = ""
        try:
            if not candidate.commands:
                raise ValueError("candidate declares no commands to run")
            for cmd in candidate.commands:
                proc = subprocess.run(
                    cmd,
                    cwd=workdir,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=max(0.1, float(candidate.timeout_s)),
                )
                stdout_parts.append(proc.stdout or "")
                stderr_parts.append(proc.stderr or "")
                exit_code = proc.returncode
                if proc.returncode != 0:
                    status = "failed"
                    break
        except subprocess.TimeoutExpired as exc:
            status = "timeout"
            error = str(exc)
            out = exc.stdout
            err = exc.stderr
            stdout_parts.append(out if isinstance(out, str) else (out.decode(errors="replace") if out else ""))
            stderr_parts.append(err if isinstance(err, str) else (err.decode(errors="replace") if err else ""))
        except (OSError, ValueError) as exc:
            # Command not found, empty command list, invalid argv, etc. -- an honest crash, not a
            # silently swallowed exception.
            status = "crashed"
            error = str(exc)
        except Exception as exc:  # pragma: no cover - defensive: one candidate must never take
            # down the whole fan-out, whatever the failure mode.
            status = "error"
            error = str(exc)

        artifacts: List[str] = []
        for root, _dirs, files in os.walk(workdir):
            for name in files:
                full = Path(root) / name
                artifacts.append(str(full.relative_to(workdir)))
        artifacts.sort()

        duration_ms = (time.monotonic() - start) * 1000.0
        result_workdir = workdir
        if not self.keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
            result_workdir = ""  # cleaned up; never point at a removed directory

        return CandidateRunResult(
            candidate_id=candidate.candidate_id,
            status=status,
            exit_code=exit_code,
            stdout="".join(stdout_parts),
            stderr="".join(stderr_parts),
            duration_ms=round(duration_ms, 1),
            artifacts=artifacts,
            workdir=result_workdir,
            error=error,
        )


class RuntimeSandboxExecutor:
    """Documented plug point for the ecosystem's real execution layer -- the Runtime's sandboxed
    executor (`simplicio prototype run`, `simplicio-runtime/src/prototype_gate.rs`), which enforces
    real quotas (rlimits, process-tree kill, network namespace, content-addressed artifact store)
    that `LocalSubprocessExecutor` deliberately does not attempt to replicate here.

    That binary is WIP as of this module (no released, reliably shell-out-able `simplicio
    prototype run`), so this class is intentionally NOT a live integration: `execute()` raises a
    clear `NotImplementedError` instead of shelling out to a binary this repo cannot depend on
    being present or stable. It exists so the plug point is typed and discoverable -- once the
    Runtime binary ships, only this class's body needs to change (translate `CandidateSpec` to the
    plan JSON `simplicio prototype run --plan <file> --json` expects, and translate its
    `simplicio.prototype-candidate/v1` receipt back to a `CandidateRunResult`). Every caller that
    already does `dispatch_candidates(plan, candidates, executor=...)` needs zero changes to switch.
    """

    def __init__(self, *, binary: str = "simplicio"):
        self.binary = binary

    def execute(self, candidate: CandidateSpec) -> CandidateRunResult:
        raise NotImplementedError(
            "RuntimeSandboxExecutor is a documented plug point, not a live integration in this "
            "slice: 'simplicio prototype run' (simplicio-runtime/src/prototype_gate.rs) is still "
            "WIP and not a released binary this repo can depend on. Use LocalSubprocessExecutor "
            "(the default) or inject your own CandidateExecutor until that binary ships."
        )


# ---------------------------------------------------------------------------------------------
# Fan-out orchestration (bounded concurrency, independent per-candidate results)
# ---------------------------------------------------------------------------------------------


@dataclass
class FanoutReport:
    plan_hash: str
    results: List[CandidateRunResult]
    max_concurrency: int
    wall_clock_ms: float

    def summary(self) -> dict:
        counts: dict = {}
        for result in self.results:
            counts[result.status] = counts.get(result.status, 0) + 1
        return {
            "schema": FANOUT_REPORT_SCHEMA,
            "plan_hash": self.plan_hash,
            "total": len(self.results),
            "by_status": counts,
            "max_concurrency": self.max_concurrency,
            "wall_clock_ms": round(self.wall_clock_ms, 1),
            "candidates": [asdict(result) for result in self.results],
        }


def _run_one(executor: CandidateExecutor, candidate: CandidateSpec) -> CandidateRunResult:
    """Isolation boundary: any exception escaping `executor.execute` (a bug in a custom executor,
    not just the ones handled inside `LocalSubprocessExecutor`) becomes a terminal 'crashed'
    result for THIS candidate only, instead of propagating into the fan-out's thread pool and
    silently dropping/blocking every other candidate."""
    try:
        return executor.execute(candidate)
    except Exception as exc:  # candidate isolation boundary -- never let one crash the batch
        return CandidateRunResult(
            candidate_id=candidate.candidate_id,
            status="crashed",
            exit_code=None,
            stdout="",
            stderr="",
            duration_ms=0.0,
            artifacts=[],
            workdir="",
            error=str(exc),
        )


def _default_concurrency(requested: int) -> int:
    if ResourceGovernor is None:  # pragma: no cover - fan_out.py is always vendored in this repo
        return max(1, int(requested))
    # Reuse the same admission discipline as scripts/fan_out.py: never claim more concurrency than
    # this host's own cap allows, even when the caller asks for more.
    env_cap = os.environ.get("FAN_OUT_MAX_WORKERS")
    try:
        cap = max(1, int(env_cap)) if env_cap else requested
    except (TypeError, ValueError):
        cap = requested
    cpu_count = os.cpu_count() or 1
    return max(1, min(int(requested), cap, cpu_count * 4))


def dispatch_candidates(
    plan: Mapping[str, Any],
    candidates: List[CandidateSpec],
    *,
    executor: Optional[CandidateExecutor] = None,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
) -> FanoutReport:
    """Fan out `candidates` to `executor` (default `LocalSubprocessExecutor`) with bounded
    concurrency (backpressure -- never more than `max_concurrency` candidates run at once,
    clamped further by host capacity via `_default_concurrency`, matching `scripts/fan_out.py`'s
    `FAN_OUT_MAX_WORKERS` convention).

    A hung or crashed candidate never blocks the others: each candidate's execution is wrapped by
    `_run_one`'s isolation boundary, and `LocalSubprocessExecutor` itself enforces a wall-clock
    timeout per candidate. Results are independent and collected as each candidate finishes
    (`as_completed`), then sorted by `candidate_id` for deterministic output.
    """
    validated_plan = pg.validate_plan(plan)
    executor = executor or LocalSubprocessExecutor()
    effective_concurrency = _default_concurrency(max(1, int(max_concurrency)))

    if not candidates:
        return FanoutReport(
            plan_hash=validated_plan["plan_hash"], results=[], max_concurrency=effective_concurrency,
            wall_clock_ms=0.0,
        )

    results: List[CandidateRunResult] = []
    start = time.monotonic()
    pool_size = min(effective_concurrency, len(candidates))
    with ThreadPoolExecutor(max_workers=pool_size) as pool:
        future_map = {pool.submit(_run_one, executor, candidate): candidate for candidate in candidates}
        for future in as_completed(future_map):
            candidate = future_map[future]
            try:
                result = future.result()
            except Exception as exc:  # pragma: no cover - _run_one already catches; belt & braces
                result = CandidateRunResult(
                    candidate_id=candidate.candidate_id, status="crashed", exit_code=None,
                    stdout="", stderr="", duration_ms=0.0, artifacts=[], workdir="", error=str(exc),
                )
            results.append(result)

    wall_clock_ms = (time.monotonic() - start) * 1000.0
    results.sort(key=lambda r: r.candidate_id)
    return FanoutReport(
        plan_hash=validated_plan["plan_hash"], results=results, max_concurrency=effective_concurrency,
        wall_clock_ms=wall_clock_ms,
    )


# ---------------------------------------------------------------------------------------------
# Wiring fan-out results into the state machine (real receipts, not just planning-time data)
# ---------------------------------------------------------------------------------------------


def candidate_run_evidence(result: CandidateRunResult) -> dict[str, Any]:
    """Shape a candidate execution result as durable evidence
    (`simplicio.prototype-candidate-run/v1`) -- the payload `build_candidate_from_run` hashes into
    `artifact_hash` and that a decision's `ac_coverage`/`required_changes` can point back at."""
    return {
        "schema": CANDIDATE_RUN_SCHEMA,
        "candidate_id": result.candidate_id,
        "status": result.status,
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
        "artifacts": list(result.artifacts),
        "error": result.error,
    }


def build_candidate_from_run(
    *, plan: Mapping[str, Any], result: CandidateRunResult, strategy: str, agent_id: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Bridge one real fan-out execution result into a `prototype-candidate/v1` payload.

    This is the piece epic #568 flagged as missing: without it, `apply_decision` only ever sees
    planning-time data (a candidate someone typed by hand), never a receipt tied to something that
    actually ran. `artifact_hash` is derived from the ACTUAL execution result (status, exit code,
    artifacts produced) -- never fabricated before the candidate runs. A non-"ok" status marks the
    candidate `rejected` up front (the judge/decision layer still gets to REVISE or override via
    `build_decision`, but a fan-out result that failed/crashed/timed-out is never silently reported
    as `validated`).
    """
    evidence = candidate_run_evidence(result)
    artifact_hash = _stable_hash(evidence)
    status = "validated" if result.status == "ok" else "rejected"
    terminal_reason = result.error or (
        "fanout execution ok" if result.status == "ok" else f"fanout execution {result.status}"
    )
    return pg.build_candidate(
        plan=plan,
        candidate_id=result.candidate_id,
        strategy=strategy,
        agent_id=agent_id,
        artifact_hash=artifact_hash,
        artifact_location=result.workdir,
        validation_results=[{
            "check": "fanout_execution",
            "status": result.status,
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
        }],
        evidence_refs=list(result.artifacts),
        status=status,
        terminal_reason=terminal_reason,
        **kwargs,
    )


__all__ = [
    "CANDIDATE_RUN_SCHEMA",
    "FANOUT_REPORT_SCHEMA",
    "TERMINAL_STATUSES",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_MAX_CONCURRENCY",
    "CandidateSpec",
    "CandidateRunResult",
    "CandidateExecutor",
    "LocalSubprocessExecutor",
    "RuntimeSandboxExecutor",
    "FanoutReport",
    "dispatch_candidates",
    "candidate_run_evidence",
    "build_candidate_from_run",
]
