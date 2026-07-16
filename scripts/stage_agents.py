#!/usr/bin/env python3
"""simplicio-loop — Portable Stage Agents CLI (issue #423, epic #422).

Diagnostic CLI over the ``simplicio_loop.stage_agents`` contract: validates
schemas/fixtures, renders a run's stage graph, binds/checks a stage receipt,
and reports the reducer's current status. Stdlib-only, deterministic, no
network. Non-zero exit + a stable ``reason_code`` on any violation.

Usage:
    python3 scripts/stage_agents.py validate --manifest contracts/stage-agents/v1/stages.json
    python3 scripts/stage_agents.py validate --fixture contracts/stage-agents/v1/fixtures/agent_instance.valid.json --schema simplicio.agent-instance/v1
    python3 scripts/stage_agents.py graph --run-id run-1 --task-id task-1
    python3 scripts/stage_agents.py receipt --fixture contracts/stage-agents/v1/fixtures/stage_receipt.valid.json
    python3 scripts/stage_agents.py status --run-id run-1 --task-id task-1 --receipts-dir <dir>
    python3 scripts/stage_agents.py selftest
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simplicio_loop import stage_agents as sa  # noqa: E402


def _load_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _emit(payload: dict, *, exit_code: int = 0) -> int:
    print(json.dumps(payload, indent=2, sort_keys=True))
    return exit_code


def cmd_validate(opts) -> int:
    try:
        if opts.fixture:
            if not opts.schema:
                return _emit({"ok": False, "reason_code": "missing_schema_arg"}, exit_code=2)
            instance = _load_json(opts.fixture)
            sa.validate_against_schema(instance, opts.schema)
            return _emit({"ok": True, "schema": opts.schema, "fixture": opts.fixture})
        manifest = sa.load_manifest(Path(opts.manifest)) if opts.manifest else sa.load_manifest()
        sa.validate_manifest(manifest)
        return _emit({
            "ok": True,
            "stages": [s["stage_id"] for s in manifest["stages"]],
            "roles": [r["role_id"] for r in manifest["roles"]],
        })
    except sa.StageAgentError as exc:
        return _emit({"ok": False, "reason_code": exc.reason_code, "error": str(exc)}, exit_code=1)


def cmd_graph(opts) -> int:
    try:
        manifest = sa.load_manifest(Path(opts.manifest)) if opts.manifest else sa.load_manifest()
        graph = sa.build_run_stage_graph(
            manifest, run_id=opts.run_id, task_id=opts.task_id,
            generated_at=opts.generated_at or "1970-01-01T00:00:00Z",
            source_manifest_hash=opts.manifest_hash or "sha256:unset",
        )
        return _emit({"ok": True, "graph": graph})
    except sa.StageAgentError as exc:
        return _emit({"ok": False, "reason_code": exc.reason_code, "error": str(exc)}, exit_code=1)


def cmd_receipt(opts) -> int:
    try:
        receipt = _load_json(opts.fixture)
        sa.validate_against_schema(receipt, sa.STAGE_RECEIPT_SCHEMA)
        classification = sa.classify_receipt_schema(receipt) if receipt.get("schema") != sa.STAGE_RECEIPT_SCHEMA \
            else sa.STAGE_RECEIPT_SCHEMA
        return _emit({"ok": True, "classification": classification, "status": receipt.get("status")})
    except sa.StageAgentError as exc:
        return _emit({"ok": False, "reason_code": exc.reason_code, "error": str(exc)}, exit_code=1)


def cmd_status(opts) -> int:
    try:
        manifest = sa.load_manifest(Path(opts.manifest)) if opts.manifest else sa.load_manifest()
        state = sa.StageGraphState(manifest, run_id=opts.run_id, task_id=opts.task_id)
        receipts_dir = Path(opts.receipts_dir) if opts.receipts_dir else None
        if receipts_dir and receipts_dir.is_dir():
            for receipt_path in sorted(receipts_dir.glob("*.json")):
                receipt = _load_json(str(receipt_path))
                state.apply_receipt(receipt, fence=receipt.get("fence", 0), plan_revision=receipt.get("plan_revision", 0))
        return _emit({
            "ok": True,
            "passed_stages": sorted(state.passed_stages.keys()),
            "unlocked_ready_stages": state.unlocked_ready_stages(),
            "rejected": state.rejected,
            "terminal_reached": state.terminal_reached(),
        })
    except sa.StageAgentError as exc:
        return _emit({"ok": False, "reason_code": exc.reason_code, "error": str(exc)}, exit_code=1)


def cmd_selftest(_opts) -> int:
    checks: list[bool] = []

    def chk(name: str, value, expected) -> None:
        ok = value == expected
        checks.append(ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")

    manifest = sa.load_manifest()
    chk("manifest.valid", bool(sa.validate_manifest(manifest)), True)
    chk("stage.implement.role", sa.stage_by_id(manifest, "implement")["role_id"], "implementer")
    try:
        sa.validate_manifest({"schema": "simplicio.stages-manifest/v1", "roles": [], "stages": [
            {**sa.stage_by_id(manifest, "coordinate"), "role_id": "ghost"},
        ]})
        chk("unknown_role.rejected", False, True)
    except sa.StageAgentError as exc:
        chk("unknown_role.rejected", exc.reason_code, "unknown_role")
    cyclic = {"schema": "simplicio.stages-manifest/v1", "roles": manifest["roles"], "stages": [
        {**sa.stage_by_id(manifest, "coordinate"), "stage_id": "a", "depends_on": ["b"], "next_stages": ["b"]},
        {**sa.stage_by_id(manifest, "implement"), "stage_id": "b", "depends_on": ["a"], "next_stages": ["a"]},
    ]}
    try:
        sa.validate_manifest(cyclic)
        chk("cycle.rejected", False, True)
    except sa.StageAgentError as exc:
        chk("cycle.rejected", exc.reason_code, "cycle_detected")
    ok = all(checks)
    print(f"selftest: {'PASS' if ok else 'FAIL'} ({sum(checks)}/{len(checks)})")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="stage_agents.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="validate stages.json or a single fixture against a schema")
    p_validate.add_argument("--manifest", default=None)
    p_validate.add_argument("--fixture", default=None)
    p_validate.add_argument("--schema", default=None)
    p_validate.set_defaults(func=cmd_validate)

    p_graph = sub.add_parser("graph", help="render the run-stage-graph for a run/task")
    p_graph.add_argument("--manifest", default=None)
    p_graph.add_argument("--run-id", required=True)
    p_graph.add_argument("--task-id", required=True)
    p_graph.add_argument("--generated-at", default=None)
    p_graph.add_argument("--manifest-hash", default=None)
    p_graph.set_defaults(func=cmd_graph)

    p_receipt = sub.add_parser("receipt", help="validate a stage-receipt fixture and classify it")
    p_receipt.add_argument("--fixture", required=True)
    p_receipt.set_defaults(func=cmd_receipt)

    p_status = sub.add_parser("status", help="replay receipts from a directory and report reducer state")
    p_status.add_argument("--manifest", default=None)
    p_status.add_argument("--run-id", required=True)
    p_status.add_argument("--task-id", required=True)
    p_status.add_argument("--receipts-dir", default=None)
    p_status.set_defaults(func=cmd_status)

    p_selftest = sub.add_parser("selftest", help="deterministic self-check, no fixtures needed")
    p_selftest.set_defaults(func=cmd_selftest)

    opts = parser.parse_args()
    return opts.func(opts)


if __name__ == "__main__":
    raise SystemExit(main())
