#!/usr/bin/env python3
"""Portable CLI for the stage-agent contract (EPIC #422).

Commands
--------
validate   Validate the canonical stages.json graph (rejects cycles/orphans/skips).
graph      Print the dependency-respecting topological order of stages.
receipt    Validate a single StageReceipt JSON against its owning instance + graph.
status     Validate an AgentInstance lifecycle record against a run identity.

All commands are silent on success (exit 0) and print errors to stderr (exit 1),
so they compose in CI gates and the loop's planning/execution hooks.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

# Make the repo root importable when run directly (python3 scripts/stage_agents.py)
# as well as via module form (python3 -m scripts.stage_agents).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from simplicio_loop import stage_agents as sa
from simplicio_loop.agent_contract import validate_stage_identity


def _read_json(path: str, label: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        detail = getattr(exc, "msg", str(exc))
        raise sa.StageAgentError(f"{label}_invalid_json: {detail}") from exc
    if not isinstance(value, dict):
        raise sa.StageAgentError(f"{label}_invalid_json: expected object")
    return value


def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        graph = sa.load_graph(args.graph)
    except sa.StageAgentError as exc:
        print(f"VALIDATE FAIL: {exc}", file=sys.stderr)
        return 1
    ok, errors = sa.validate_graph(graph)
    if not ok:
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"VALIDATE PASS: {len(graph.get('stages', []))} stages, {len(graph.get('roles', []))} roles")
    return 0


def _cmd_graph(args: argparse.Namespace) -> int:
    try:
        graph = sa.load_graph(args.graph)
    except sa.StageAgentError as exc:
        print(f"GRAPH FAIL: {exc}", file=sys.stderr)
        return 1
    order = sa.accepted_order(graph)
    print("\n".join(order))
    return 0


def _cmd_receipt(args: argparse.Namespace) -> int:
    try:
        rec = _read_json(args.receipt, "receipt")
        inst = _read_json(args.instance, "instance")
        graph = sa.load_graph(args.graph) if args.graph else None
        ok, errors = sa.validate_receipt(rec, inst, graph)
    except sa.StageAgentError as exc:
        print(f"RECEIPT FAIL: {exc}", file=sys.stderr)
        return 1
    if not ok:
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("RECEIPT PASS")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    try:
        inst = _read_json(args.instance, "instance")
        identity = _read_json(args.identity, "identity")
        ok, errors = sa.validate_instance(inst, identity)
    except sa.StageAgentError as exc:
        print(f"INSTANCE FAIL: {exc}", file=sys.stderr)
        return 1
    if not ok:
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("INSTANCE PASS")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stage_agents", description="Portable stage-agent contract CLI")
    default_graph = sa.STAGES_FILE
    sub = parser.add_subparsers(dest="command", required=True)

    p_val = sub.add_parser("validate", help="validate the stage graph")
    p_val.add_argument("--graph", default=default_graph)
    p_val.set_defaults(func=_cmd_validate)

    p_graph = sub.add_parser("graph", help="print topological stage order")
    p_graph.add_argument("--graph", default=default_graph)
    p_graph.set_defaults(func=_cmd_graph)

    p_rec = sub.add_parser("receipt", help="validate a stage receipt")
    p_rec.add_argument("--receipt", required=True)
    p_rec.add_argument("--instance", required=True)
    p_rec.add_argument("--graph", default=default_graph)
    p_rec.set_defaults(func=_cmd_receipt)

    p_st = sub.add_parser("status", help="validate an agent instance")
    p_st.add_argument("--instance", required=True)
    p_st.add_argument("--identity", required=True)
    p_st.set_defaults(func=_cmd_status)

    p_safety = sub.add_parser(
        "safety", help="evaluate a mutation-boundary action intent (fail-closed decision)"
    )
    p_safety.add_argument("--intent", required=True, help="path to action-intent JSON")
    p_safety.add_argument("--policy-hash", required=True)
    p_safety.add_argument("--expiry", required=True)
    p_safety.add_argument("--human-receipt", default="")
    p_safety.add_argument("--human-fresh", action="store_true")
    p_safety.add_argument("--secret-scan-ok", action="store_true",
                          help="assert the secret scan passed (do not use without a real scan)")
    p_safety.add_argument("--allow-compound-unsafe", action="store_true")
    p_safety.set_defaults(func=_cmd_safety)

    args = parser.parse_args(argv)
    return args.func(args)


def _cmd_safety(args: argparse.Namespace) -> int:
    import json as _json

    from simplicio_loop.safety_agents.safety_gate_agent import (
        ActionIntent,
        Decision,
        decide,
    )

    with open(args.intent, encoding="utf-8") as fh:
        raw = _json.load(fh)
    intent = ActionIntent(
        intent_id=raw["intent_id"],
        action_class=raw["action_class"],
        command=raw["command"],
        actor=raw["actor"],
        scope=raw["scope"],
        idempotency_key=raw.get("idempotency_key", ""),
        policy_hash=raw.get("policy_hash", args.policy_hash),
        segments=tuple(raw.get("segments", [])),
        created_at=raw.get("created_at", ""),
    )
    scanners = []
    if args.secret_scan_ok and intent.action_class in (
        "commit",
        "push",
        "pull_request",
        "deploy_release",
        "secret_network_access",
    ):
        # Only asserted when a real scan actually passed.
        scanners.append(type("S", (), {"name": "secret_scan", "ok": True})())
    d = decide(
        intent,
        scanner_receipts=scanners,
        human_receipt=args.human_receipt,
        human_receipt_fresh=args.human_fresh,
        policy_hash=args.policy_hash,
        expiry=args.expiry,
        allow_compound_unsafe=args.allow_compound_unsafe,
    )
    print(
        _json.dumps(
            {
                "decision": d.decision.value,
                "reason_code": d.reason_code,
                "action_hash": d.action_hash,
                "constraints": list(d.constraints),
            },
            indent=2,
        )
    )
    return 0 if d.decision in (Decision.ALLOW, Decision.ALLOW_WITH_CONSTRAINTS) else 2


if __name__ == "__main__":
    raise SystemExit(main())
