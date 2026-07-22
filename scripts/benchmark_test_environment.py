#!/usr/bin/env python3
"""Microbenchmark for allocation validation and receipt generation (#620)."""
import tempfile
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from simplicio_loop.test_environment import REQUEST_SCHEMA, ServiceDefinition, TestEnvironmentHub


def main(iterations=1000):
    request = {"schema": REQUEST_SCHEMA, "identity": {"run_id": "r", "task_id": "t", "attempt_id": "a", "fence": "1"},
               "production": False, "network_policy": "offline", "services": [{"name": "db", "version": "1"}]}
    with tempfile.TemporaryDirectory() as root:
        hub = TestEnvironmentHub(root, services={"db": ServiceDefinition("db", ("1",), lambda i, p, r: ("true",))})
        started = time.perf_counter()
        for _ in range(iterations): hub.validate(request)
        elapsed = time.perf_counter() - started
    print(f"validation iterations={iterations} total_ms={elapsed*1000:.3f} ops_per_second={iterations/elapsed:.0f}")


if __name__ == "__main__": main()
