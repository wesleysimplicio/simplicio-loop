#!/usr/bin/env python3
"""CLI for the #430 `feedback_recovery_agent` concrete stage-agent role.

Commands
--------
classify   Classify a raw reason_code (+ optional detail) into the failure
           taxonomy and print the stable fingerprint.
route      Compute the routing decision (target stage, invalidation
           dimensions, retryable, reconcile) for a failure_class + observed
           change dimensions, from a JSON input file.
receipt    Build a `simplicio.feedback-recovery-receipt/v1` from a JSON input
           file and print it (or write it to `--out`). Exits 1 unless the
           verdict is `routed`.
invalidate Compute the transitive receipt-invalidation closure for a list of
           changed dimensions.

Silent-on-success, errors to stderr -- same discipline as
`scripts/implementation_agent.py`.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

_REPO_ROOT = __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from simplicio_loop import feedback_recovery_agent as fra


def _load(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _cmd_classify(args: argparse.Namespace) -> int:
    failure_class = fra.classify_failure(reason_code=args.reason_code, detail=args.detail or "")
    fp = fra.signal_fingerprint(args.detail or args.reason_code)
    print(json.dumps({"failure_class": failure_class, "fingerprint": fp}, ensure_ascii=False, indent=2))
    return 0


def _cmd_route(args: argparse.Namespace) -> int:
    payload = _load(args.input)
    try:
        decision = fra.route_decision(**payload)
    except fra.FeedbackRecoveryAgentError as exc:
        print(f"ROUTE BLOCKED ({exc.reason_code}): {exc}", file=sys.stderr)
        return 1
    print(json.dumps(decision, ensure_ascii=False, indent=2))
    return 0


def _cmd_invalidate(args: argparse.Namespace) -> int:
    payload = _load(args.input)
    dimensions = payload.get("dimensions") or []
    receipts = payload.get("receipts") or []
    closure = fra.invalidation_closure(dimensions)
    invalidated = fra.invalidate_receipts(receipts, dimensions=dimensions)
    print(json.dumps({"invalidated_kinds": closure, "invalidated_receipts": invalidated},
                      ensure_ascii=False, indent=2))
    return 0


def _cmd_receipt(args: argparse.Namespace) -> int:
    payload = _load(args.input)
    try:
        receipt = fra.build_feedback_recovery_receipt(**payload)
    except fra.FeedbackRecoveryAgentError as exc:
        print(f"RECEIPT BLOCKED ({exc.reason_code}): {exc}", file=sys.stderr)
        return 1
    text = json.dumps(receipt, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    if not fra.receipt_is_routed(receipt):
        print(f"RECEIPT NOT ROUTED: verdict={receipt.get('verdict')} next_action={receipt.get('next_action')}",
              file=sys.stderr)
        return 1
    print("RECEIPT ROUTED", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="feedback_recovery_agent", description="feedback_recovery_agent (#430) CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_cls = sub.add_parser("classify", help="classify a raw reason_code into the failure taxonomy")
    p_cls.add_argument("--reason-code", dest="reason_code", required=True)
    p_cls.add_argument("--detail", default="", help="optional failure detail text to fingerprint")
    p_cls.set_defaults(func=_cmd_classify)

    p_rt = sub.add_parser("route", help="compute the routing decision from a JSON input file")
    p_rt.add_argument("--input", required=True, help="JSON file with route_decision() kwargs")
    p_rt.set_defaults(func=_cmd_route)

    p_inv = sub.add_parser("invalidate", help="compute the receipt invalidation closure")
    p_inv.add_argument("--input", required=True, help='JSON file with {"dimensions": [...], "receipts": [...]}')
    p_inv.set_defaults(func=_cmd_invalidate)

    p_rec = sub.add_parser("receipt", help="build the feedback-recovery-receipt/v1 from a JSON input file")
    p_rec.add_argument("--input", required=True, help="JSON file with build_feedback_recovery_receipt() kwargs")
    p_rec.add_argument("--out", default="", help="optional path to write the receipt JSON")
    p_rec.set_defaults(func=_cmd_receipt)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
