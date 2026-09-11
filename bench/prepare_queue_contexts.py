"""Central bounded Mapper handoffs for the frozen ten-task benchmark."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time

from queue_workflow_benchmark import TASKS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--token-budget", type=int, default=1200)
    parser.add_argument("--limit", type=int, default=2)
    args = parser.parse_args()
    root, out = Path(args.repo).resolve(), Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    records = []
    for task_id, source, kind, goal in TASKS:
        argv = ["simplicio-mapper", "handoff", str(root), "--goal", goal,
                "--token-budget", str(args.token_budget), "--limit", str(args.limit), "--execution-context", "--json"]
        start = time.perf_counter_ns()
        result = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=60)
        elapsed = time.perf_counter_ns() - start
        raw = out / f"{task_id}.handoff.json"
        raw.write_text(result.stdout)
        (out / f"{task_id}.stderr.txt").write_text(result.stderr)
        try:
            handoff = json.loads(result.stdout)
        except ValueError:
            handoff = {}
        record = {"task": task_id, "argv": argv, "exit_code": result.returncode,
                  "wall_ns": elapsed, "raw_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
                  "raw_bytes": len(result.stdout.encode()), "ready": handoff.get("ready"),
                  "reason": handoff.get("reason"), "keys": sorted(handoff)}
        # Keep the producer's complete bounded output, not an uncertified manual summary.
        records.append(record)
        (out / "receipts.json").write_text(json.dumps(records, indent=2))
        print(json.dumps(record), flush=True)
    return 0 if all(r["exit_code"] == 0 and r["ready"] is True for r in records) else 2


if __name__ == "__main__":
    raise SystemExit(main())
