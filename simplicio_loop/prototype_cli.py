"""CLI surface for the Prototype-First gate (#568 P0 slice).

Thin argparse layer over `simplicio_loop.prototype_gate` -- every command below only calls
functions already exported there; no new schema/state logic lives in this file. Exposed as:

    simplicio-loop prototype plan|classify|validate-schema|doctor --json

and, for repos/CI that invoke workers directly (this repo's own convention, see
`scripts/evidence_receipt.py`):

    python3 scripts/prototype_gate_cli.py plan|classify|validate-schema|doctor --json
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, List

from simplicio_loop import prototype_gate as pg


def _print(payload: Any, _as_json: bool = True) -> None:
    # Every verb here is a deterministic, machine-consumable control-plane query -- output is
    # always JSON; `--json` is accepted on every subcommand for CLI-convention compatibility
    # with the rest of this repo's workers, but there is no alternate human-prose mode to opt
    # out of (nothing here is meant to be prose-summarized).
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _parse_signal(raw: str) -> tuple[str, bool]:
    if "=" in raw:
        key, _, value = raw.partition("=")
        return key.strip(), value.strip().lower() not in {"0", "false", "no", ""}
    return raw.strip(), True


def cmd_plan(args: argparse.Namespace) -> int:
    try:
        plan = pg.build_plan(
            work_item_id=args.work_item, goal=args.goal, prototype_type=args.type,
            source_sha=args.source_sha, level=args.level, estimated_budget=args.budget,
            validators=args.validator or [], context_pack_hash=args.context_pack_hash or "",
            negative_space=args.negative_space or [],
        )
    except pg.PrototypeGateError as exc:
        _print({"error": str(exc)}, args.json)
        return 2
    state = pg.init_state(work_item_id=args.work_item, plan=plan)
    saved_path = None
    if not args.no_persist:
        saved_path = pg.save_state(state, repo=args.repo)
    _print({"plan": plan, "state": state, "state_path": saved_path}, args.json)
    return 0


def cmd_classify(args: argparse.Namespace) -> int:
    signals = dict(_parse_signal(raw) for raw in (args.signal or []))
    try:
        result = pg.classify_necessity(task_description=args.task_description, signals=signals)
    except pg.PrototypeGateError as exc:
        _print({"error": str(exc)}, args.json)
        return 2
    if not result["required"] and args.emit_not_required_receipt:
        if not args.work_item:
            _print({"error": "--work-item is required with --emit-not-required-receipt"}, args.json)
            return 2
        result["not_required_receipt"] = pg.build_not_required_receipt(
            work_item_id=args.work_item, task_description=args.task_description, signals=signals,
            policy=args.policy or "",
        )
    _print(result, args.json)
    return 0 if result["required"] or not args.exit_code else 1


_VALIDATORS = {
    pg.PLAN_SCHEMA: lambda payload, ctx: pg.validate_plan(payload, current_source_sha=ctx.get("source_sha")),
    pg.CANDIDATE_SCHEMA: lambda payload, ctx: pg.validate_candidate(payload, plan=ctx.get("plan")),
    pg.DECISION_SCHEMA: lambda payload, ctx: pg.validate_decision(
        payload, plan=ctx["plan"], candidate_hash=ctx.get("candidate_hash", payload.get("candidate_hash", "")),
        current_source_sha=ctx.get("source_sha"),
    ),
    pg.RECEIPT_SCHEMA: lambda payload, ctx: pg.validate_receipt(
        payload, plan=ctx.get("plan"), candidate=ctx.get("candidate"), decision=ctx.get("decision"),
    ),
}


def _load_json_arg(path: str | None, inline: str | None) -> dict | None:
    if inline:
        return json.loads(inline)
    if path:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    return None


def cmd_validate_schema(args: argparse.Namespace) -> int:
    payload = _load_json_arg(args.file, args.inline)
    if payload is None:
        payload = json.loads(sys.stdin.read())
    schema = payload.get("schema") if isinstance(payload, dict) else None
    validator = _VALIDATORS.get(schema)
    if validator is None:
        _print({"valid": False, "error": f"unknown or unsupported schema: {schema!r}"}, args.json)
        return 2
    ctx = {
        "plan": _load_json_arg(args.plan_file, args.plan_inline),
        "candidate": _load_json_arg(args.candidate_file, args.candidate_inline),
        "decision": _load_json_arg(args.decision_file, args.decision_inline),
        "source_sha": args.current_source_sha,
    }
    try:
        result = validator(payload, ctx)
    except pg.PrototypeGateError as exc:
        _print({"valid": False, "error": str(exc)}, args.json)
        return 2
    ok = result.get("valid", True) if isinstance(result, dict) else True
    _print(result, args.json)
    return 0 if ok else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    import os

    state_dir = pg._state_dir(args.repo)
    tracked = []
    if os.path.isdir(state_dir):
        for name in sorted(os.listdir(state_dir)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(state_dir, name), encoding="utf-8") as handle:
                    state = json.load(handle)
            except (OSError, ValueError):
                continue
            tracked.append({
                "work_item_id": state.get("work_item_id"), "status": state.get("status"),
                "current_level": state.get("current_level"), "revise_count": state.get("revise_count"),
            })
    report = {
        "schemas": [pg.PLAN_SCHEMA, pg.CANDIDATE_SCHEMA, pg.DECISION_SCHEMA, pg.RECEIPT_SCHEMA,
                    pg.NECESSITY_SCHEMA, pg.NOT_REQUIRED_SCHEMA, pg.STATE_SCHEMA],
        "levels": list(pg.LEVELS),
        "stall_detector_available": pg._journal_analyze is not None,
        "state_dir": state_dir,
        "tracked_items": tracked,
    }
    _print(report, args.json)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prototype")
    sub = parser.add_subparsers(dest="verb", required=True)

    p_plan = sub.add_parser("plan", help="build+freeze a prototype plan and initialize its state")
    p_plan.add_argument("--json", action="store_true", help="accepted for CLI-convention compatibility; output is always JSON")
    p_plan.add_argument("--work-item", required=True)
    p_plan.add_argument("--goal", required=True)
    p_plan.add_argument("--type", required=True, choices=sorted(pg.TYPES))
    p_plan.add_argument("--source-sha", required=True)
    p_plan.add_argument("--level", default="P0", choices=list(pg.LEVELS))
    p_plan.add_argument("--budget", type=float, default=0)
    p_plan.add_argument("--validator", action="append")
    p_plan.add_argument("--context-pack-hash", default="")
    p_plan.add_argument("--negative-space", action="append")
    p_plan.add_argument("--repo", default=".")
    p_plan.add_argument("--no-persist", action="store_true")
    p_plan.set_defaults(func=cmd_plan)

    p_classify = sub.add_parser("classify", help="explainable prototype-necessity classification")
    p_classify.add_argument("--json", action="store_true", help="accepted for CLI-convention compatibility; output is always JSON")
    p_classify.add_argument("--task-description", required=True)
    p_classify.add_argument("--signal", action="append", help="RISK_SIGNAL or RISK_SIGNAL=true/false, repeatable")
    p_classify.add_argument("--emit-not-required-receipt", action="store_true")
    p_classify.add_argument("--work-item")
    p_classify.add_argument("--policy", default="")
    p_classify.add_argument("--exit-code", action="store_true", help="exit 1 when not required")
    p_classify.set_defaults(func=cmd_classify)

    p_val = sub.add_parser("validate-schema", help="validate a plan/candidate/decision/receipt payload")
    p_val.add_argument("--json", action="store_true", help="accepted for CLI-convention compatibility; output is always JSON")
    p_val.add_argument("--file")
    p_val.add_argument("--inline")
    p_val.add_argument("--plan-file")
    p_val.add_argument("--plan-inline")
    p_val.add_argument("--candidate-file")
    p_val.add_argument("--candidate-inline")
    p_val.add_argument("--decision-file")
    p_val.add_argument("--decision-inline")
    p_val.add_argument("--candidate-hash")
    p_val.add_argument("--current-source-sha")
    p_val.set_defaults(func=cmd_validate_schema)

    p_doctor = sub.add_parser("doctor", help="health-check the prototype gate + tracked flows")
    p_doctor.add_argument("--json", action="store_true", help="accepted for CLI-convention compatibility; output is always JSON")
    p_doctor.add_argument("--repo", default=".")
    p_doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
