#!/usr/bin/env python3
"""Measured baseline/adaptive benchmark over exactly three pinned worktrees."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import time
from typing import Any, Dict, Iterable, Sequence

from simplicio_loop.validation_execution import REQUIRED_CONTEXT_HASHES, ValidationExecutor, ValidationTask


def _sha(repo: Path) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repo), "rev-parse", "HEAD"), capture_output=True, text=True, check=True,
    )
    return completed.stdout.strip()


def _context(repo: Path, sha: str, commands: Sequence[Sequence[str]]) -> Dict[str, str]:
    import hashlib

    def digest(value: Any) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    lockfiles = {}
    for name in ("uv.lock", "poetry.lock", "requirements.txt", "pyproject.toml"):
        path = repo / name
        if path.is_file():
            lockfiles[name] = path.read_bytes().hex()
    values = {
        "source_hash": sha,
        "test_hash": digest(commands),
        "dependency_hash": digest(lockfiles),
        "environment_hash": digest({"platform": sys.platform}),
        "command_hash": digest(commands),
        "config_hash": digest(lockfiles.get("pyproject.toml", "")),
        "lockfile_hash": digest(lockfiles),
        "toolchain_hash": digest({"python": sys.version}),
    }
    assert set(values) == set(REQUIRED_CONTEXT_HASHES)
    return values


def _measure(tasks: Sequence[ValidationTask], context: Dict[str, str], workers: int) -> Dict[str, Any]:
    before_cpu = time.process_time()
    before_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    started = time.perf_counter()
    receipt = ValidationExecutor(max_workers=workers).execute(
        tasks, context=context, final_gate_required=False,
    )
    elapsed = time.perf_counter() - started
    after_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    passed = sum(item["status"] == "PASSED" for item in receipt["results"])
    return {
        "total_seconds": elapsed,
        "test_seconds": sum(item["duration_ms"] for item in receipt["results"]) / 1000,
        "cpu_seconds": time.process_time() - before_cpu,
        "peak_rss_kib_delta": max(0, after_rss - before_rss),
        "attempts": len(receipt["results"]),
        "tokens": None,
        "tokens_null_reason": "NO_LLM_USED",
        "resolution_rate": passed / len(receipt["results"]) if receipt["results"] else 0,
        "promotable": receipt["promotable"],
    }


def run_repository(repo: Path, commands: Sequence[Sequence[str]], repetitions: int) -> Dict[str, Any]:
    sha = _sha(repo)
    context = _context(repo, sha, commands)
    all_tasks = tuple(
        ValidationTask(str(index), tuple(command), resources=(str(repo),), cwd=str(repo))
        for index, command in enumerate(commands)
    )
    # Adaptive is intentionally capped at 20 during edit/converge; final gate remains full.
    adaptive_tasks = all_tasks[:20]
    samples = {"baseline": [], "adaptive": []}
    for _ in range(repetitions):
        samples["baseline"].append(_measure(all_tasks, context, 1))
        samples["adaptive"].append(_measure(adaptive_tasks, context, min(4, len(adaptive_tasks) or 1)))
    summary = {}
    for mode, values in samples.items():
        summary[mode] = {
            key: statistics.mean(item[key] for item in values)
            for key in ("total_seconds", "test_seconds", "cpu_seconds", "peak_rss_kib_delta",
                        "attempts", "resolution_rate")
        }
        summary[mode]["tokens"] = None
        summary[mode]["tokens_null_reason"] = "NO_LLM_USED"
    summary["quality_regression"] = (
        summary["adaptive"]["resolution_rate"] < summary["baseline"]["resolution_rate"]
    )
    return {
        "repo": repo.name,
        "sha": sha,
        "workload_count": len(commands),
        "repetitions": repetitions,
        "samples": samples,
        "summary": summary,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", action="append", required=True)
    parser.add_argument("--workload", required=True, help="JSON array of argv arrays")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if len(args.repo) != 3:
        parser.error("exactly three --repo worktrees are required")
    if args.repetitions < 3:
        parser.error("--repetitions must be at least 3")
    commands = json.loads(Path(args.workload).read_text(encoding="utf-8"))
    if not commands or not all(isinstance(item, list) and item for item in commands):
        parser.error("workload must contain non-empty argv arrays")
    repos = [run_repository(Path(path).resolve(), commands, args.repetitions) for path in args.repo]
    receipt = {
        "schema": "simplicio.validation-execution-benchmark/v1",
        "hardware": {
            "platform": sys.platform,
            "python": sys.version,
            "same_process": True,
        },
        "workload": commands,
        "repositories": repos,
        "local_llm_started": False,
    }
    Path(args.output).write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return 1 if any(item["summary"]["quality_regression"] for item in repos) else 0


if __name__ == "__main__":
    raise SystemExit(main())
