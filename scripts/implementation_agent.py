#!/usr/bin/env python3
"""CLI for the #426 `implementation_agent` concrete stage-agent role.

Commands
--------
assignment   Build a normalized `simplicio.implementation-assignment/v1` from a
             JSON input file (see `--help` for the expected shape) and print it.
receipt      Build a `simplicio.implementation-stage-receipt/v1` from a JSON
             input file and print it (or write it to `--out`). Exits 1 when
             the verdict is not `pass`, or when a fail-closed invariant check
             (capability/path/drift) raises -- the attempt is void either way.
capability   Check whether a mutation-capability JSON blob is currently valid.
paths        Check a list of touched paths against an assignment's allowlist.

Silent-on-success, errors to stderr -- same discipline as `scripts/intake_planner.py`.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

_REPO_ROOT = __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from simplicio_loop import implementation_agent as ia


def _load(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _cmd_assignment(args: argparse.Namespace) -> int:
    payload = _load(args.input)
    try:
        assignment = ia.build_assignment(**payload)
    except ia.ImplementationAgentError as exc:
        print(f"ASSIGNMENT BLOCKED ({exc.reason_code}): {exc}", file=sys.stderr)
        return 1
    print(json.dumps(assignment, ensure_ascii=False, indent=2))
    return 0


def _cmd_receipt(args: argparse.Namespace) -> int:
    payload = _load(args.input)
    try:
        receipt = ia.build_implementation_stage_receipt(**payload)
    except ia.ImplementationAgentError as exc:
        print(f"RECEIPT BLOCKED ({exc.reason_code}): {exc}", file=sys.stderr)
        return 1
    text = json.dumps(receipt, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    if not ia.receipt_is_passed(receipt):
        print(f"RECEIPT NOT PASSED: verdict={receipt.get('verdict')} failing={receipt.get('failing_checks')}",
              file=sys.stderr)
        return 1
    print("RECEIPT PASSED", file=sys.stderr)
    return 0


def _cmd_capability(args: argparse.Namespace) -> int:
    payload = _load(args.input)
    if ia.is_capability_valid(payload):
        print("CAPABILITY VALID")
        return 0
    print("CAPABILITY INVALID", file=sys.stderr)
    return 1


def _cmd_paths(args: argparse.Namespace) -> int:
    payload = _load(args.input)
    allowed = payload.get("allowed_paths") or []
    violations = ia.check_path_allowlist(args.path or [], allowed_paths=allowed)
    if violations:
        print("PATH VIOLATIONS: " + ", ".join(violations), file=sys.stderr)
        return 1
    print("PATHS OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="implementation_agent", description="implementation_agent (#426) CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_asn = sub.add_parser("assignment", help="build the implementation-assignment/v1 from a JSON input file")
    p_asn.add_argument("--input", required=True, help="JSON file with build_assignment() kwargs")
    p_asn.set_defaults(func=_cmd_assignment)

    p_rec = sub.add_parser("receipt", help="build the implementation-stage-receipt/v1 from a JSON input file")
    p_rec.add_argument("--input", required=True, help="JSON file with build_implementation_stage_receipt() kwargs")
    p_rec.add_argument("--out", default="", help="optional path to write the receipt JSON")
    p_rec.set_defaults(func=_cmd_receipt)

    p_cap = sub.add_parser("capability", help="check whether a mutation-capability JSON blob is currently valid")
    p_cap.add_argument("--input", required=True, help="JSON file with the capability blob")
    p_cap.set_defaults(func=_cmd_capability)

    p_pth = sub.add_parser("paths", help="check touched paths against an assignment's allowlist")
    p_pth.add_argument("--input", required=True, help="JSON file with {\"allowed_paths\": [...]}")
    p_pth.add_argument("path", nargs="*", help="paths to check")
    p_pth.set_defaults(func=_cmd_paths)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
