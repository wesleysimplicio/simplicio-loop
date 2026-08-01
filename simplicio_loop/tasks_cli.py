from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path

SCHEMA = "simplicio.tasks-run-plan/v1"

def _items(scope: str) -> list[str]:
    source = Path(scope) if scope else None
    if source and source.is_file():
        values = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(values, list):
            raise ValueError("scope file must contain a JSON array")
        return sorted({str(value).strip() for value in values if str(value).strip()})
    return [scope] if scope else []

def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="simplicio-loop tasks")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the gated discover-to-PR pipeline")
    run.add_argument("scope", nargs="?", default="")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--action-gate", action="store_true")
    run.add_argument("--workspace", default=".")
    run.add_argument("--checkpoint", default="")
    run.add_argument("--agent-command", default="")
    run.add_argument("--max-workers", type=int, default=1)
    run.add_argument("--retry-budget", type=int, default=1)
    args = parser.parse_args(argv)
    if args.dry_run:
        try:
            items = _items(args.scope)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            _emit({"schema": SCHEMA, "state": "blocked", "reason": str(exc)})
            return 2
        pipeline = ["implement:coding-loop", "review:adversarial-review", "pr"]
        rows = [{"item": item, "state": "partial", "worktree_isolation": True, "action_gate": "required", "pipeline": pipeline, "evidence": {"pr": None, "verification": None}} for item in items]
        _emit({"schema": SCHEMA, "dry_run": True, "items": rows, "deduplicated_count": len(rows), "state": "partial"})
        return 0
    if not args.action_gate:
        _emit({"schema": SCHEMA, "state": "blocked", "reason": "action_gate_required"})
        return 2
    command = shlex.split(args.agent_command, posix=False)
    if not command:
        _emit({"schema": SCHEMA, "state": "blocked", "reason": "agent_command_required"})
        return 2
    try:
        from .tasks_live import run_live
        result = run_live(args.scope, workspace=args.workspace, checkpoint=args.checkpoint, agent_command=command, action_gate=True, max_workers=args.max_workers, retry_budget=args.retry_budget)
    except Exception as exc:
        _emit({"schema": SCHEMA, "state": "blocked", "reason": f"{type(exc).__name__}: {exc}"})
        return 2
    _emit(result)
    return 0 if result.get("state") == "completed" else 3
