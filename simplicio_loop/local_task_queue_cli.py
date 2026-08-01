"""JSON CLI for the durable local task queue."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from .local_task_queue import LocalTaskQueue
from .remote_queue import QueueConflict, QueueUnavailable


def _git_root(repo: str) -> Path:
    candidate = Path(repo).resolve()
    command = ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    except OSError as exc:
        if os.name != "nt" or getattr(exc, "winerror", None) != 6:
            raise
        with tempfile.TemporaryDirectory(prefix="simplicio-queue-git-") as directory:
            stdout = Path(directory) / "stdout.txt"
            stderr = Path(directory) / "stderr.txt"
            shell_command = subprocess.list2cmdline(command) + f' > "{stdout}" 2> "{stderr}"'
            completed = subprocess.run(shell_command, shell=True, timeout=10)
            result = subprocess.CompletedProcess(
                command, completed.returncode,
                stdout.read_text(encoding="utf-8"), stderr.read_text(encoding="utf-8"),
            )
    if result.returncode != 0:
        raise ValueError("--repo must resolve to a Git worktree root")
    root = Path(result.stdout.strip()).resolve()
    if candidate != root:
        raise ValueError("--repo must be the Git worktree root")
    return root


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="simplicio-loop queue")
    parser.add_argument("--repo", default=".")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("status", "top", "drain", "resume", "doctor", "reclaim", "gc", "migrate"):
        command = sub.add_parser(action)
        if action in {"top"}:
            command.add_argument("--limit", type=int, default=20)
        if action == "drain":
            command.add_argument("--timeout", type=float, default=0.0)
        if action == "gc":
            command.add_argument("--apply", action="store_true")
        if action == "migrate":
            command.add_argument("--apply", action="store_true")
    for action in ("inspect", "cancel"):
        command = sub.add_parser(action)
        command.add_argument("task_id")
    args = parser.parse_args(argv)
    try:
        queue = LocalTaskQueue(_git_root(args.repo), allow_legacy=args.action == "migrate")
    except (OSError, subprocess.TimeoutExpired, ValueError, QueueUnavailable) as exc:
        print(json.dumps({"schema": "simplicio.loop.local-task-queue-error/v1",
                          "status": "error", "code": "unavailable",
                          "reason": str(exc)}, sort_keys=True))
        return 2
    try:
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
            value = {"schema": queue.status_local()["schema"],
                     "reclaimed": queue.reclaim_stale()}
        elif args.action == "migrate":
            value = queue.migrate(dry_run=not args.apply)
        else:
            value = queue.gc_terminal(apply=args.apply)
    except (QueueConflict, QueueUnavailable, KeyError, OSError, ValueError, sqlite3.Error) as exc:
        code = "not_found" if isinstance(exc, KeyError) else (
            "conflict" if isinstance(exc, QueueConflict) else "unavailable")
        reason = str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)
        print(json.dumps({"schema": "simplicio.loop.local-task-queue-error/v1",
                          "status": "error", "code": code, "reason": reason}, sort_keys=True))
        return 4 if code == "not_found" else (3 if code == "conflict" else 2)
    print(json.dumps(value, sort_keys=True))
    return 1 if args.action == "doctor" and not value.get("healthy", False) else 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
