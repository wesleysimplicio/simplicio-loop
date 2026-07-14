#!/usr/bin/env python3
"""CLI shell for the fail-closed quality-matrix gate (#278, extended by #283).

    python3 scripts/quality_matrix.py build --run-dir <dir> [--coverage-threshold N] [--change-type T]
    python3 scripts/quality_matrix.py check --run-dir <dir>
    python3 scripts/quality_matrix.py classify --title "..." [--label L ...]
    python3 scripts/quality_matrix.py selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from simplicio_loop.quality_matrix import (
    CHANGE_TYPES,
    DEFAULT_COVERAGE_THRESHOLD,
    build_quality_matrix_template,
    classify_change_type,
    default_policy_for_change_type,
    evaluate_quality_matrix,
    receipt_path,
)


def cmd_build(args: argparse.Namespace) -> int:
    template = build_quality_matrix_template(args.coverage_threshold, change_type=args.change_type)
    out = receipt_path(args.run_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(template, ensure_ascii=False, indent=2))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    verdict = evaluate_quality_matrix(args.run_dir)
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return 0 if verdict["ready"] else 1


def cmd_classify(args: argparse.Namespace) -> int:
    change_type = classify_change_type(args.title or "", args.label or [])
    policy = default_policy_for_change_type(change_type)
    print(json.dumps({"change_type": change_type, "policy": policy}, ensure_ascii=False, indent=2))
    return 0


def cmd_selftest(_args: argparse.Namespace) -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        verdict = evaluate_quality_matrix(str(run_dir))
        assert verdict["ready"] is False
        assert verdict["reason_code"] == "quality_matrix_missing"

        template = build_quality_matrix_template()
        (run_dir / "quality-matrix.json").write_text(json.dumps(template), encoding="utf-8")
        verdict = evaluate_quality_matrix(str(run_dir))
        assert verdict["ready"] is False  # template is all-unset by design

        passing = {
            "schema": "simplicio.quality-matrix/v1",
            "coverage_threshold": DEFAULT_COVERAGE_THRESHOLD,
            "requirements": {
                name: {"status": "pass", "proof_ref": f"tests/{name}"}
                for name in ("implementation", "unit", "integration", "system", "regression", "benchmark")
            },
            "coverage": {"measured": 90.0},
        }
        (run_dir / "quality-matrix.json").write_text(json.dumps(passing), encoding="utf-8")
        verdict = evaluate_quality_matrix(str(run_dir))
        assert verdict["ready"] is True

        # #283: change classification is deterministic and label-authoritative.
        assert classify_change_type("", ["bug"]) == "bug"
        assert classify_change_type("fix null pointer crash", []) == "fix"
        assert classify_change_type("add SSO login feature", []) == "feat"
        assert classify_change_type("update docs for the API", []) == "chore"
        assert classify_change_type("unrelated title", []) == "task"

        # #283: opt-in TDD lane — absent by default (no regression on #278 receipts),
        # required and validated (distinct RED != GREEN refs) once policy opts in.
        tdd_policy = dict(passing, policy={"tdd_required": True})
        (run_dir / "quality-matrix.json").write_text(json.dumps(tdd_policy), encoding="utf-8")
        verdict = evaluate_quality_matrix(str(run_dir))
        assert verdict["ready"] is False
        assert verdict["reason_code"] == "quality_tdd_missing"

        tdd_passing = dict(tdd_policy)
        tdd_passing["requirements"] = dict(passing["requirements"], tdd={
            "status": "pass", "red_proof_ref": "tests/test_x.py::red", "green_proof_ref": "tests/test_x.py::green",
        })
        (run_dir / "quality-matrix.json").write_text(json.dumps(tdd_passing), encoding="utf-8")
        verdict = evaluate_quality_matrix(str(run_dir))
        assert verdict["ready"] is True

        # #283: justified NOT_APPLICABLE benchmark, only when policy opts in.
        na_receipt = dict(passing, policy={"allow_justified_not_applicable": True})
        na_receipt["requirements"] = dict(passing["requirements"])
        na_receipt["requirements"]["benchmark"] = {"status": "not_applicable", "justification": "no perf-sensitive code path touched"}
        (run_dir / "quality-matrix.json").write_text(json.dumps(na_receipt), encoding="utf-8")
        verdict = evaluate_quality_matrix(str(run_dir))
        assert verdict["ready"] is True
    print("selftest: PASS quality-matrix")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quality_matrix")
    sub = parser.add_subparsers(dest="verb", required=True)
    p_build = sub.add_parser("build")
    p_build.add_argument("--run-dir", required=True)
    p_build.add_argument("--coverage-threshold", type=float, default=DEFAULT_COVERAGE_THRESHOLD)
    p_build.add_argument("--change-type", choices=list(CHANGE_TYPES), default=None)
    p_build.set_defaults(func=cmd_build)
    p_check = sub.add_parser("check")
    p_check.add_argument("--run-dir", required=True)
    p_check.set_defaults(func=cmd_check)
    p_classify = sub.add_parser("classify")
    p_classify.add_argument("--title", default="")
    p_classify.add_argument("--label", action="append", default=[])
    p_classify.set_defaults(func=cmd_classify)
    p_self = sub.add_parser("selftest")
    p_self.set_defaults(func=cmd_selftest)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
