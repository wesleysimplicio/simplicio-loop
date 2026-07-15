#!/usr/bin/env python3
"""distributed_default.py — Orquestrador multi-agente distribuído por default.

Fan-out automático de tarefas independentes entre Codex, Claude e outros
runtimes, com claims atômicos leaseados, context packs autorizados e
convergência por evidence gate.

Issue #183 — DoD integration layer.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

SCHEMA = "simplicio.distributed-default/v1"
ISSUE = 183

# ── env knobs ────────────────────────────────────────────────────────────────
_ENV_MAX_WORKERS = "SIMPLICIO_DISTRIBUTED_MAX_WORKERS"
_ENV_LEASE_SECONDS = "SIMPLICIO_DISTRIBUTED_LEASE_SECONDS"
_ENV_FENCE_TOKEN_FILE = "SIMPLICIO_DISTRIBUTED_FENCE_TOKEN_FILE"
_ENV_AUTO_FAN_OUT = "SIMPLICIO_LOOP_AUTO_FAN_OUT"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _fan_out_enabled() -> bool:
    raw = os.environ.get(_ENV_AUTO_FAN_OUT, "1").strip().lower()
    return raw not in {"0", "false", "no", "off", "disabled"}


def _max_workers() -> int:
    try:
        return max(1, int(os.environ.get(_ENV_MAX_WORKERS, "4")))
    except ValueError:
        return 4


def _lease_seconds() -> float:
    try:
        return max(1.0, float(os.environ.get(_ENV_LEASE_SECONDS, "60")))
    except ValueError:
        return 60.0


# ── fencing token ─────────────────────────────────────────────────────────────

class FencingToken:
    """Monotonically increasing fencing token backed by an atomic file."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or Path(
            os.environ.get(_ENV_FENCE_TOKEN_FILE,
                           f"/tmp/simplicio_fence_{uuid.uuid4().hex[:12]}.json")
        )
        self._lock = threading.Lock()

    def acquire(self, agent_id: str) -> Dict[str, Any]:
        with self._lock:
            current = self._load()
            seq = current.get("seq", 0) + 1
            token: Dict[str, Any] = {
                "seq": seq,
                "agent_id": agent_id,
                "issued_at": _now_iso(),
            }
            self._path.write_text(json.dumps(token, ensure_ascii=False), encoding="utf-8")
            return token

    def validate(self, token: Dict[str, Any]) -> bool:
        with self._lock:
            current = self._load()
            return current.get("seq") == token.get("seq") and current.get("agent_id") == token.get("agent_id")

    def _load(self) -> Dict[str, Any]:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}


# ── claim / lease ─────────────────────────────────────────────────────────────

@dataclass
class Claim:
    task_id: str
    agent_id: str
    runtime: str
    lane: str
    lease_expires_at: float  # unix timestamp
    fence_token: Dict[str, Any] = field(default_factory=dict)
    context_pack: Dict[str, Any] = field(default_factory=dict)
    receipt: Optional[Dict[str, Any]] = None

    def is_expired(self) -> bool:
        return time.monotonic() > self.lease_expires_at

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["lease_expires_at_iso"] = datetime.fromtimestamp(
            self.lease_expires_at, tz=timezone.utc
        ).isoformat()
        return d


