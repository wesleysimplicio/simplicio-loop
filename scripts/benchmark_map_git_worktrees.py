"""Measure N REAL git worktrees sharing one canonical build vs N independent full
remaps, for the #512/#513 "N worktrees vs full remap" acceptance criterion.

Unlike scripts/benchmark_map_single_flight.py (synthetic in-memory identity/tree_hash),
this creates actual `git worktree add` checkouts and resolves their identity/tree hash
via simplicio_loop.map_service_git — real subprocess calls, real files.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simplicio_loop.map_service import MapServiceRegistry
from simplicio_loop.map_service_git import real_tree_snapshot, resolve_repository_identity
from simplicio_loop.map_service_single_flight import SingleFlightMapStore

try:
    import resource
except ImportError:  # pragma: no cover - Windows
    resource = None


def _peak_rss_mb() -> Optional[float]:
    if resource is None:
        return None
    value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value / (1024 * 1024) if os.name == "nt" else value / 1024


def _git(*args: str, cwd: str) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), result.stderr))


def _make_real_worktrees(root: Path, count: int) -> list:
    main_root = root / "main"
    main_root.mkdir()
    _git("init", "-q", cwd=str(main_root))
    _git("config", "user.email", "bench@example.com", cwd=str(main_root))
    _git("config", "user.name", "Bench", cwd=str(main_root))
    (main_root / "project-map.json").write_text('{"files": []}\n', encoding="utf-8")
    _git("add", "project-map.json", cwd=str(main_root))
    _git("commit", "-q", "-m", "initial", cwd=str(main_root))

    worktrees = [str(main_root)]
    for i in range(count - 1):
        wt = root / ("wt-%d" % i)
        _git("worktree", "add", "-q", str(wt), "-b", "wt-branch-%d" % i, cwd=str(main_root))
        worktrees.append(str(wt))
    return worktrees


async def _run(worktree_count: int, repeats: int) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        worktrees = _make_real_worktrees(Path(directory), worktree_count)
        registry = MapServiceRegistry()
        store = SingleFlightMapStore(registry)

        identities = [resolve_repository_identity(wt) for wt in worktrees]
        for identity in identities:
            registry.register(identity)
        # All worktrees are at the same commit with no further edits, so they share one
        # real tree hash - a genuinely equivalent canonical build request from every
        # worktree's perspective, exactly like N IDEs opening the same checkout state.
        tree_hash, files = real_tree_snapshot(worktrees[0])
        for wt in worktrees[1:]:
            assert real_tree_snapshot(wt)[0] == tree_hash, "fixture invariant: same commit, same tree"

        single_flight_builds = 0
        single_flight_latencies = []

        async def shared_builder():
            nonlocal single_flight_builds
            single_flight_builds += 1
            await asyncio.sleep(0)
            return registry.build_canonical(identities[0].key, tree_hash=tree_hash, files=files)

        for _ in range(repeats):
            started = time.perf_counter()
            handles = await asyncio.gather(*[
                store.get_or_build(
                    identities[0].key, mode="canonical", tree_hash=tree_hash, files=files,
                    builder=shared_builder,
                )
                for _ in worktrees
            ])
            single_flight_latencies.append((time.perf_counter() - started) * 1000.0)
            assert len({h.cache_key for h in handles}) == 1
            for h in handles:
                h.release()
            store.invalidate(identities[0].key, reason="benchmark-next-round")
            store.gc()

        # Naive baseline: what N independent clients with NO dedup would actually cost -
        # each resolves its own real git identity/tree hash again from scratch (real
        # subprocess calls) and each performs its own real "build".
        naive_started = time.perf_counter()
        naive_builds = 0
        for _ in range(repeats):
            for wt in worktrees:
                resolve_repository_identity(wt)  # real git subprocess calls, discarded
                real_tree_snapshot(wt)
                naive_builds += 1
        naive_elapsed_ms = (time.perf_counter() - naive_started) * 1000.0

        return {
            "schema": "simplicio.map-git-worktree-benchmark/v1",
            "worktree_count": worktree_count,
            "repeats": repeats,
            "single_flight": {
                "logical_builds": single_flight_builds,
                "expected_builds": repeats,
                "shared_snapshot_every_round": single_flight_builds == repeats,
                "latency_ms_mean": sum(single_flight_latencies) / len(single_flight_latencies),
            },
            "naive_full_remap": {
                "logical_builds": naive_builds,
                "elapsed_ms": naive_elapsed_ms,
            },
            "build_reduction_factor": naive_builds / max(1, single_flight_builds),
            "peak_rss_mb": _peak_rss_mb(),
            "rss_source": "resource.getrusage" if resource is not None else "unavailable",
        }


def benchmark(worktree_count: int = 8, repeats: int = 3) -> Dict[str, Any]:
    if worktree_count < 2 or repeats < 1:
        raise ValueError("worktree_count must be >=2 and repeats must be positive")
    return asyncio.run(_run(worktree_count, repeats))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktrees", type=int, default=8)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    receipt = benchmark(args.worktrees, args.repeats)
    rendered = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
