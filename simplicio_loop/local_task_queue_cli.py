"""JSON CLI for the durable local task queue."""

from __future__ import annotations

import argparse
import json

from .local_task_queue import LocalTaskQueue


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="simplicio-loop queue")
    parser.add_argument("--repo", default=".")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("status", "top", "drain", "resume", "doctor", "reclaim", "gc"):
        command = sub.add_parser(action)
        if action in {"top"}:
            command.add_argument("--limit", type=int, default=20)
        if action == "drain":
            command.add_argument("--timeout", type=float, default=0.0)
        if action == "gc":
            command.add_argument("--apply", action="store_true")
    for action in ("inspect", "cancel"):
        command = sub.add_parser(action)
        command.add_argument("task_id")
    args = parser.parse_args(argv)
    queue = LocalTaskQueue(args.repo)
    if args.action == "status":
        value = queue.status_local()
    elif args.action == "top":
        value = queue.top(limit=args.limit)
    elif args.action == "inspect":
        value = queue.inspect_local(args.task_id)
    elif args.action == "cancel":
        value = queue.cancel_local(args.task_id)
    elif args.action == "drain":
        value = queue.drain(timeout=args.timeout)
    elif args.action == "resume":
        queue.resume()
        value = queue.status_local()
    elif args.action == "doctor":
        value = queue.doctor_local()
    elif args.action == "reclaim":
        value = {"reclaimed": queue.reclaim_stale()}
    else:
        value = queue.gc_terminal(apply=args.apply)
    print(json.dumps(value, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
