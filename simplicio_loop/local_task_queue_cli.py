"""JSON CLI for the durable local task queue."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

from .local_task_queue import LocalTaskQueue


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
    try:
        queue = LocalTaskQueue(_git_root(args.repo))
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        parser.error(str(exc))
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
