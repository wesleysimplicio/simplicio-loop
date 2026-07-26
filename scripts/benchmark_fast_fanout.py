"""Measure real Fast builds for shared versus independent fan-out slots."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from simplicio_loop.fast_fanout import FastFanoutCoordinator
from simplicio_loop.fast_integration import FastConfig, FastLoopIntegration

SCHEMA = "simplicio.loop-fast-fanout-benchmark/v1"

def _command() -> tuple[str, ...]:
    return tuple(shlex.split(os.environ.get("SIMPLICIO_FAST_COMMAND", "simplicio-fast")))


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


class _LibraryFastIntegration:
    """Use the installed Fast core without paying CLI/Mapper startup per slot."""

    def __init__(self, root: Path, snapshot: Path) -> None:
        from simplicio_fast.snapshot import build_snapshot

        self.root = root
        self.snapshot = snapshot
        self._build_snapshot = build_snapshot

    def prepare(self, task: str) -> dict:
        metrics = self._build_snapshot(self.root, self.snapshot, timeout_seconds=180)
        generation = f"SFAST001:{metrics.generation}"
        context_hash = "sha256:" + hashlib.sha256(
            json.dumps({"generation": generation, "task": task}, sort_keys=True).encode()
        ).hexdigest()
        plan_hash = "sha256:" + hashlib.sha256(
            json.dumps({"generation": generation, "files": metrics.files}, sort_keys=True).encode()
        ).hexdigest()
        return {"status": "READY", "generation": generation,
                "context_hash": context_hash, "plan_hash": plan_hash,
                "metrics": asdict(metrics), "snapshot_sha256": _sha256(self.snapshot)}

    def apply(self, changeset, *, winner, generation, context_hash):
        return {"status": "READY", "applied": False, "reason": "benchmark_only"}

    def refresh(self):
        metrics = self._build_snapshot(self.root, self.snapshot, timeout_seconds=180)
        return {"status": "MEASURED", "generation": f"SFAST001:{metrics.generation}",
                "metrics": asdict(metrics)}

def _independent(root: Path, slots: int, repeats: int, tmp: Path, command: tuple[str, ...]) -> dict:
    started = time.perf_counter()
    builds = 0
    for repeat in range(repeats):
        for slot in range(slots):
            config = FastConfig(command=command, snapshot=str(tmp / f"baseline-{repeat}-{slot}.sfast"),
                                state=str(tmp / f"baseline-{repeat}-{slot}.json"), timeout_seconds=180)
            receipt = FastLoopIntegration(root, config=config).ingest()
            if receipt.get("fallback"):
                raise RuntimeError(str(receipt.get("reason") or "Fast baseline fallback"))
            builds += 1
    return {"wall_ms": (time.perf_counter() - started) * 1000, "builds": builds}

def _shared(root: Path, slots: int, repeats: int, tmp: Path, command: tuple[str, ...]) -> dict:
    started = time.perf_counter()
    builds = 0
    for repeat in range(repeats):
        config = FastConfig(command=command, snapshot=str(tmp / f"shared-{repeat}.sfast"),
                            state=str(tmp / f"shared-{repeat}.json"), timeout_seconds=180)
        integration = FastLoopIntegration(root, config=config)
        coordinator = FastFanoutCoordinator(root, integration=integration)
        receipt = coordinator.prepare("benchmark shared Fast context across fan-out slots")
        if receipt.get("status") not in {"MEASURED", "REUSED"}:
            raise RuntimeError(str(receipt))
        for slot in range(slots):
            coordinator.acquire_slot(f"slot-{slot}", overlay_tree_hash=f"tree-{slot}")
        builds += coordinator.status()["metrics"]["canonical_builds"]
    return {"wall_ms": (time.perf_counter() - started) * 1000, "builds": builds}


def _library_independent(root: Path, slots: int, repeats: int, tmp: Path) -> dict:
    started = time.perf_counter()
    cpu_started = time.process_time()
    builds = 0
    rows = []
    from simplicio_fast.snapshot import build_snapshot

    for repeat in range(repeats):
        for slot in range(slots):
            snapshot = tmp / f"library-baseline-{repeat}-{slot}.sfast"
            metrics = build_snapshot(root, snapshot, timeout_seconds=180)
            rows.append({"repeat": repeat, "slot": slot, "metrics": asdict(metrics),
                         "snapshot_sha256": _sha256(snapshot)})
            builds += 1
    return {"wall_ms": (time.perf_counter() - started) * 1000,
            "cpu_ms": (time.process_time() - cpu_started) * 1000,
            "builds": builds, "runs": rows}


def _library_shared(root: Path, slots: int, repeats: int, tmp: Path) -> dict:
    started = time.perf_counter()
    cpu_started = time.process_time()
    builds = 0
    rows = []
    for repeat in range(repeats):
        snapshot = tmp / f"library-shared-{repeat}.sfast"
        integration = _LibraryFastIntegration(root, snapshot)
        coordinator = FastFanoutCoordinator(root, integration=integration)
        prepared = coordinator.prepare("benchmark shared Fast context across fan-out slots")
        for slot in range(slots):
            coordinator.acquire_slot(f"slot-{slot}", overlay_tree_hash=f"tree-{slot}")
        builds += coordinator.status()["metrics"]["canonical_builds"]
        rows.append({"repeat": repeat, "prepared": prepared,
                     "snapshot_sha256": _sha256(snapshot)})
    return {"wall_ms": (time.perf_counter() - started) * 1000,
            "cpu_ms": (time.process_time() - cpu_started) * 1000,
            "builds": builds, "runs": rows}

def benchmark(root: str | Path = ".", *, slots: int = 5, repeats: int = 10,
              engine: str = "library") -> dict:
    if slots < 1 or repeats < 1:
        raise ValueError("slots and repeats must be positive")
    root = Path(root).resolve()
    command = _command()
    with tempfile.TemporaryDirectory(prefix="simplicio-fast-fanout-", dir=str(root)) as directory:
        tmp = Path(directory)
        if engine == "library":
            baseline = _library_independent(root, slots, repeats, tmp)
            shared = _library_shared(root, slots, repeats, tmp)
        elif engine == "cli":
            baseline = _independent(root, slots, repeats, tmp, command)
            shared = _shared(root, slots, repeats, tmp, command)
        else:
            raise ValueError("engine must be library or cli")
    baseline_hashes = {row.get("snapshot_sha256") for row in baseline.get("runs", [])
                       if row.get("snapshot_sha256")}
    shared_hashes = {row.get("snapshot_sha256") for row in shared.get("runs", [])
                     if row.get("snapshot_sha256")}
    return {"schema": SCHEMA, "root": str(root), "slots": slots,
            "repeats": repeats, "engine": engine, "baseline": baseline, "shared": shared,
            "build_reduction_factor": baseline["builds"] / shared["builds"] if shared["builds"] else None,
            "wall_speedup": baseline["wall_ms"] / shared["wall_ms"] if shared["wall_ms"] else None,
            "functional_equivalence": bool(baseline_hashes and shared_hashes and baseline_hashes == shared_hashes),
            "ttft_ms": None, "tokens": None, "rss_mb": None,
            "page_faults": None,
            "null_metrics_reason": "No local LLM is used; TTFT/tokens are inapplicable, and child-process RSS/page-fault accounting is not portable on Windows.",
            "local_llm": False}

def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--slots", type=int, default=5)
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--engine", choices=("library", "cli"), default="library")
    args = parser.parse_args(argv)
    print(json.dumps(benchmark(args.root, slots=args.slots, repeats=args.repeats, engine=args.engine),
                     ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
