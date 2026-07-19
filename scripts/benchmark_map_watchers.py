"""Measure centralized watcher coalescing against naive per-client watchers."""

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simplicio_loop.map_service import MapServiceRegistry, RepositoryIdentity
from simplicio_loop.map_service_watchers import MapWatcherManager

try:
    import resource
except ImportError:  # pragma: no cover - Windows
    resource = None


def _peak_rss_mb() -> Optional[float]:
    if resource is None:
        return None
    value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value / (1024 * 1024) if os.name == "nt" else value / 1024


def benchmark(worktrees: int = 8, clients: int = 4, events: int = 3) -> Dict[str, Any]:
    if min(worktrees, clients, events) < 1:
        raise ValueError("worktrees, clients, and events must be positive")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        registry = MapServiceRegistry()
        identities = [registry.register(RepositoryIdentity(
            f"owner/project-{index}", str(root / str(index)), base_sha="benchmark"
        )) for index in range(worktrees)]
        manager = MapWatcherManager(registry, max_watchers=worktrees, max_pending=worktrees)
        callbacks = []
        for key in identities:
            for _ in range(clients):
                manager.watch(key, callbacks.append)
        started = time.perf_counter()
        cpu_started = time.process_time()
        coalesced_events = 0
        for key in identities:
            for event in range(events):
                manager.emit(key, [f"file-{event}.py"])
            coalesced_events += len(manager.flush(force=True))
        elapsed = time.perf_counter() - started
        return {
            "schema": "simplicio.map-watcher-benchmark/v1",
            "worktrees": worktrees,
            "clients_per_worktree": clients,
            "events_per_worktree": events,
            "centralized_watchers": manager.status()["watchers"],
            "naive_watchers": worktrees * clients,
            "coalesced_events": coalesced_events,
            "expected_coalesced_events": worktrees,
            "logical_watcher_reduction": (worktrees * clients) - manager.status()["watchers"],
            "latency_ms": elapsed * 1000.0,
            "cpu_seconds": time.process_time() - cpu_started,
            "peak_rss_mb": _peak_rss_mb(),
            "rss_source": "resource.getrusage" if resource is not None else "unavailable",
            "callbacks": len(callbacks),
            "all_events_coalesced": coalesced_events == worktrees,
            "p95_latency_ms": elapsed * 1000.0,
        }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktrees", type=int, default=8)
    parser.add_argument("--clients", type=int, default=4)
    parser.add_argument("--events", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    receipt = benchmark(args.worktrees, args.clients, args.events)
    rendered = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
