"""Local benchmark for issue #691's bounded workspace session slice.

This uses a deterministic in-process transport seam so it measures bridge
admission and scheduling rather than an installed Runtime's workload.
"""

from __future__ import annotations

import json
import statistics
import sys
import threading
import time
import tracemalloc
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from simplicio_loop.runtime_bridge import RuntimeBridge


class _TimedProcess:
    def __init__(self, latency: float) -> None:
        self.process = self
        self.latency = latency

    def poll(self):
        return None

    def call_tool(self, *_args, **_kwargs):
        time.sleep(self.latency)
        return {"ok": True}

    def close(self) -> None:
        return None


def _bridge(root: Path, latency: float) -> RuntimeBridge:
    bridge = RuntimeBridge(binary="unused")
    processes = {}

    def process_for(path: Path):
        return processes.setdefault(str(path), _TimedProcess(latency))

    bridge._process_for_workspace = process_for  # type: ignore[method-assign]
    return bridge


def _serial(bridge: RuntimeBridge, workspaces: list[Path], count: int) -> float:
    started = time.perf_counter()
    for workspace in workspaces:
        for index in range(count):
            bridge.runtime_call(str(workspace), "simplicio_status", {},
                                idempotency_key=f"serial-{workspace.name}-{index}")
    return (time.perf_counter() - started) * 1000


def _parallel(bridge: RuntimeBridge, workspaces: list[Path], count: int) -> tuple[float, list[float]]:
    durations: list[float] = []
    lock = threading.Lock()

    def worker(workspace: Path) -> None:
        for index in range(count):
            started = time.perf_counter()
            bridge.runtime_call(str(workspace), "simplicio_status", {},
                                idempotency_key=f"parallel-{workspace.name}-{index}")
            with lock:
                durations.append((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(workspace,)) for workspace in workspaces]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return (time.perf_counter() - started) * 1000, durations


def main() -> None:
    with TemporaryDirectory(prefix="runtime-bridge-bench-") as directory:
        root = Path(directory)
        workspaces = [root / "workspace-a", root / "workspace-b"]
        for workspace in workspaces:
            workspace.mkdir()
        count = 20
        latency = 0.002
        tracemalloc.start()
        baseline_bridge = _bridge(root, latency)
        serial_ms = _serial(baseline_bridge, workspaces, count)
        parallel_bridge = _bridge(root, latency)
        parallel_ms, durations = _parallel(parallel_bridge, workspaces, count)
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        result = {
            "schema": "simplicio.runtime-bridge-benchmark/v1",
            "workspaces": len(workspaces),
            "calls_per_workspace": count,
            "transport_latency_ms": latency * 1000,
            "serial_baseline_ms": round(serial_ms, 3),
            "bounded_parallel_ms": round(parallel_ms, 3),
            "throughput_speedup": round(serial_ms / parallel_ms, 3),
            "call_p50_ms": round(statistics.median(durations), 3),
            "call_p95_ms": round(sorted(durations)[int(len(durations) * 0.95) - 1], 3),
            "peak_tracemalloc_bytes": peak_bytes,
            "session_status": parallel_bridge.status(),
        }
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
