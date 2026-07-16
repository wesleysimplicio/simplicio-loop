#!/usr/bin/env python3
"""Inspect open GitHub PRs at the Simplicio-loop review cadence.

Read-only by design: report actionable review, CI, rebase, and conflict work;
the loop then returns those items to the normal fix-and-evidence flow.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from simplicio_loop.pr_patrol import DEFAULT_CADENCE, PrPatrol, PrPatrolError


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="read-only Simplicio-loop open PR patrol")
    parser.add_argument("--repo", required=True, help="GitHub repository owner/name")
    parser.add_argument("--completed-items", type=int, default=0)
    parser.add_argument("--cadence", type=int, default=DEFAULT_CADENCE)
    parser.add_argument("--final", action="store_true", help="run the mandatory final reconciliation")
    parser.add_argument("--post-merge", action="store_true", help="run the mandatory post-merge reconciliation")
    args = parser.parse_args(argv)
    try:
        report = PrPatrol(args.repo).inspect(completed_items=args.completed_items,
                                             cadence=args.cadence, final=args.final,
                                             post_merge=args.post_merge)
    except (PrPatrolError, ValueError) as exc:
        print(json.dumps({"schema": "simplicio.pr-patrol/v1", "status": "unverified",
                          "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
