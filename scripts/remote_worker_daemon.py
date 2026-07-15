#!/usr/bin/env python3
"""Standalone remote-worker process for issue #286.

Run as its own OS process (``python3 scripts/remote_worker_daemon.py claim ...``) against
either a shared SQLite queue file (``--db``) or a real networked queue (``--http URL``, backed
by ``scripts/remote_queue_server.py``). Exists so end-to-end tests can spawn genuinely
independent processes -- not threads in one interpreter -- and prove the full claim/heartbeat/
cancel/complete contract, whether the transport is a shared file
(``tests/test_remote_worker_e2e.py``) or a real HTTP socket
(``tests/test_remote_worker_http_e2e.py``). A status file is the only IPC surface, written
after every state change so a test can observe progress without parsing stdout timing.

Subcommands:

* ``claim``  -- claim exactly one named task, heartbeat it for ``--hold-seconds``, complete it.
* ``cancel`` -- request cooperative cancellation of the active lease for a task.
* ``enqueue`` -- publish one task (idempotent) so a test/tooling script doesn't need direct
  backend access -- required for the HTTP transport, where the queue's SQLite file is private
  to the server process.
* ``serve``  -- long-running worker loop: discover -> try_claim -> heartbeat+work -> complete,
  repeated for as long as the process lives. This is the unit ``scripts/remote_worker_supervisor.py``
  supervises: a crash of a ``serve`` process is what the supervisor detects and restarts.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from simplicio_loop.remote_queue import (  # noqa: E402
    HTTPRemoteQueue, QueueConflict, QueueUnavailable, RemoteQueue, SQLiteRemoteQueue,
)
from simplicio_loop.worker_daemon import RemoteWorkerDaemon, sleep_in_slices  # noqa: E402


def _write_status(status_file: str, payload: dict) -> None:
    path = Path(status_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _add_backend_args(parser: argparse.ArgumentParser) -> None:
    """Shared transport selection: exactly one of ``--db`` (SQLite, same-host/shared-file) or
    ``--http`` (a real network client against ``scripts/remote_queue_server.py``)."""
    parser.add_argument("--db", default=None, help="shared SQLite queue file path")
    parser.add_argument("--http", default=None, help="base URL of a real remote_queue_server.py instance")
    parser.add_argument("--token", default=os.environ.get("SIMPLICIO_QUEUE_TOKEN"),
                        help="bearer token for --http (ignored for --db)")


def _build_queue(args: argparse.Namespace) -> RemoteQueue:
    if bool(args.db) == bool(args.http):
        raise SystemExit("exactly one of --db or --http is required")
    if args.http:
        return HTTPRemoteQueue(args.http, token=args.token, timeout=10.0)
    return SQLiteRemoteQueue(args.db)


def _cmd_claim(args: argparse.Namespace) -> int:
    queue = _build_queue(args)
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
    queue = _build_queue(args)
    try:
        result = queue.request_cancel(args.task_id, reason=args.reason)
    except QueueConflict as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps({"ok": True, **result}))
    return 0


def _cmd_enqueue(args: argparse.Namespace) -> int:
    queue = _build_queue(args)
    payload: Optional[dict] = json.loads(args.payload) if args.payload else None
    queue.enqueue(args.task_id, payload)
    print(json.dumps({"ok": True, "task_id": args.task_id}))
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    """Long-running worker loop: this is the process a real supervisor keeps alive.

    Repeatedly discovers ready, capability-eligible work, claims the first task it wins, runs
    a bounded (``--work-seconds``) cooperative unit of "work" while heartbeating, completes it,
    then loops back to discover more. Idles for ``--poll-interval`` seconds between discovery
    attempts when nothing is claimable. Exits cleanly on SIGTERM (what
    ``remote_worker_supervisor.py`` sends for a graceful stop) but is otherwise expected to run
    forever -- an uncaught crash or a hard kill is exactly the failure the supervisor exists to
    detect and recover from.
    """
    queue = _build_queue(args)
    daemon = RemoteWorkerDaemon(queue, agent_id=args.agent_id, capabilities=tuple(args.capabilities or ()),
                               heartbeat_interval=args.heartbeat_interval, lease_ttl=args.ttl)
    stop = {"flag": False}

    def _handle_sigterm(*_a: Any) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        signal.signal(signal.SIGINT, _handle_sigterm)
    except (ValueError, OSError):  # pragma: no cover - not every host allows this in a thread
        pass

    _write_status(args.status_file, {"pid": os.getpid(), "state": "idle", "task_id": "", "ts": time.time()})
    while not stop["flag"]:
        try:
            tasks = daemon.discover(limit=5)
        except QueueUnavailable:
            time.sleep(args.poll_interval)
            continue
        claimed_any = False
        for candidate in tasks:
            if stop["flag"]:
                break
            task_id = candidate["task_id"]
            lease = daemon.try_claim(task_id, idempotency_key=f"{args.agent_id}:{task_id}:{time.time()}")
            if lease is None:
                continue
            claimed_any = True
            _write_status(args.status_file, {"pid": os.getpid(), "state": "running",
                                             "task_id": task_id, "ts": time.time()})

            def work(check_cancelled, _task_id=task_id) -> dict:
                sleep_in_slices(args.work_seconds, slice_seconds=min(0.1, args.heartbeat_interval / 2),
                                check_cancelled=check_cancelled)
                return {"ok": True, "task_id": _task_id}

            outcome = daemon.run_task(lease, work, receipt_ref=f"receipts/{task_id}.json")
            _write_status(args.status_file, {"pid": os.getpid(), "state": outcome.status,
                                             "task_id": task_id, "ts": time.time()})
        if not claimed_any:
            time.sleep(args.poll_interval)
    _write_status(args.status_file, {"pid": os.getpid(), "state": "stopped", "task_id": "", "ts": time.time()})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    # Backend/transport flags are declared per-subparser (not the top-level parser):
    # argparse only accepts top-level-parser arguments *before* the subcommand token, which
    # would force callers to write `--db PATH claim ...` instead of the more natural
    # `claim --db PATH ...` used throughout this module's tests/callers.
    p_claim = sub.add_parser("claim", help="claim one task, heartbeat it for --hold-seconds, then complete")
    _add_backend_args(p_claim)
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
    _add_backend_args(p_cancel)
    p_cancel.add_argument("--task-id", required=True)
    p_cancel.add_argument("--reason", default="cancelled")
    p_cancel.set_defaults(func=_cmd_cancel)

    p_enqueue = sub.add_parser("enqueue", help="publish one task (idempotent)")
    _add_backend_args(p_enqueue)
    p_enqueue.add_argument("--task-id", required=True)
    p_enqueue.add_argument("--payload", default=None, help="JSON object payload")
    p_enqueue.set_defaults(func=_cmd_enqueue)

    p_serve = sub.add_parser("serve", help="long-running worker loop (discover/claim/work/complete)")
    _add_backend_args(p_serve)
    p_serve.add_argument("--agent-id", required=True)
    p_serve.add_argument("--capabilities", nargs="*", default=())
    p_serve.add_argument("--ttl", type=float, default=5.0)
    p_serve.add_argument("--heartbeat-interval", type=float, default=1.0)
    p_serve.add_argument("--work-seconds", type=float, default=2.0)
    p_serve.add_argument("--poll-interval", type=float, default=0.3)
    p_serve.add_argument("--status-file", required=True)
    p_serve.set_defaults(func=_cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