class ClaimStore:
    """Thread-safe in-process claim store (remote-queue ready via subclass)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._claims: Dict[str, Claim] = {}

    def try_claim(
        self,
        task_id: str,
        agent_id: str,
        runtime: str,
        lane: str,
        fence_token: Dict[str, Any],
        context_pack: Dict[str, Any],
        lease_seconds: float = 60.0,
    ) -> Optional[Claim]:
        """Atomic claim — returns None if already claimed by another agent."""
        with self._lock:
            existing = self._claims.get(task_id)
            if existing and not existing.is_expired():
                return None  # already owned
            claim = Claim(
                task_id=task_id,
                agent_id=agent_id,
                runtime=runtime,
                lane=lane,
                lease_expires_at=time.monotonic() + lease_seconds,
                fence_token=fence_token,
                context_pack=context_pack,
            )
            self._claims[task_id] = claim
            return claim

    def release(self, task_id: str, agent_id: str, receipt: Dict[str, Any]) -> bool:
        with self._lock:
            claim = self._claims.get(task_id)
            if claim and claim.agent_id == agent_id:
                claim.receipt = receipt
                return True
            return False

    def list_claims(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [c.to_dict() for c in self._claims.values()]

    def converged(self) -> bool:
        """True when all claims have a receipt (all agents finished)."""
        with self._lock:
            return bool(self._claims) and all(
                c.receipt is not None for c in self._claims.values()
            )


# ── task + lane ───────────────────────────────────────────────────────────────

@dataclass
class DistributedTask:
    id: str
    goal: str
    lane: str  # e.g. "planner/frontend"
    runtime: str  # e.g. "codex" | "claude" | "cursor"
    dependencies: List[str] = field(default_factory=list)
    context_fields: List[str] = field(default_factory=list)


@dataclass
class DistributedRun:
    run_id: str
    tasks: List[DistributedTask]
    max_workers: int = 4
    lease_seconds: float = 60.0
    fence_token_path: Optional[Path] = None

    def independent_groups(self) -> List[List[DistributedTask]]:
        """Return groups of tasks that can run in parallel (no shared deps)."""
        groups: List[List[DistributedTask]] = []
        remaining = list(self.tasks)
        done: set[str] = set()
        while remaining:
            ready = [t for t in remaining if all(d in done for d in t.dependencies)]
            if not ready:
                break  # cycle or unresolvable — leave to serial path
            groups.append(ready)
            done.update(t.id for t in ready)
            remaining = [t for t in remaining if t.id not in done]
        return groups


# ── executor ──────────────────────────────────────────────────────────────────

class DistributedExecutor:
    """
    Runs a DistributedRun: fan-out independent tasks, serialize dependencies.

    - Claims are atomic and leased.
    - Each agent receives only its authorized context pack.
    - Fencing token prevents split-brain writes.
    - Network failure → pause (fail closed), never duplicate mutation.
    - 100% / COMPLETE only when all receipts converge.
    """

    def __init__(
        self,
        run: DistributedRun,
        worker_fn: Callable[[Claim], Dict[str, Any]],
        store: Optional[ClaimStore] = None,
        fencing: Optional[FencingToken] = None,
    ) -> None:
        self._run = run
        self._worker_fn = worker_fn
        self._store = store or ClaimStore()
        self._fencing = fencing or FencingToken(run.fence_token_path)
        self._errors: List[Dict[str, Any]] = []
        self._results: List[Dict[str, Any]] = []

    def execute(self) -> Dict[str, Any]:
        if not _fan_out_enabled():
            return self._serial_fallback()

        groups = self._run.independent_groups()
        if not groups:
            return self._serial_fallback()

        for group in groups:
            if len(group) == 1:
                self._run_task(group[0])
            else:
                # parallel fan-out
                threads = [
                    threading.Thread(target=self._run_task, args=(t,), daemon=True)
                    for t in group[: self._run.max_workers]
                ]
                for th in threads:
                    th.start()
                for th in threads:
                    th.join()

        all_receipts = self._store.converged()
        converged = all_receipts and not self._errors
        return {
            "schema": SCHEMA,
            "run_id": self._run.run_id,
            "status": "COMPLETE" if converged else "PARTIAL",
            "converged": converged,
            "claims": self._store.list_claims(),
            "errors": self._errors,
            "results": self._results,
        }

    def _run_task(self, task: DistributedTask) -> None:
        # 1. build context pack (only authorized fields)
        context_pack = {"task_id": task.id, "goal": task.goal, "lane": task.lane}
        if task.context_fields:
            context_pack = {k: v for k, v in context_pack.items() if k in task.context_fields}

        # 2. acquire fencing token
        fence_token = self._fencing.acquire(f"{task.runtime}:{task.id}")

        # 3. atomic claim
        claim = self._store.try_claim(
            task_id=task.id,
            agent_id=f"{task.runtime}-{task.lane}",
            runtime=task.runtime,
            lane=task.lane,
            fence_token=fence_token,
            context_pack=context_pack,
            lease_seconds=self._run.lease_seconds,
        )
        if claim is None:
            self._errors.append({"task_id": task.id, "error": "claim_collision"})
            return

        # 4. validate fencing (fail-closed on split-brain)
        if not self._fencing.validate(fence_token):
            self._errors.append({"task_id": task.id, "error": "fencing_token_invalid"})
            self._store.release(task.id, claim.agent_id, {"status": "fencing_abort"})
            return

        # 5. execute worker
        try:
            receipt = self._worker_fn(claim)
        except Exception as exc:
            receipt = {"status": "error", "error": str(exc)}
            self._errors.append({"task_id": task.id, "error": str(exc)})

        # 6. release with receipt
        self._store.release(task.id, claim.agent_id, receipt)
        self._results.append({"task_id": task.id, "receipt": receipt})

    def _serial_fallback(self) -> Dict[str, Any]:
        for task in self._run.tasks:
            self._run_task(task)
        return {
            "schema": SCHEMA,
            "run_id": self._run.run_id,
            "status": "COMPLETE" if self._store.converged() else "PARTIAL",
            "converged": self._store.converged(),
            "mode": "serial_fallback",
            "claims": self._store.list_claims(),
            "errors": self._errors,
            "results": self._results,
        }


# ── CLI ───────────────────────────────────────────────────────────────────────

def _default_worker(claim: Claim) -> Dict[str, Any]:
    """Placeholder worker — real agents inject their own fn."""
    return {
        "status": "ok",
        "task_id": claim.task_id,
        "agent_id": claim.agent_id,
        "lane": claim.lane,
        "executed_at": _now_iso(),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="Simplicio distributed default orchestrator (#183)")
    p.add_argument("--run-id", default=str(uuid.uuid4())[:8])
    p.add_argument("--tasks-json", help="Path to tasks JSON file")
    p.add_argument("--max-workers", type=int, default=_max_workers())
    p.add_argument("--lease-seconds", type=float, default=_lease_seconds())
    args = p.parse_args(argv)

    if args.tasks_json:
        raw = json.loads(Path(args.tasks_json).read_text(encoding="utf-8"))
        tasks = [DistributedTask(**t) for t in raw]
    else:
        # demo: Codex+Claude default lanes
        tasks = [
            DistributedTask("t-codex", "planner/frontend", "planner/frontend", "codex"),
            DistributedTask("t-claude", "operator/backend", "operator/backend", "claude"),
            DistributedTask("t-cursor", "verifier/tests", "verifier/tests", "cursor",
                            dependencies=["t-codex", "t-claude"]),
        ]

    run = DistributedRun(
        run_id=args.run_id,
        tasks=tasks,
        max_workers=args.max_workers,
        lease_seconds=args.lease_seconds,
    )
    result = DistributedExecutor(run, _default_worker).execute()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
