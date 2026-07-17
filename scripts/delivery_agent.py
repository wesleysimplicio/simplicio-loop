#!/usr/bin/env python3
"""CLI for the #429 `delivery_agent` concrete stage-agent role.

Commands
--------
preconditions   Check delivery preconditions from a JSON input file and print the result.
                Exits 1 when any precondition is unmet.
composed-verify Run the composed verification gate (tests + review synthesis) from a
                JSON input file. Exits 1 when not ok.
identity        Check identity consistency across upstream receipts from a JSON input
                file. Exits 1 on drift.
reachability    Run `GitHubDeliveryAdapter.check_reachability` for a commit against a
                target branch (real git calls against the current repo). Exits 1 when
                not reachable.
receipt         Build a `simplicio.delivery-stage-receipt/v1` from a JSON input file
                describing a saga's recorded steps, and print/write it. Exits 1 when the
                receipt is not delivered.
stage-receipt   Build the #429 receipt AND project it into the canonical
                `simplicio.stage-receipt/v1` the coordinator (#424) actually validates.

Silent-on-success, errors to stderr -- same discipline as `scripts/implementation_agent.py`.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

_REPO_ROOT = __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from simplicio_loop import delivery_agent as da


def _load(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _cmd_preconditions(args: argparse.Namespace) -> int:
    payload = _load(args.input)
    result = da.check_preconditions(**payload)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    if not result.ok:
        print("PRECONDITIONS NOT MET: " + "; ".join(result.errors), file=sys.stderr)
        return 1
    print("PRECONDITIONS OK", file=sys.stderr)
    return 0


def _cmd_composed_verify(args: argparse.Namespace) -> int:
    payload = _load(args.input)
    result = da.composed_verification(**payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"]:
        print("COMPOSED VERIFICATION FAILED", file=sys.stderr)
        return 1
    print("COMPOSED VERIFICATION PASSED", file=sys.stderr)
    return 0


def _cmd_identity(args: argparse.Namespace) -> int:
    payload = _load(args.input)
    current = payload["current"]
    receipts = payload["receipts"]
    errors: list[str] = []
    for label, receipt in receipts.items():
        errors.extend(da.check_identity_match(current, receipt, label=label))
    if errors:
        print("IDENTITY DRIFT: " + "; ".join(errors), file=sys.stderr)
        return 1
    print("IDENTITY CONSISTENT")
    return 0


def _cmd_reachability(args: argparse.Namespace) -> int:
    adapter = da.GitHubDeliveryAdapter(repo=args.repo)
    result = adapter.check_reachability(commit_sha=args.commit, target_branch=args.target)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("reachable"):
        print("NOT REACHABLE", file=sys.stderr)
        return 1
    print("REACHABLE", file=sys.stderr)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    payload = _load(args.input)
    steps = [da.SagaStep(event=s["event"], ok=bool(s["ok"]), detail=s.get("detail") or {}) for s in payload["steps"]]
    print(json.dumps(da.delivery_status(steps), ensure_ascii=False, indent=2))
    return 0


class _StepsView:
    """Minimal duck-typed stand-in for a `DeliverySaga` -- `build_delivery_stage_receipt`
    only ever reads `.steps`, so the CLI doesn't need a full live adapter/ledger to
    build a receipt from an already-recorded saga's JSON."""
    def __init__(self, steps):
        self.steps = [da.SagaStep(event=s["event"], ok=bool(s["ok"]), detail=s.get("detail") or {}) for s in steps]


def _build_delivery_receipt(payload):
    payload = dict(payload)
    payload["preconditions"] = da.DeliveryPreconditions(
        ok=bool(payload["preconditions"]["ok"]), errors=list(payload["preconditions"].get("errors") or []),
    )
    payload["saga"] = _StepsView(payload["saga"]["steps"])
    return da.build_delivery_stage_receipt(**payload)


def _cmd_receipt(args: argparse.Namespace) -> int:
    payload = _load(args.input)
    receipt = _build_delivery_receipt(payload)
    text = json.dumps(receipt, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    if not da.receipt_is_delivered(receipt):
        print(f"RECEIPT NOT DELIVERED: verdict={receipt.get('verdict')}", file=sys.stderr)
        return 1
    print("RECEIPT DELIVERED", file=sys.stderr)
    return 0


def _cmd_stage_receipt(args: argparse.Namespace) -> int:
    payload = _load(args.input)
    receipt = _build_delivery_receipt(payload)
    stage_receipt = da.to_stage_receipt(
        receipt, receipt_id=args.receipt_id, agent_instance_id=args.agent_instance_id,
        attempt_ordinal=args.attempt_ordinal, context_hash=args.context_hash,
        manifest_hash=args.manifest_hash,
    )
    text = json.dumps(stage_receipt, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    return 0 if da.receipt_is_delivered(receipt) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="delivery_agent", description="delivery_agent (#429) CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_pre = sub.add_parser("preconditions", help="check delivery preconditions from a JSON input file")
    p_pre.add_argument("--input", required=True, help="JSON file with check_preconditions() kwargs")
    p_pre.set_defaults(func=_cmd_preconditions)

    p_cv = sub.add_parser("composed-verify", help="run the composed verification gate")
    p_cv.add_argument("--input", required=True, help="JSON file with composed_verification() kwargs")
    p_cv.set_defaults(func=_cmd_composed_verify)

    p_id = sub.add_parser("identity", help="check identity consistency across upstream receipts")
    p_id.add_argument("--input", required=True, help='JSON file: {"current": {...}, "receipts": {label: {...}}}')
    p_id.set_defaults(func=_cmd_identity)

    p_re = sub.add_parser("reachability", help="check whether a commit is reachable from a target branch")
    p_re.add_argument("--repo", required=True, help="owner/name (used only for adapter identity, git ops are local)")
    p_re.add_argument("--commit", required=True, help="commit sha to check")
    p_re.add_argument("--target", required=True, help="target branch name")
    p_re.set_defaults(func=_cmd_reachability)

    p_st = sub.add_parser("status", help="compute status/blocker/next-action from recorded saga steps")
    p_st.add_argument("--input", required=True, help='JSON file: {"steps": [{"event", "ok", "detail"}, ...]}')
    p_st.set_defaults(func=_cmd_status)

    receipt_input_help = ('JSON file: {"run_id","task_id","attempt_id","fence","plan_revision",'
                           '"identity","preconditions":{"ok","errors"},"saga":{"steps":[...]},'
                           '"source_id","target_branch","pr_url"}')
    p_rec = sub.add_parser("receipt", help="build the simplicio.delivery-stage-receipt/v1 from a recorded saga")
    p_rec.add_argument("--input", required=True, help=receipt_input_help)
    p_rec.add_argument("--out", default="")
    p_rec.set_defaults(func=_cmd_receipt)

    p_sr = sub.add_parser("stage-receipt", help="build the #429 receipt AND project it into the canonical "
                           "simplicio.stage-receipt/v1 the coordinator (#424) actually validates")
    p_sr.add_argument("--input", required=True, help=receipt_input_help)
    p_sr.add_argument("--receipt-id", required=True)
    p_sr.add_argument("--agent-instance-id", required=True)
    p_sr.add_argument("--attempt-ordinal", type=int, default=1)
    p_sr.add_argument("--context-hash", required=True, help="64-hex sha256 from the coordinator's AgentInstance")
    p_sr.add_argument("--manifest-hash", required=True, help="64-hex sha256 from the coordinator's AgentInstance")
    p_sr.add_argument("--out", default="")
    p_sr.set_defaults(func=_cmd_stage_receipt)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
