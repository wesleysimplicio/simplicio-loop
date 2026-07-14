#!/usr/bin/env python3
"""CLI shell for the #285 GitHub lifecycle adapter (canonical status comment).

    python3 scripts/github_lifecycle.py render --state PLANNED --run-id r1 --attempt-id a1
    python3 scripts/github_lifecycle.py publish --owner acme --repo widgets --issue 12 \
        --state CLAIMED --run-id r1 --attempt-id a1
    python3 scripts/github_lifecycle.py selftest

`publish` shells out to the real `gh` CLI (via `scripts/pr_evidence.py::publish_comment`,
the same #295 idempotent create-or-update primitive) and re-queries the same comment to
confirm the id/body-hash match before reporting success -- see
`simplicio_loop/github_lifecycle.py` for the full contract and what remains out of scope
for issue #285 (list_ready/get_details/reconcile, lease/fencing-gated ownership, outbox).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from pr_evidence import publish_comment  # noqa: E402

from simplicio_loop.github_lifecycle import (  # noqa: E402
    LIFECYCLE_STATES,
    publish_lifecycle_state,
    render_lifecycle_comment,
)


def cmd_render(args: argparse.Namespace) -> int:
    body = render_lifecycle_comment(state=args.state, run_id=args.run_id, attempt_id=args.attempt_id,
                                    goal=args.goal or "")
    print(body)
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    receipt = publish_lifecycle_state(
        owner=args.owner, repo=args.repo, issue=args.issue, state=args.state,
        run_id=args.run_id, attempt_id=args.attempt_id, fencing_token=args.fencing_token,
        publish_comment_fn=publish_comment, goal=args.goal or "",
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0 if receipt["verified"] else 1


def cmd_selftest(_args: argparse.Namespace) -> int:
    from simplicio_loop.github_lifecycle import validate_transition

    checks = []

    def chk(name, got, want):
        ok = got == want
        checks.append(ok)
        print("  [%s] %-40s got=%r want=%r" % ("ok" if ok else "XX", name, got, want))

    chk("transition.valid", validate_transition("CLAIMED", "PLANNED")["ok"], True)
    chk("transition.invalid", validate_transition("CLAIMED", "MERGED")["ok"], False)
    chk("transition.duplicate_noop", validate_transition("PLANNED", "PLANNED")["reason_code"], "duplicate_noop")
    chk("transition.regression_needs_reason",
        validate_transition("PR_OPEN", "IN_PROGRESS", reason_code="SOURCE_CHANGED")["ok"], True)

    body = render_lifecycle_comment(state="CLAIMED", run_id="r", attempt_id="a")
    chk("render.has_marker", "simplicio-loop:lifecycle-status:v1" in body, True)
    chk("all_states_valid_arg", set(LIFECYCLE_STATES) >= {"CLAIMED", "PLANNED", "CLOSED"}, True)

    ok = all(checks)
    print("selftest: %s (%d/%d) github-lifecycle" % ("PASS" if ok else "FAIL", sum(checks), len(checks)))
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="github_lifecycle")
    sub = parser.add_subparsers(dest="verb", required=True)

    p_render = sub.add_parser("render")
    p_render.add_argument("--state", required=True, choices=list(LIFECYCLE_STATES))
    p_render.add_argument("--run-id", required=True)
    p_render.add_argument("--attempt-id", required=True)
    p_render.add_argument("--goal", default="")
    p_render.set_defaults(func=cmd_render)

    p_publish = sub.add_parser("publish")
    p_publish.add_argument("--owner", required=True)
    p_publish.add_argument("--repo", required=True)
    p_publish.add_argument("--issue", required=True)
    p_publish.add_argument("--state", required=True, choices=list(LIFECYCLE_STATES))
    p_publish.add_argument("--run-id", required=True)
    p_publish.add_argument("--attempt-id", required=True)
    p_publish.add_argument("--fencing-token", default="")
    p_publish.add_argument("--goal", default="")
    p_publish.set_defaults(func=cmd_publish)

    p_self = sub.add_parser("selftest")
    p_self.set_defaults(func=cmd_selftest)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
