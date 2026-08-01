from __future__ import annotations

import argparse
import json
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

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="simplicio-loop tasks")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="plan the gated discover-to-PR pipeline")
    run.add_argument("scope", nargs="?", default="")
    run.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.dry_run:
        print(json.dumps({"schema": SCHEMA, "state": "blocked", "reason": "action_gate_required"}))
        return 2
    try:
        items = _items(args.scope)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"schema": SCHEMA, "state": "blocked", "reason": str(exc)}))
        return 2
    pipeline = ["implement:coding-loop", "review:adversarial-review", "pr"]
    rows = [{"item": item, "state": "partial", "worktree_isolation": True, "action_gate": "required", "pipeline": pipeline, "evidence": {"pr": None, "verification": None}} for item in items]
    print(json.dumps({"schema": SCHEMA, "dry_run": True, "items": rows, "deduplicated_count": len(rows), "state": "partial"}, sort_keys=True))
    return 0
