"""Small deterministic #888 broker binding benchmark."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from simplicio_loop.checkpoint_lifecycle import CheckpointLifecycle
from simplicio_loop.fast_fanout import CanonicalGeneration
from simplicio_loop.generation_broker import GenerationBroker
from simplicio_loop.map_service import MapServiceRegistry, RepositoryIdentity


def main(candidate_count: int = 100) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        base = root / "base"
        base.mkdir()
        registry = MapServiceRegistry()
        identity = RepositoryIdentity("owner/project", str(base), base_sha="abc")
        identity_key = registry.register(identity)
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
        started = time.perf_counter_ns()
        bindings = [
            broker.bind(
                identity_key,
                tree_hash="tree",
                files=["a.py"],
                candidate_id=f"candidate-{index}",
                generation=generation,
            )
            for index in range(candidate_count)
        ]
        elapsed_ns = time.perf_counter_ns() - started
        print(
            json.dumps(
                {
                    "candidate_count": candidate_count,
                    "elapsed_ns": elapsed_ns,
                    "ns_per_binding": elapsed_ns // candidate_count,
                    "unique_canonical_cache_keys": len(
                        {item.canonical_cache_key for item in bindings}
                    ),
                    "unique_overlays": len({item.overlay_path for item in bindings}),
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
