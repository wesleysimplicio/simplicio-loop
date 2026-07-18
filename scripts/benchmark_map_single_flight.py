"""Measure shared map builds, handles, and resource receipts for issue #512."""

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simplicio_loop.map_service import MapServiceRegistry, RepositoryIdentity
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


def _p95(values):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))]


async def _run(clients: int, repeats: int) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        registry = MapServiceRegistry()
        identity_key = registry.register(
            RepositoryIdentity("owner/project", str(root), base_sha="benchmark")
        )
        store = SingleFlightMapStore(registry)
        builder_calls = 0
        latencies = []
        cpu_started = time.process_time()
        rss_before = _peak_rss_mb()

        async def builder():
            nonlocal builder_calls
            builder_calls += 1
            await asyncio.sleep(0)
            return registry.build_canonical(
                identity_key, tree_hash=f"tree-{builder_calls}",
                files=[str(root / "project-map.json")],
            )

        for _ in range(repeats):
            started = time.perf_counter()
            handles = await asyncio.gather(*[
                store.get_or_build(
                    identity_key, mode="canonical", tree_hash=f"tree-{builder_calls + 1}",
                    builder=builder,
                )
                for _ in range(clients)
            ])
            latencies.append((time.perf_counter() - started) * 1000.0)
            assert len({handle.cache_key for handle in handles}) == 1
            for handle in handles:
                handle.release()
            store.invalidate(identity_key, reason="benchmark-next-round")
            store.gc()

        elapsed = sum(latencies) / 1000.0
        cpu_seconds = time.process_time() - cpu_started
        return {
            "schema": "simplicio.map-single-flight-benchmark/v1",
            "clients": clients,
            "repeats": repeats,
            "builder_calls": builder_calls,
            "expected_builds": repeats,
            "logical_io_operations": builder_calls,
            "latency_ms_mean": statistics.mean(latencies),
            "latency_ms_p95": _p95(latencies),
            "cpu_seconds": cpu_seconds,
            "peak_rss_mb": _peak_rss_mb() or rss_before,
            "rss_source": "resource.getrusage" if resource is not None else "unavailable",
            "all_clients_shared_snapshot": builder_calls == repeats,
            "elapsed_seconds": elapsed,
        }


def benchmark(clients: int = 24, repeats: int = 5) -> Dict[str, Any]:
    if clients < 1 or repeats < 1:
        raise ValueError("clients and repeats must be positive")
    return asyncio.run(_run(clients, repeats))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", type=int, default=24)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    receipt = benchmark(args.clients, args.repeats)
    rendered = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
