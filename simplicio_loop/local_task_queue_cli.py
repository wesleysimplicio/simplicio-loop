"""JSON CLI for the durable local task queue."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

from .local_task_queue import LocalTaskQueue
from .mapper_operations import MapperOperationsError
from .mapper_queue import MapperQueue
from .remote_queue import QueueConflict, QueueUnavailable


MIGRATION_SCHEMA = "simplicio.loop.mapper-queue-migration/v1"
TERMINAL_LEGACY_STATES = frozenset(("completed", "cancelled", "failed"))
IMPORTABLE_LEGACY_STATES = frozenset(("ready", "queued", "pending"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _legacy_queue_path(repo: str) -> Path:
    root = _git_root(repo)
    path = root / ".simplicio" / "orchestrator" / "queue.sqlite3"
    if path.is_symlink() or not path.is_file():
        raise QueueUnavailable(f"legacy queue is missing: {path}")
    return path


def _migrate_legacy_queue(repo: str, destination: MapperQueue, *, apply: bool) -> dict[str, object]:
    source = _legacy_queue_path(repo)
    source_sha256 = _sha256_file(source)
    backup = source.with_name(f"{source.name}.mapper-backup-{time.time_ns()}")
    imported: list[str] = []
    skipped: list[dict[str, str]] = []
    mapper_results: list[dict[str, object]] = []
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute("SELECT task_id,status,payload,updated_at FROM tasks ORDER BY task_id").fetchall()
        except sqlite3.Error as exc:
            raise QueueUnavailable(f"legacy queue inventory failed: {exc}") from exc
        inventory = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise QueueUnavailable(f"invalid legacy payload for {row['task_id']}") from exc
            if not isinstance(payload, dict):
                raise QueueUnavailable(f"legacy payload is not an object for {row['task_id']}")
            item = {"task_id": str(row["task_id"]), "status": str(row["status"]),
                    "payload": payload, "updated_at": row["updated_at"]}
            inventory.append(item)

    for item in inventory:
        if item["status"] in TERMINAL_LEGACY_STATES:
            skipped.append({"task_id": item["task_id"], "status": item["status"], "reason": "terminal_history_requires_receipt_import"})
            continue
        if item["status"] not in IMPORTABLE_LEGACY_STATES:
            skipped.append({"task_id": item["task_id"], "status": item["status"], "reason": "active_or_unknown_state_requires_reconciliation"})
            continue
        if not apply:
            imported.append(item["task_id"])
            continue
        result = destination.submit(
            item["task_id"],
            {**item["payload"], "legacy_source": str(source), "legacy_updated_at": item["updated_at"]},
            idempotency_key=f"legacy-loop:{item['task_id']}",
        )
        mapper_results.append(dict(result))
        imported.append(item["task_id"])

    backup_sha256 = None
    if apply:
        with sqlite3.connect(source) as source_db, sqlite3.connect(backup) as backup_db:
            source_db.backup(backup_db)
        backup_sha256 = _sha256_file(backup)
    return {
        "schema": MIGRATION_SCHEMA,
        "status": "applied" if apply else "planned",
        "dry_run": not apply,
        "source": str(source),
        "source_sha256": source_sha256,
        "backup": str(backup) if apply else None,
        "backup_sha256": backup_sha256,
        "destination": str(destination.database),
        "imported_task_ids": imported,
        "mapper_results": mapper_results,
        "skipped": skipped,
        "counts": {"source": len(inventory), "imported": len(imported), "skipped": len(skipped)},
        "legacy_policy": "read-only-until-explicit-cutover",
    }


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
    parser.add_argument("--route", choices=("legacy", "mapper"), default="legacy")
    parser.add_argument("--mapper-db", default=None,
                        help="initialized MapperStore operations.sqlite path")
    parser.add_argument("--mapper-init", action="store_true",
                        help="explicitly initialize --mapper-db before the command")
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
        if args.route == "mapper":
            if args.action in {"drain", "resume", "gc"}:
                raise QueueUnavailable(
                    f"MAPPER_ROUTE_UNSUPPORTED: queue {args.action} requires a legacy-only local state machine"
                )
            if not args.mapper_db:
                raise ValueError("--mapper-db is required with --route mapper")
            queue = MapperQueue(args.mapper_db, auto_create=False)
            if args.mapper_init and (args.action != "migrate" or args.apply):
                queue.initialize()
            if args.action == "migrate":
                value = _migrate_legacy_queue(args.repo, queue, apply=args.apply)
                print(json.dumps(value, sort_keys=True))
                return 0
        else:
            if args.mapper_db or args.mapper_init:
                raise ValueError("--mapper-db/--mapper-init require --route mapper")
            queue = LocalTaskQueue(_git_root(args.repo), allow_legacy=args.action == "migrate")
    except (OSError, subprocess.TimeoutExpired, ValueError, QueueUnavailable,
            MapperOperationsError) as exc:
        print(json.dumps({"schema": "simplicio.loop.local-task-queue-error/v1",
                          "status": "error", "code": "unavailable",
                          "reason": str(exc)}, sort_keys=True))
        return 2
    try:
        if args.route == "mapper":
            if args.action == "status":
                value = queue.status()
            elif args.action == "top":
                value = queue.operations.list_ready(limit=args.limit)
            elif args.action == "inspect":
                value = queue.status(args.task_id)
            elif args.action == "cancel":
                value = queue.cancel(args.task_id)
            elif args.action == "doctor":
                value = {
                    "schema": "simplicio.loop.mapper-queue-doctor/v1",
                    "route": "mapper",
                    "database": str(queue.database),
                    "healthy": True,
                    "capabilities": queue.capabilities(),
                }
            elif args.action == "reclaim":
                value = queue.reclaim_expired()
            else:  # pragma: no cover - guarded before MapperQueue construction
                raise QueueUnavailable("MAPPER_ROUTE_UNSUPPORTED")
        else:
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
    except (QueueConflict, QueueUnavailable, MapperOperationsError, KeyError,
            OSError, ValueError, sqlite3.Error) as exc:
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
