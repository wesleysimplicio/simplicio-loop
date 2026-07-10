#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from simplicio_loop.runner import arm_run, change_phase, read_status, reconcile_delivery


def cmd_run(args: argparse.Namespace) -> int:
    payload = arm_run(args.repo, args.task, args.delivery, args.max_iterations)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    payload = read_status(args.repo, args.run_id)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    payload = change_phase(args.repo, args.run_id, "awaiting_decision", "resume requested")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    payload = change_phase(args.repo, args.run_id, "cancelled", "cancel requested")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_deliver(args: argparse.Namespace) -> int:
    payload = {}
    if args.payload_file:
        payload = json.loads(open(args.payload_file, encoding="utf-8").read())
    result = reconcile_delivery(args.repo, args.run_id, args.state, source_kind=args.source_kind,
                                source_payload=payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_selftest(_args: argparse.Namespace) -> int:
    print("selftest: PASS run-state cli shell loaded")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_state")
    sub = parser.add_subparsers(dest="verb", required=True)

    p_run = sub.add_parser("run")
    p_run.add_argument("--repo", required=True)
    p_run.add_argument("--task", required=True)
    p_run.add_argument("--delivery", default="verified")
    p_run.add_argument("--max-iterations", type=int, default=12)
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status")
    p_status.add_argument("--repo", required=True)
    p_status.add_argument("--run-id", default="")
    p_status.set_defaults(func=cmd_status)

    p_resume = sub.add_parser("resume")
    p_resume.add_argument("--repo", required=True)
    p_resume.add_argument("--run-id", required=True)
    p_resume.set_defaults(func=cmd_resume)

    p_cancel = sub.add_parser("cancel")
    p_cancel.add_argument("--repo", required=True)
    p_cancel.add_argument("--run-id", required=True)
    p_cancel.set_defaults(func=cmd_cancel)

    p_deliver = sub.add_parser("deliver")
    p_deliver.add_argument("--repo", required=True)
    p_deliver.add_argument("--run-id", required=True)
    p_deliver.add_argument("--state", required=True)
    p_deliver.add_argument("--source-kind", default="local")
    p_deliver.add_argument("--payload-file", default="")
    p_deliver.set_defaults(func=cmd_deliver)

    p_self = sub.add_parser("selftest")
    p_self.set_defaults(func=cmd_selftest)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
