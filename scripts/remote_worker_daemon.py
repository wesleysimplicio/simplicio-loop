#!/usr/bin/env python3
"""Standalone remote-worker process for issue #286.

Run as its own OS process (``python3 scripts/remote_worker_daemon.py claim ...``) against
a shared SQLite queue file. Exists so the real end-to-end test
(``tests/test_remote_worker_e2e.py``) can spawn two independent processes -- not two
threads in the same interpreter -- and prove: process A claims a task and heartbeats it;
process B's claim of the same task is rejected while A's lease is alive; killing process A
(simulating a crash) lets its lease expire; process B then claims and completes the same
task. A status file is the only IPC surface, written after every state change so the test
can observe progress without parsing stdout timing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from simplicio_loop.remote_queue import QueueConflict, SQLiteRemoteQueue  # noqa: E402
from simplicio_loop.worker_daemon import RemoteWorkerDaemon, sleep_in_slices  # noqa: E402


def _write_status(status_file: str, payload: dict) -> None:
    path = Path(status_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _cmd_claim(args: argparse.Namespace) -> int:
    queue = SQLiteRemoteQueue(args.db)
    daemon = RemoteWorkerDaemon(queue, agent_id=args.agent_id, capabilities=(),
                               heartbeat_interval=args.heartbeat_interval, lease_ttl=args.ttl)
    lease = daemon.try_claim(args.task_id, idempotency_key=args.idempotency_key)
    if lease is None:
        _write_status(args.status_file, {"pid": os.getpid(), "claimed": False, "state": "rejected"})
        print(json.dumps({"claimed": False}))
        return 3

    _write_status(args.status_file, {
        "pid": os.getpid(), "claimed": True, "state": "running",
        "fencing_token": lease.fencing_token, "lease_id": lease.lease_id,
    })

    def work(check_cancelled) -> dict:
        finished = sleep_in_slices(args.hold_seconds, slice_seconds=min(0.1, args.heartbeat_interval / 2),
                                   check_cancelled=check_cancelled)
        return {"finished": finished, "hold_seconds": args.hold_seconds}

    outcome = daemon.run_task(lease, work, receipt_ref=args.receipt_ref)
    _write_status(args.status_file, {
        "pid": os.getpid(), "claimed": True, "state": outcome.status,
        "fencing_token": lease.fencing_token, "detail": outcome.detail,
    })
    print(json.dumps({"claimed": True, "status": outcome.status}))
    return 0 if outcome.status == "completed" else (2 if outcome.status == "cancelled" else 1)


def _cmd_cancel(args: argparse.Namespace) -> int:
    queue = SQLiteRemoteQueue(args.db)
    try:
        result = queue.request_cancel(args.task_id, reason=args.reason)
    except QueueConflict as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps({"ok": True, **result}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    # ``--db`` is intentionally declared on each subparser (not the top-level parser):
    # argparse only accepts arguments of the top-level parser *before* the subcommand
    # token, which would force callers to write `--db PATH claim ...` instead of the
    # more natural `claim --db PATH ...` used throughout this module's tests/callers.
    p_claim = sub.add_parser("claim", help="claim one task, heartbeat it for --hold-seconds, then complete")
    p_claim.add_argument("--db", required=True, help="shared SQLite queue file path")
    p_claim.add_argument("--agent-id", required=True)
    p_claim.add_argument("--task-id", required=True)
    p_claim.add_argument("--idempotency-key", required=True)
    p_claim.add_argument("--ttl", type=float, default=2.0)
    p_claim.add_argument("--heartbeat-interval", type=float, default=0.5)
    p_claim.add_argument("--hold-seconds", type=float, default=5.0)
    p_claim.add_argument("--receipt-ref", default="receipts/task.json")
    p_claim.add_argument("--status-file", required=True)
    p_claim.set_defaults(func=_cmd_claim)

    p_cancel = sub.add_parser("cancel", help="request cooperative cancellation of the active lease")
    p_cancel.add_argument("--db", required=True, help="shared SQLite queue file path")
    p_cancel.add_argument("--task-id", required=True)
    p_cancel.add_argument("--reason", default="cancelled")
    p_cancel.set_defaults(func=_cmd_cancel)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
