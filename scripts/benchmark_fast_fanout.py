"""Measure real Fast builds for shared versus independent fan-out slots."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import tempfile
import time
from pathlib import Path

from simplicio_loop.fast_fanout import FastFanoutCoordinator
from simplicio_loop.fast_integration import FastConfig, FastLoopIntegration

SCHEMA = "simplicio.loop-fast-fanout-benchmark/v1"

def _command() -> tuple[str, ...]:
    return tuple(shlex.split(os.environ.get("SIMPLICIO_FAST_COMMAND", "simplicio-fast")))

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

def benchmark(root: str | Path = ".", *, slots: int = 5, repeats: int = 10) -> dict:
    if slots < 1 or repeats < 1:
        raise ValueError("slots and repeats must be positive")
    root = Path(root).resolve()
    command = _command()
    with tempfile.TemporaryDirectory(prefix="simplicio-fast-fanout-", dir=str(root)) as directory:
        tmp = Path(directory)
        baseline = _independent(root, slots, repeats, tmp, command)
        shared = _shared(root, slots, repeats, tmp, command)
    return {"schema": SCHEMA, "root": str(root), "slots": slots,
            "repeats": repeats, "baseline": baseline, "shared": shared,
            "build_reduction_factor": baseline["builds"] / shared["builds"] if shared["builds"] else None,
            "wall_speedup": baseline["wall_ms"] / shared["wall_ms"] if shared["wall_ms"] else None,
            "ttft_ms": None, "tokens": None, "rss_mb": None,
            "page_faults": None,
            "null_metrics_reason": "No local LLM is used; TTFT/tokens are inapplicable, and child-process RSS/page-fault accounting is not portable on Windows.",
            "local_llm": False}

def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--slots", type=int, default=5)
    parser.add_argument("--repeats", type=int, default=10)
    args = parser.parse_args(argv)
    print(json.dumps(benchmark(args.root, slots=args.slots, repeats=args.repeats), ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
