#!/usr/bin/env python3
"""CLI shell for the fail-closed quality-matrix gate (#278).

    python3 scripts/quality_matrix.py build --run-dir <dir> [--coverage-threshold N]
    python3 scripts/quality_matrix.py check --run-dir <dir>
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
    DEFAULT_COVERAGE_THRESHOLD,
    build_quality_matrix_template,
    evaluate_quality_matrix,
    receipt_path,
)


def cmd_build(args: argparse.Namespace) -> int:
    template = build_quality_matrix_template(args.coverage_threshold)
    out = receipt_path(args.run_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(template, ensure_ascii=False, indent=2))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    verdict = evaluate_quality_matrix(args.run_dir)
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return 0 if verdict["ready"] else 1


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
    print("selftest: PASS quality-matrix")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quality_matrix")
    sub = parser.add_subparsers(dest="verb", required=True)
    p_build = sub.add_parser("build")
    p_build.add_argument("--run-dir", required=True)
    p_build.add_argument("--coverage-threshold", type=float, default=DEFAULT_COVERAGE_THRESHOLD)
    p_build.set_defaults(func=cmd_build)
    p_check = sub.add_parser("check")
    p_check.add_argument("--run-dir", required=True)
    p_check.set_defaults(func=cmd_check)
    p_self = sub.add_parser("selftest")
    p_self.set_defaults(func=cmd_selftest)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
