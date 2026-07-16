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
import sys
from typing import Any

from simplicio_loop import stage_agents as sa
from simplicio_loop.agent_contract import validate_stage_identity


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
    rec = json.load(open(args.receipt, encoding="utf-8"))
    inst = json.load(open(args.instance, encoding="utf-8"))
    graph = sa.load_graph(args.graph) if args.graph else None
    ok, errors = sa.validate_receipt(rec, inst, graph)
    if not ok:
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("RECEIPT PASS")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    inst = json.load(open(args.instance, encoding="utf-8"))
    identity = json.load(open(args.identity, encoding="utf-8"))
    ok, errors = sa.validate_instance(inst, identity)
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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
