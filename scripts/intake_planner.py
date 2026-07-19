#!/usr/bin/env python3
"""CLI for the #425 `intake_planner` concrete stage-agent role.

Commands
--------
receipt   Build a `simplicio.intake-planner-receipt/v1` from a JSON input file
          (see `--help` for the expected shape) and print it (or write it to
          `--out`). Exits 1 (never mutates anything) when the verdict is
          BLOCKED, matching the boundary this role is not allowed to cross.
stage-receipt Build the #425 receipt AND project it into the canonical
              `simplicio.stage-receipt/v1` the coordinator (#424) actually validates.
boundary  Check a list of touched paths (or verbs: commit/pr/push/merge)
          against the role's allowlist; exits 1 on any violation.

Silent-on-success, errors to stderr -- same discipline as `scripts/stage_agents.py`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from simplicio_loop import intake_planner as ip


def _cmd_receipt(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = json.load(open(args.input, encoding="utf-8"))
    try:
        receipt = ip.build_intake_planner_receipt(**payload)
    except ip.IntakePlannerBoundaryError as exc:
        print(f"RECEIPT BLOCKED (boundary): {exc}", file=sys.stderr)
        return 1
    text = json.dumps(receipt, ensure_ascii=False, indent=2)
    if args.out:
        if not ip.is_path_in_boundary(args.out):
            print(f"RECEIPT BLOCKED (boundary): out-of-boundary --out path: {args.out}", file=sys.stderr)
            return 1
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    if not ip.receipt_is_passed(receipt):
        print(f"RECEIPT BLOCKED: {receipt.get('failing_checks')}", file=sys.stderr)
        return 1
    print("RECEIPT PASSED", file=sys.stderr)
    return 0


def _cmd_stage_receipt(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = json.load(open(args.input, encoding="utf-8"))
    try:
        receipt = ip.build_intake_planner_receipt(**payload)
    except ip.IntakePlannerBoundaryError as exc:
        print(f"RECEIPT BLOCKED (boundary): {exc}", file=sys.stderr)
        return 1
    stage_receipt = ip.to_stage_receipt(
        receipt, receipt_id=args.receipt_id, agent_instance_id=args.agent_instance_id,
        task_id=args.task_id, attempt_id=args.attempt_id, fence=args.fence,
        attempt_ordinal=args.attempt_ordinal, context_hash=args.context_hash,
        manifest_hash=args.manifest_hash,
    )
    text = json.dumps(stage_receipt, ensure_ascii=False, indent=2)
    if args.out:
        if not ip.is_path_in_boundary(args.out):
            print(f"RECEIPT BLOCKED (boundary): out-of-boundary --out path: {args.out}", file=sys.stderr)
            return 1
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    return 0 if ip.receipt_is_passed(receipt) else 1


def _cmd_boundary(args: argparse.Namespace) -> int:
    try:
        ip.assert_boundary_ok(args.path or [])
    except ip.IntakePlannerBoundaryError as exc:
        print(f"BOUNDARY FAIL: {exc}", file=sys.stderr)
        return 1
    print("BOUNDARY OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="intake_planner", description="intake_planner (#425) CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_rec = sub.add_parser("receipt", help="build the intake-planner-receipt/v1 from a JSON input file")
    p_rec.add_argument("--input", required=True, help="JSON file with build_intake_planner_receipt() kwargs")
    p_rec.add_argument("--out", default="", help="optional path to write the receipt JSON")
    p_rec.set_defaults(func=_cmd_receipt)

    p_sr = sub.add_parser("stage-receipt", help="build the #425 receipt AND project it into the canonical "
                           "simplicio.stage-receipt/v1 the coordinator (#424) actually validates")
    p_sr.add_argument("--input", required=True, help="JSON file with build_intake_planner_receipt() kwargs")
    p_sr.add_argument("--receipt-id", required=True)
    p_sr.add_argument("--agent-instance-id", required=True)
    p_sr.add_argument("--task-id", required=True)
    p_sr.add_argument("--attempt-id", required=True)
    p_sr.add_argument("--fence", required=True)
    p_sr.add_argument("--attempt-ordinal", type=int, default=1)
    p_sr.add_argument("--context-hash", required=True, help="64-hex sha256 from the coordinator's AgentInstance")
    p_sr.add_argument("--manifest-hash", required=True, help="64-hex sha256 from the coordinator's AgentInstance")
    p_sr.add_argument("--out", default="")
    p_sr.set_defaults(func=_cmd_stage_receipt)

    p_bnd = sub.add_parser("boundary", help="check touched paths/verbs against the role's allowlist")
    p_bnd.add_argument("path", nargs="*", help="paths (or commit/pr/push/merge verbs) to check")
    p_bnd.set_defaults(func=_cmd_boundary)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
