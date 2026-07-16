#!/usr/bin/env python3
"""simplicio-loop — Independent Review Panel CLI (issue #427, epic #422).

Materializes the four independent per-item reviewer roles
(security_correctness_reviewer, maintainability_reviewer,
runtime_reproduction_verifier, blast_radius_reviewer) over
``simplicio_loop.review_panel``. Stdlib-only, deterministic given fixed
inputs.

Usage:
    python3 scripts/review_panel.py roles
    python3 scripts/review_panel.py rubric-hash --role security_correctness_reviewer
    python3 scripts/review_panel.py waves --slots 2
    python3 scripts/review_panel.py synthesize --receipts receipts.json
    python3 scripts/review_panel.py selftest
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simplicio_loop import review_panel as rp  # noqa: E402


def _emit(payload: dict, *, exit_code: int = 0) -> int:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return exit_code


def cmd_roles(_opts) -> int:
    return _emit({"ok": True, "roles": rp.build_role_definitions()})


def cmd_rubric_hash(opts) -> int:
    try:
        return _emit({"ok": True, "role_id": opts.role, "rubric_hash": rp.rubric_hash(opts.role)})
    except rp.ReviewPanelError as exc:
        return _emit({"ok": False, "reason_code": exc.reason_code, "error": str(exc)}, exit_code=1)


def cmd_waves(opts) -> int:
    try:
        waves = rp.plan_reviewer_waves(opts.slots)
        return _emit({"ok": True, "waves": waves})
    except rp.ReviewPanelError as exc:
        return _emit({"ok": False, "reason_code": exc.reason_code, "error": str(exc)}, exit_code=1)


def cmd_synthesize(opts) -> int:
    receipts = json.loads(Path(opts.receipts).read_text(encoding="utf-8"))
    result = rp.synthesize(receipts)
    return _emit({"ok": True, **result}, exit_code=0 if result["verdict"] == rp.VERDICT_PASS else 1)


def cmd_selftest(_opts) -> int:
    checks: list[bool] = []

    def chk(name: str, value, expected) -> None:
        ok = value == expected
        checks.append(ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")

    roles = rp.build_role_definitions()
    chk("roles.count", len(roles), 4)
    chk("roles.ids", {r["role_id"] for r in roles}, set(rp.REVIEWER_ROLE_IDS))

    h1 = rp.rubric_hash("security_correctness_reviewer")
    h2 = rp.rubric_hash("security_correctness_reviewer")
    chk("rubric_hash.stable", h1, h2)
    chk("rubric_hash.distinct_per_role", len({rp.rubric_hash(r) for r in rp.REVIEWER_ROLE_IDS}), 4)

    try:
        rp.reject_same_actor(
            implementer_instance_id="impl-1",
            reviewer_instance_ids={"security_correctness_reviewer": "impl-1"},
        )
        chk("same_actor.rejected", False, True)
    except rp.ReviewPanelError as exc:
        chk("same_actor.rejected", exc.reason_code, rp.REASON_SAME_ACTOR)

    waves = rp.plan_reviewer_waves(2)
    chk("waves.count_for_2_slots", len(waves), 2)

    ok = all(checks)
    print(f"selftest: {'PASS' if ok else 'FAIL'} ({sum(checks)}/{len(checks)})")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="review_panel.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("roles", help="print the four materialized reviewer role definitions").set_defaults(func=cmd_roles)

    p_hash = sub.add_parser("rubric-hash", help="print the hash-pinned rubric hash for a role")
    p_hash.add_argument("--role", required=True, choices=list(rp.REVIEWER_ROLE_IDS))
    p_hash.set_defaults(func=cmd_rubric_hash)

    p_waves = sub.add_parser("waves", help="plan reviewer waves for a given slot count")
    p_waves.add_argument("--slots", type=int, required=True)
    p_waves.set_defaults(func=cmd_waves)

    p_synth = sub.add_parser("synthesize", help="synthesize a panel verdict from a JSON list of receipts")
    p_synth.add_argument("--receipts", required=True, help="path to a JSON file containing a list of receipts")
    p_synth.set_defaults(func=cmd_synthesize)

    sub.add_parser("selftest", help="deterministic self-check, no fixtures needed").set_defaults(func=cmd_selftest)

    opts = parser.parse_args()
    return opts.func(opts)


if __name__ == "__main__":
    raise SystemExit(main())
