"""CLI surface for the Prototype-First gate (#568 P0 slice).

Thin argparse layer over `simplicio_loop.prototype_gate`/`prototype_fanout`/`prototype_judge` --
every command below only calls functions already exported there; no new schema/state/execution/
judging logic lives in this file. Exposed as:

    simplicio-loop prototype plan|generate|list|show|validate|compare|decide|promote|reject|doctor --json

(`validate` is an alias of `validate-schema`, kept for back-compat with existing callers) and,
for repos/CI that invoke workers directly (this repo's own convention, see
`scripts/evidence_receipt.py`):

    python3 scripts/prototype_gate_cli.py plan|generate|list|show|validate|compare|decide|promote|reject|doctor --json

This closes epic #568's own checklist item #18 ("Expor CLI/API `prototype plan|generate|list|
show|validate|compare|decide|promote|reject|doctor --json`") -- `generate` wires
`prototype_fanout.dispatch_candidates`, `compare`/`decide` wire `prototype_judge.judge_and_decide`,
`promote` wires `prototype_judge.judge_transition` against the persisted state machine, `reject`
is the manual terminal-decision override path, and `list`/`show` read the same on-disk state
`plan`/`doctor` already use.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, List, Mapping

from simplicio_loop import prototype_gate as pg
from simplicio_loop import prototype_fanout as pf
from simplicio_loop import prototype_judge as pj


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


def _load_candidates_arg(path: str | None, inline: str | None) -> list[dict]:
    payload = _load_json_arg(path, inline)
    if payload is None:
        payload = json.loads(sys.stdin.read())
    if isinstance(payload, Mapping):
        payload = payload.get("candidates", [])
    if not isinstance(payload, list):
        raise pg.PrototypeGateError("candidates payload must be a JSON list (or {'candidates': [...]})")
    return payload


def cmd_generate(args: argparse.Namespace) -> int:
    """Fan out N candidate specs against a frozen plan and bridge each real execution result
    into a `prototype-candidate/v1` payload -- wires `prototype_fanout` end to end."""
    plan = _load_json_arg(args.plan_file, args.plan_inline)
    if plan is None:
        _print({"error": "--plan-file or --plan-inline is required"}, args.json)
        return 2
    try:
        specs_raw = _load_candidates_arg(args.candidates_file, args.candidates_inline)
        specs = [pf.CandidateSpec(**spec) for spec in specs_raw]
        report = pf.dispatch_candidates(plan, specs, max_concurrency=args.max_concurrency)
        candidates = [
            pf.build_candidate_from_run(
                plan=plan, result=result,
                strategy=next((s.strategy for s in specs if s.candidate_id == result.candidate_id), ""),
                agent_id=next((s.agent_id for s in specs if s.candidate_id == result.candidate_id), ""),
            )
            for result in report.results
        ]
    except (pg.PrototypeGateError, TypeError, ValueError) as exc:
        _print({"error": str(exc)}, args.json)
        return 2
    saved_path = None
    if args.persist:
        if not args.work_item:
            _print({"error": "--work-item is required with --persist"}, args.json)
            return 2
        saved_path = _candidates_path(args.work_item, args.repo)
        os.makedirs(os.path.dirname(saved_path), exist_ok=True)
        with open(saved_path, "w", encoding="utf-8") as handle:
            json.dump(candidates, handle, ensure_ascii=False, indent=2)
    _print({"report": report.summary(), "candidates": candidates, "candidates_path": saved_path}, args.json)
    return 0


def _candidates_path(work_item_id: str, repo: str) -> str:
    safe = pg._ITEM_RE.sub("_", str(work_item_id)).strip("_") or "item"
    return os.path.join(pg._state_dir(repo), f"{safe}.candidates.json")


def cmd_list(args: argparse.Namespace) -> int:
    """List every prototype flow tracked on disk under this repo's state dir."""
    state_dir = pg._state_dir(args.repo)
    items: list[dict[str, Any]] = []
    if os.path.isdir(state_dir):
        for name in sorted(os.listdir(state_dir)):
            if not name.endswith(".json") or name.endswith(".candidates.json"):
                continue
            state = pg.load_state(name[: -len(".json")], repo=args.repo)
            if state is None:
                continue
            work_item_id = state.get("work_item_id", name[: -len(".json")])
            items.append({
                "work_item_id": work_item_id,
                "status": state.get("status"),
                "current_level": state.get("current_level"),
                "revise_count": state.get("revise_count", 0),
                **pg.gate_status(work_item_id, repo=args.repo),
            })
    _print({"state_dir": state_dir, "items": items}, args.json)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """Show the full persisted state + gate readiness for one tracked work item."""
    state = pg.load_state(args.work_item, repo=args.repo)
    if state is None:
        _print({"error": f"no prototype flow tracked for work item {args.work_item!r}"}, args.json)
        return 1
    candidates_path = _candidates_path(args.work_item, args.repo)
    candidates = None
    if os.path.exists(candidates_path):
        with open(candidates_path, encoding="utf-8") as handle:
            candidates = json.load(handle)
    _print({
        "state": state, "gate": pg.gate_status(args.work_item, repo=args.repo),
        "candidates": candidates, "candidates_path": candidates_path if candidates is not None else None,
    }, args.json)
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    """Read-only ranked comparison of candidates against a plan -- no decision is produced,
    no state is mutated (see `decide`/`promote` for the transitions that do)."""
    plan = _load_json_arg(args.plan_file, args.plan_inline)
    candidates = _load_candidates_arg(args.candidates_file, args.candidates_inline)
    try:
        pg.validate_plan(plan)
        for candidate in candidates:
            pg.validate_candidate(candidate, plan=plan)
        report = pj.RuleBasedJudge().judge(plan, candidates, args.judge_id)
    except pg.PrototypeGateError as exc:
        _print({"error": str(exc)}, args.json)
        return 2
    _print(report, args.json)
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    """Produce a real ACCEPT/REVISE/REJECT decision via the independent judge, without
    touching any persisted promotion state (see `promote` to also apply the transition)."""
    plan = _load_json_arg(args.plan_file, args.plan_inline)
    candidates = _load_candidates_arg(args.candidates_file, args.candidates_inline)
    try:
        decision, report = pj.judge_and_decide(plan, candidates, args.judge_id)
    except pg.PrototypeGateError as exc:
        _print({"error": str(exc)}, args.json)
        return 2
    _print({"decision": decision, "verdicts": report}, args.json)
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    """Judge candidates against the plan and apply the resulting decision to the work item's
    PERSISTED promotion state (P0 -> P1 -> P2 -> FULL), saving the new state back to disk."""
    plan = _load_json_arg(args.plan_file, args.plan_inline)
    candidates = _load_candidates_arg(args.candidates_file, args.candidates_inline)
    state = pg.load_state(args.work_item, repo=args.repo)
    if state is None:
        _print({"error": f"no prototype flow tracked for work item {args.work_item!r}; run `plan` first"}, args.json)
        return 2
    try:
        new_state, decision, report = pj.judge_transition(
            state, plan, candidates, args.judge_id, current_source_sha=args.current_source_sha,
        )
    except pg.PrototypeGateError as exc:
        _print({"error": str(exc)}, args.json)
        return 2
    saved_path = pg.save_state(new_state, repo=args.repo)
    _print({"state": new_state, "decision": decision, "verdicts": report, "state_path": saved_path}, args.json)
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    """Manual terminal-decision override (REJECT/BLOCKED) applied to a work item's persisted
    state, for callers that already decided outside the judge (e.g. a human override)."""
    plan = _load_json_arg(args.plan_file, args.plan_inline)
    state = pg.load_state(args.work_item, repo=args.repo)
    if state is None:
        _print({"error": f"no prototype flow tracked for work item {args.work_item!r}; run `plan` first"}, args.json)
        return 2
    try:
        decision = pg.build_decision(
            plan=plan, candidate_hash=args.candidate_hash, decision=args.outcome, reason=args.reason,
        )
        new_state = pg.apply_decision(
            state, plan=plan, decision=decision, candidate_hash=args.candidate_hash,
            current_source_sha=args.current_source_sha,
        )
    except pg.PrototypeGateError as exc:
        _print({"error": str(exc)}, args.json)
        return 2
    saved_path = pg.save_state(new_state, repo=args.repo)
    _print({"state": new_state, "decision": decision, "state_path": saved_path}, args.json)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    state_dir = pg._state_dir(args.repo)
    tracked = []
    if os.path.isdir(state_dir):
        for name in sorted(os.listdir(state_dir)):
            if not name.endswith(".json") or name.endswith(".candidates.json"):
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

    p_val = sub.add_parser("validate-schema", aliases=["validate"],
                           help="validate a plan/candidate/decision/receipt payload")
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

    p_gen = sub.add_parser("generate", help="fan out candidate specs against a plan and build real candidate payloads")
    p_gen.add_argument("--json", action="store_true", help="accepted for CLI-convention compatibility; output is always JSON")
    p_gen.add_argument("--plan-file")
    p_gen.add_argument("--plan-inline")
    p_gen.add_argument("--candidates-file", help="JSON list of CandidateSpec-shaped objects (or {'candidates': [...]})")
    p_gen.add_argument("--candidates-inline")
    p_gen.add_argument("--max-concurrency", type=int, default=pf.DEFAULT_MAX_CONCURRENCY)
    p_gen.add_argument("--work-item")
    p_gen.add_argument("--repo", default=".")
    p_gen.add_argument("--persist", action="store_true", help="write the built candidates to disk (needs --work-item)")
    p_gen.set_defaults(func=cmd_generate)

    p_list = sub.add_parser("list", help="list every prototype flow tracked on disk")
    p_list.add_argument("--json", action="store_true", help="accepted for CLI-convention compatibility; output is always JSON")
    p_list.add_argument("--repo", default=".")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="show one tracked work item's full state + gate readiness")
    p_show.add_argument("--json", action="store_true", help="accepted for CLI-convention compatibility; output is always JSON")
    p_show.add_argument("--work-item", required=True)
    p_show.add_argument("--repo", default=".")
    p_show.set_defaults(func=cmd_show)

    p_compare = sub.add_parser("compare", help="read-only ranked comparison of candidates against a plan")
    p_compare.add_argument("--json", action="store_true", help="accepted for CLI-convention compatibility; output is always JSON")
    p_compare.add_argument("--plan-file")
    p_compare.add_argument("--plan-inline")
    p_compare.add_argument("--candidates-file", help="JSON list of prototype-candidate/v1 payloads (or {'candidates': [...]})")
    p_compare.add_argument("--candidates-inline")
    p_compare.add_argument("--judge-id", required=True)
    p_compare.set_defaults(func=cmd_compare)

    p_decide = sub.add_parser("decide", help="produce an ACCEPT/REVISE/REJECT decision via the independent judge")
    p_decide.add_argument("--json", action="store_true", help="accepted for CLI-convention compatibility; output is always JSON")
    p_decide.add_argument("--plan-file")
    p_decide.add_argument("--plan-inline")
    p_decide.add_argument("--candidates-file")
    p_decide.add_argument("--candidates-inline")
    p_decide.add_argument("--judge-id", required=True)
    p_decide.set_defaults(func=cmd_decide)

    p_promote = sub.add_parser("promote", help="judge candidates and apply the decision to the persisted promotion state")
    p_promote.add_argument("--json", action="store_true", help="accepted for CLI-convention compatibility; output is always JSON")
    p_promote.add_argument("--work-item", required=True)
    p_promote.add_argument("--plan-file")
    p_promote.add_argument("--plan-inline")
    p_promote.add_argument("--candidates-file")
    p_promote.add_argument("--candidates-inline")
    p_promote.add_argument("--judge-id", required=True)
    p_promote.add_argument("--current-source-sha")
    p_promote.add_argument("--repo", default=".")
    p_promote.set_defaults(func=cmd_promote)

    p_reject = sub.add_parser("reject", help="manual terminal REJECT/BLOCKED override applied to the persisted state")
    p_reject.add_argument("--json", action="store_true", help="accepted for CLI-convention compatibility; output is always JSON")
    p_reject.add_argument("--work-item", required=True)
    p_reject.add_argument("--plan-file")
    p_reject.add_argument("--plan-inline")
    p_reject.add_argument("--candidate-hash", required=True)
    p_reject.add_argument("--reason", default="")
    p_reject.add_argument("--outcome", default="REJECT", choices=["REJECT", "BLOCKED"])
    p_reject.add_argument("--current-source-sha")
    p_reject.add_argument("--repo", default=".")
    p_reject.set_defaults(func=cmd_reject)

    return parser


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
