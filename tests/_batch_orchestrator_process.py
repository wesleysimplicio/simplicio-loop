"""Real, standalone orchestrator process for the issue #288 cross-process crash/recovery
test (``tests/test_batch_crash_recovery.py``).

Run as ``python _batch_orchestrator_process.py --items-json ... --queue-db ... --journal-dir
... --result-file ... [--max-workers N] [--retry-budget N]``. Reads a list of already-armed
run items, attaches a real ``SQLiteRemoteQueue`` (the same on-disk file every invocation
shares) plus a real context pack to each, and calls the production
``simplicio_loop.runner.dispatch_operator_batch`` for real -- no mocking inside this process.
Determinism for the parts that would otherwise require a live ``simplicio-mapper``/
``simplicio-dev-cli`` binary or a live GitHub API comes entirely from the project's existing
env-var opt-in test hooks (``SIMPLICIO_LOOP_FAKE_DEVCLI_PREFLIGHT_JSON``,
``SIMPLICIO_LOOP_FAKE_OPERATOR_EXEC_JSON``), set by the parent test process before spawning
this one -- this script itself does no monkeypatching, because it cannot: it is a genuinely
separate OS process.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from simplicio_loop import runner as runner_mod  # noqa: E402
from simplicio_loop.agent_contract import build_context_pack  # noqa: E402
from simplicio_loop.remote_queue import SQLiteRemoteQueue  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--items-json", required=True)
    parser.add_argument("--queue-db", required=True)
    parser.add_argument("--journal-dir", required=True)
    parser.add_argument("--result-file", required=True)
    parser.add_argument("--max-workers", type=int, default=1)
    parser.add_argument("--retry-budget", type=int, default=0)
    args = parser.parse_args()

    raw_items = json.loads(Path(args.items_json).read_text(encoding="utf-8"))
    queue = SQLiteRemoteQueue(args.queue_db)

    items = []
    for raw in raw_items:
        identity = raw["identity"]
        context_pack = build_context_pack(
            task_id=raw["task_id"], goal=raw["goal"], identity=identity, acs=raw.get("acs") or (),
        )
        items.append({
            "repo": raw["repo"], "run_id": raw["run_id"], "task_index": raw["task_index"],
            "worker_id": identity["agent_id"], "task_id": raw["task_id"],
            "distributed_queue": queue,
            "agent_identity": identity,
            "context_pack": context_pack,
            "worktree_context": {"mode": "worktree", "path": raw["repo"], "branch": raw["branch"]},
            "isolation_key": raw["repo"],
        })

    result = runner_mod.dispatch_operator_batch(
        items, max_workers=args.max_workers, retry_budget=args.retry_budget,
        journal_dir=args.journal_dir,
    )
    Path(args.result_file).write_text(json.dumps(result, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
