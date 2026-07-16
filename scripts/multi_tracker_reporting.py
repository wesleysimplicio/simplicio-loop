#!/usr/bin/env python3
"""Portable CLI for multi-tracker stage reporting (EPIC #422, issue #436).

Commands
--------
detect     Run detect() on every registered provider, print capability JSON.
publish    Dispatch a canonical stage-event envelope to all connected providers.
selftest   Exercise the dispatcher end-to-end against the deterministic fake
           provider (no network) and prove GitHub-required + idempotency.

All commands print JSON to stdout and exit 0 on success. ``publish`` exits 1
if the completion verdict is ``blocked`` for a GitHub-required run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from simplicio_loop import multi_tracker_reporting as mtr


def _cmd_detect(args: argparse.Namespace) -> int:
    dispatcher = mtr.default_dispatcher()
    capabilities = dispatcher.detect_all()
    print(json.dumps({name: cap.to_dict() for name, cap in capabilities.items()},
                      ensure_ascii=False, indent=2))
    return 0


def _cmd_publish(args: argparse.Namespace) -> int:
    envelope = mtr.StageEventEnvelope(
        run_id=args.run_id,
        task_id=args.task_id,
        source=args.source,
        stage=args.stage,
        agent=args.agent,
        attempt=args.attempt,
        fence=args.fence,
        status=args.status,
        sequence=args.sequence,
    )
    targets = {}
    if args.target:
        for pair in args.target:
            provider, _, target = pair.partition("=")
            targets[provider] = target

    dispatcher = mtr.default_dispatcher()
    results = dispatcher.dispatch(envelope, targets)
    verdict = dispatcher.completion_verdict(results, github_required=(args.source == "github"))
    out = {
        "verdict": verdict,
        "providers": {name: r.to_dict() for name, r in results.items()},
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if verdict != "blocked" else 1


def _cmd_selftest(args: argparse.Namespace) -> int:
    # 1. NOT_CONNECTED is reported honestly for unconfigured providers.
    dispatcher = mtr.default_dispatcher(github_provider=mtr.FakeReportingProvider("github", connected=True))
    caps = dispatcher.detect_all()
    for name in ("azure_devops", "jira", "asana", "trello"):
        assert caps[name].state == "NOT_CONNECTED", f"{name} should be NOT_CONNECTED in sandbox"
        assert caps[name].reason_code, f"{name} must carry a reason_code"

    # 2. GitHub required + confirmed -> global verdict confirmed; optional
    #    disconnected providers are skipped, never attempted.
    envelope = mtr.StageEventEnvelope(
        run_id="run-1", task_id="task-1", source="github", stage="implementation",
        agent="agent-a", attempt=1, fence="fence-1", status="COMPLETE", sequence=1,
    )
    results = dispatcher.dispatch(envelope, targets={"github": "issue-42"})
    assert results["github"].status == "confirmed"
    for name in ("azure_devops", "jira", "asana", "trello"):
        assert results[name].status == "skipped_not_connected"
    verdict = dispatcher.completion_verdict(results, github_required=True)
    assert verdict == "confirmed"

    # 3. Idempotency: same run_id+task_id+provider+target updates, not duplicates.
    fake = mtr.FakeReportingProvider("azure_devops", connected=True)
    d2 = mtr.ReportingDispatcher([fake])
    r1 = d2.dispatch(envelope, targets={"azure_devops": "wi-7"})
    r2 = d2.dispatch(
        mtr.StageEventEnvelope(run_id="run-1", task_id="task-1", source="local", stage="review",
                                agent="agent-a", attempt=1, fence="fence-1", status="COMPLETE", sequence=2),
        targets={"azure_devops": "wi-7"},
    )
    assert r1["azure_devops"].remote_comment_id == r2["azure_devops"].remote_comment_id, "must update, not duplicate"

    print("selftest: PASS multi-tracker-reporting (not-connected honest, github-required, idempotent)")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="multi_tracker_reporting")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("detect")

    p_pub = sub.add_parser("publish")
    p_pub.add_argument("--run-id", required=True)
    p_pub.add_argument("--task-id", required=True)
    p_pub.add_argument("--source", default="github")
    p_pub.add_argument("--stage", required=True)
    p_pub.add_argument("--agent", required=True)
    p_pub.add_argument("--attempt", type=int, default=1)
    p_pub.add_argument("--fence", required=True)
    p_pub.add_argument("--status", required=True)
    p_pub.add_argument("--sequence", type=int, default=0)
    p_pub.add_argument("--target", action="append", help="provider=target, repeatable")

    sub.add_parser("selftest")

    args = parser.parse_args(argv)
    if args.command == "detect":
        return _cmd_detect(args)
    if args.command == "publish":
        return _cmd_publish(args)
    if args.command == "selftest":
        return _cmd_selftest(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
