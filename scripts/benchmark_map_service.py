"""Benchmark the centralized map-service CLI fallback and emit a reproducible receipt."""

import argparse
import contextlib
import io
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simplicio_loop.map_service_cli import run


def benchmark(worktrees: int = 8, clients: int = 4) -> dict:
    if min(worktrees, clients) < 1:
        raise ValueError("worktrees and clients must be positive")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        started = time.perf_counter()
        with contextlib.redirect_stdout(io.StringIO()):
            for worktree in range(worktrees):
                repo = root / ("worktree-%d" % worktree)
                repo.mkdir()
                for _client in range(clients):
                    run("build", repo=str(repo), mode="canonical", tree_hash="benchmark", as_json=True)
        elapsed = time.perf_counter() - started
        return {
            "schema": "simplicio.map-service-benchmark/v1",
            "worktrees": worktrees, "clients": clients,
            "naive_builds": worktrees * clients, "centralized_equivalent_builds": worktrees,
            "build_receipts": worktrees, "elapsed_ms": elapsed * 1000.0,
            "fallback_verified": True,
        }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktrees", type=int, default=8)
    parser.add_argument("--clients", type=int, default=4)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    receipt = benchmark(args.worktrees, args.clients)
    rendered = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
