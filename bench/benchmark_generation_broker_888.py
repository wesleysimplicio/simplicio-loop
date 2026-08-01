"""Cold/warm/incremental #888 broker benchmark with a signed receipt."""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
import tracemalloc
from pathlib import Path

from simplicio_loop.checkpoint_lifecycle import CheckpointLifecycle
from simplicio_loop.fast_fanout import CanonicalGeneration
from simplicio_loop.generation_broker import GenerationBroker
from simplicio_loop.map_service import MapServiceRegistry, RepositoryIdentity


def _digest(value: dict[str, object]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _rss_bytes(fallback: int) -> tuple[int, str]:
    try:
        import psutil  # type: ignore[import-not-found]

        return int(psutil.Process().memory_info().rss), "psutil"
    except (ImportError, OSError):
        return fallback, "tracemalloc-fallback"


def main(candidate_count: int = 100) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        base = root / "base"
        base.mkdir()
        registry = MapServiceRegistry()
        identity_key = registry.register(
            RepositoryIdentity("owner/project", str(base), base_sha="abc")
        )
        generation = CanonicalGeneration("generation-1", "ctx", "abc", "plan", "receipt")
        lifecycle = CheckpointLifecycle(
            root / "runs",
            task_id="benchmark-888",
            attempt_id="attempt-1",
            source_commit="abc",
            fast_generation=generation.generation,
            base_path=base,
        )
        broker = GenerationBroker(registry, lifecycle)
        tracemalloc.start()
        cold_started = time.perf_counter_ns()
        broker.bind(
            identity_key, tree_hash="tree", files=["a.py"], candidate_id="candidate-0", generation=generation
        )
        cold_ns = time.perf_counter_ns() - cold_started
        warm_started = time.perf_counter_ns()
        for index in range(1, candidate_count):
            broker.bind(
                identity_key,
                tree_hash="tree",
                files=["a.py"],
                candidate_id=f"candidate-{index}",
                generation=generation,
            )
        warm_ns = time.perf_counter_ns() - warm_started
        incremental_started = time.perf_counter_ns()
        broker.bind(
            identity_key,
            tree_hash="tree-2",
            files=["a.py", "b.py"],
            candidate_id="incremental",
            generation=generation,
        )
        incremental_ns = time.perf_counter_ns() - incremental_started
        _, peak_rss_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        peak_rss_bytes, rss_source = _rss_bytes(peak_rss_bytes)
        manifests = list(lifecycle.overlays.glob("*/generation-binding.json"))
        overlay_bytes = sum(path.stat().st_size for path in manifests)
        estimated_rebuild_ns = cold_ns * candidate_count
        measured_ns = cold_ns + warm_ns
        receipt: dict[str, object] = {
            "schema": "simplicio.loop.generation-broker-benchmark/v1",
            "candidate_count": candidate_count,
            "cold_ns": cold_ns,
            "warm_total_ns": warm_ns,
            "warm_ns_per_binding": warm_ns // max(1, candidate_count - 1),
            "incremental_ns": incremental_ns,
            "peak_rss_bytes": peak_rss_bytes,
            "rss_source": rss_source,
            "mapped_bytes": overlay_bytes,
            "overlay_bytes_per_slot": overlay_bytes // len(manifests),
            "estimated_time_saved_ns": max(0, estimated_rebuild_ns - measured_ns),
            "canonical_builds": broker.status()["metrics"]["cache_misses"],
            "cache_hits": broker.status()["metrics"]["cache_hits"],
        }
        receipt["receipt_hash"] = _digest(receipt)
        print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
