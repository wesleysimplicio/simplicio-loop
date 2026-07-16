#!/usr/bin/env python3
"""Portable CLI for stage-agent display identity (issue #434).

Commands
--------
resolve    Resolve a display name + receipt-ready identity dict from args.
selftest   Run built-in self-checks (HOST4 normalization + fallback + format).

Silent on success beyond the requested output (exit 0); prints errors to
stderr and exits 1 on failure, so it composes in CI gates and the loop's
lifecycle/reporting hooks.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from simplicio_loop import agent_identity as ai


def _cmd_resolve(args: argparse.Namespace) -> int:
    result = ai.resolve_agent_identity(
        name=args.name,
        role=args.role,
        llm=args.llm,
        agent_instance_id=args.agent_instance_id,
        raw_user=args.raw_user,
        provider=args.provider,
        model=args.model,
        runtime=args.runtime,
        host_id=args.host_id,
        run_id=args.run_id,
        task_id=args.task_id,
        attempt_id=args.attempt_id,
        fence=args.fence,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _cmd_selftest(_args: argparse.Namespace) -> int:
    failures: list[str] = []

    # HOST4 normalization
    if ai.derive_host4("alice") != "ALIC":
        failures.append("derive_host4('alice') != 'ALIC'")
    if ai.derive_host4("Jo") != "JO":
        failures.append("derive_host4('Jo') != 'JO' (short input must not be padded)")
    if ai.derive_host4("j.o-s_e!!") != "JOSE":
        failures.append("derive_host4 must strip symbols")
    host4, reason = ai.resolve_host4("")
    if host4 != ai.HOST4_FALLBACK or reason != ai.HOST4_FALLBACK_REASON:
        failures.append("empty raw_user must fall back with a reason code")
    host4, reason = ai.resolve_host4(None)
    if host4 != ai.HOST4_FALLBACK or reason != ai.HOST4_FALLBACK_REASON:
        failures.append("None raw_user must fall back with a reason code")
    host4, reason = ai.resolve_host4("!!!")
    if host4 != ai.HOST4_FALLBACK or reason != ai.HOST4_FALLBACK_REASON:
        failures.append("symbol-only raw_user must fall back with a reason code")

    # display name format
    name = ai.format_display_name("Alex", "Review", "PC1", "Claude")
    if name != "Alex Review - #PC1 - Claude":
        failures.append(f"format_display_name mismatch: {name!r}")

    # sanitization
    injected = ai.format_display_name("Alex`code`", "Review", "PC1", "Claude")
    if "`" in injected:
        failures.append("backticks must be stripped from display name")

    # collision is allowed across hosts/LLMs (uniqueness lives elsewhere)
    ident_a = ai.resolve_agent_identity(
        name="Alex", role="Review", llm="Claude", agent_instance_id="inst-a",
        raw_user="alice",
    )
    ident_b = ai.resolve_agent_identity(
        name="Alex", role="Review", llm="Claude", agent_instance_id="inst-b",
        raw_user="alice",
    )
    if ident_a["display_name"] != ident_b["display_name"]:
        failures.append("expected display names to collide by design")
    if ident_a["agent_instance_id"] == ident_b["agent_instance_id"]:
        failures.append("expected distinct agent_instance_id")

    if failures:
        for f in failures:
            print(f"SELFTEST FAIL: {f}", file=sys.stderr)
        return 1
    print("SELFTEST PASS")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent_display_identity",
        description="Portable stage-agent display identity CLI (issue #434)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_resolve = sub.add_parser("resolve", help="resolve display name + receipt-ready identity")
    p_resolve.add_argument("--name", required=True)
    p_resolve.add_argument("--role", required=True)
    p_resolve.add_argument("--llm", required=True)
    p_resolve.add_argument("--agent-instance-id", dest="agent_instance_id", required=True)
    p_resolve.add_argument("--raw-user", dest="raw_user", default=None,
                            help="system user/hostname to derive HOST4 from; omit to auto-discover")
    p_resolve.add_argument("--provider", default=None)
    p_resolve.add_argument("--model", default=None)
    p_resolve.add_argument("--runtime", default=None)
    p_resolve.add_argument("--host-id", dest="host_id", default=None)
    p_resolve.add_argument("--run-id", dest="run_id", default=None)
    p_resolve.add_argument("--task-id", dest="task_id", default=None)
    p_resolve.add_argument("--attempt-id", dest="attempt_id", default=None)
    p_resolve.add_argument("--fence", default=None)
    p_resolve.set_defaults(func=_cmd_resolve)

    p_self = sub.add_parser("selftest", help="run built-in self-checks")
    p_self.set_defaults(func=_cmd_selftest)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
