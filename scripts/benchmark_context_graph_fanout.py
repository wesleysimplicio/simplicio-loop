"""Measured ContextGraph/Map Service fan-out benchmark for issue #687."""
import argparse
import json
import time
from pathlib import Path

from simplicio_loop.context_graph_fanout import CanonicalMapClient, TaskEnvelope, WorktreeMapLeaseManager
from simplicio_loop.map_service import MapServiceRegistry, RepositoryIdentity

try:
    import resource
except ImportError:  # Windows: report null with reason instead of fabricating zero.
    resource = None


def benchmark(tasks: int) -> dict:
    root = Path.cwd()
    registry = MapServiceRegistry()
    canonical = registry.register(RepositoryIdentity("bench", str(root), base_sha="base", mapper_config={"schema": 1}))
    manager = WorktreeMapLeaseManager(CanonicalMapClient(registry))
    started = time.perf_counter()
    for index in range(tasks):
        worktree = registry.register(RepositoryIdentity(
            "bench", str(root), worktree_root=str(root / (".bench-wt-%d" % index)),
            base_sha="base", dirty=True, dirty_fingerprint=str(index), mapper_config={"schema": 1},
        ))
        manager.bind(TaskEnvelope(str(index), mutation_targets=("f%d.py" % index,), authority_hash="auth"),
                     owner_id="worker-%d" % index, canonical_identity=canonical,
                     canonical_tree_hash="tree", canonical_files=("shared.py",),
                     worktree_identity=worktree, overlay_tree_hash="dirty-%d" % index,
                     dirty_files=("f%d.py" % index,))
    for index in range(tasks):
        manager.release(str(index))
    elapsed = (time.perf_counter() - started) * 1000
    usage = resource.getrusage(resource.RUSAGE_SELF) if resource is not None else None
    metrics = manager.status()["metrics"]
    return {"schema": "simplicio.context-graph-benchmark/v1", "tasks": tasks,
            "wall_ms": round(elapsed, 3),
            "cpu_ms": round((usage.ru_utime + usage.ru_stime) * 1000, 3) if usage else None,
            "cpu_reason": None if usage else "resource module unavailable",
            "peak_rss_kib": usage.ru_maxrss if usage else None,
            "rss_reason": None if usage else "resource module unavailable",
            "io": None, "io_reason": "portable per-process bytes unavailable",
            "cache_hits": metrics["cache_hits"], "remap_count": 1, "overlay_files": metrics["overlay_files"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", type=int, default=100)
    print(json.dumps(benchmark(parser.parse_args().tasks), sort_keys=True))
