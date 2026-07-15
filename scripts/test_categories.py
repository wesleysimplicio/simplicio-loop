#!/usr/bin/env python3
"""simplicio-loop -- CI Quality Gate: per-category test-runner split (#283).

Issue #283 asks the `unit`/`integration`/`system`/`regression` test categories to become
"distinct invokable commands with their own pass/fail" instead of one undifferentiated
`pytest tests/`. This repo already has a partial, honest convention for this: 21 of the ~190
files under `tests/` are suffixed `_unit.py` / `_integration.py` / `_system.py` / `_regression.py`
(e.g. `tests/test_oracle_gates_unit.py`, `tests/test_quality_matrix_system.py`). This script is
the first thing that actually *reads* that convention mechanically and turns it into four
separate, independently-runnable gates -- rather than inventing a fake classification for the
~170 files that were never authored with a category in mind.

Deliberately NOT done here: guessing a category for every test in the suite by keyword-matching
test names (`-k unit`) against the whole corpus. That was tried while building this script and
is unreliable -- it silently sweeps in unrelated tests whose *name* happens to contain the
substring, including slow/live/e2e tests that hang for minutes in a sandboxed shell. Only the
filename-suffix convention is used as ground truth; every other test file is reported, honestly,
as `uncategorized` (see `list --category uncategorized` / `status`) rather than silently folded
into one of the four buckets.

Usage:
    python3 scripts/test_categories.py status
    python3 scripts/test_categories.py list --category unit
    python3 scripts/test_categories.py run --category unit
    python3 scripts/test_categories.py run --category integration --emit-json report.json
    python3 scripts/test_categories.py selftest

Exit codes for `run`: 0 = category's tests all pass, 1 = at least one failed, 2 = the category
has zero matching test files (BLOCKED -- a lane that would otherwise silently report a false
"pass" for having nothing to run).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TESTS_DIR = os.path.join(REPO, "tests")

CATEGORIES = ("unit", "integration", "system", "regression")
_SUFFIXES = {c: f"_{c}.py" for c in CATEGORIES}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _list_test_files(tests_dir: str) -> list:
    try:
        names = os.listdir(tests_dir)
    except FileNotFoundError:
        return []
    return sorted(n for n in names if n.startswith("test_") and n.endswith(".py"))


def discover(category: str, tests_dir: "str | None" = None) -> list:
    """Return the `tests/*_<category>.py` files for one of the four known categories."""
    if category not in CATEGORIES:
        raise ValueError(f"unknown category {category!r}; expected one of {CATEGORIES}")
    tests_dir = tests_dir or TESTS_DIR
    suffix = _SUFFIXES[category]
    rel_root = os.path.relpath(tests_dir, REPO).replace("\\", "/")
    return [
        f"{rel_root}/{name}" for name in _list_test_files(tests_dir)
        if name.endswith(suffix)
    ]


def discover_uncategorized(tests_dir: "str | None" = None) -> list:
    """Every `tests/test_*.py` file that does NOT match any of the four category suffixes.

    Reported explicitly rather than silently absorbed into a bucket -- this is the honest
    accounting of the repo's real migration state (Fase B/C of the issue's coverage-migration
    plan applies here too: most of the suite predates the category convention).
    """
    tests_dir = tests_dir or TESTS_DIR
    all_suffixes = tuple(_SUFFIXES.values())
    rel_root = os.path.relpath(tests_dir, REPO).replace("\\", "/")
    return [
        f"{rel_root}/{name}" for name in _list_test_files(tests_dir)
        if not name.endswith(all_suffixes)
    ]


def run_category(category: str, extra_pytest_args: "list | None" = None,
                  repo: "str | None" = None, timeout: int = 300) -> dict:
    """Run exactly the `tests/*_<category>.py` files under pytest and return a plain verdict dict.

    Reusable core: both `main()` (CLI) and `scripts/quality_matrix.py populate`/the independent
    watcher re-verifier (`simplicio_loop/quality_matrix.py`) call this directly instead of
    re-deriving pass/fail from prose stdout, mirroring `regression_test_gate.py`'s
    `evaluate_regression_gate` pattern.
    """
    repo = repo or REPO
    files = discover(category, tests_dir=os.path.join(repo, "tests"))
    if not files:
        return {
            "schema": "simplicio.test-category-gate/v1",
            "category": category,
            "status": "blocked",
            "ok": False,
            "files": [],
            "returncode": None,
            "duration_s": 0.0,
            "detail": f"no tests/*_{category}.py files exist -- category has zero matching tests",
            "generated_at": _now(),
        }
    started = time.time()
    cmd = [sys.executable, "-m", "pytest", "-q"] + files + list(extra_pytest_args or [])
    try:
        proc = subprocess.run(
            cmd, cwd=repo, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout,
        )
        returncode = proc.returncode
        tail = (proc.stdout or proc.stderr or "").strip()[-2000:]
    except subprocess.TimeoutExpired:
        returncode = None
        tail = f"timed out after {timeout}s"
    duration = time.time() - started
    ok = returncode == 0
    return {
        "schema": "simplicio.test-category-gate/v1",
        "category": category,
        "status": "pass" if ok else "fail",
        "ok": ok,
        "files": files,
        "returncode": returncode,
        "duration_s": round(duration, 3),
        "detail": tail.splitlines()[-1] if tail else "",
        "stdout_tail": tail,
        "generated_at": _now(),
    }


def cmd_run(args: argparse.Namespace) -> int:
    verdict = run_category(args.category, extra_pytest_args=args.pytest_args or None)
    if args.emit_json:
        out = Path(args.emit_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(verdict, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(verdict, indent=2, sort_keys=True))
    if verdict["status"] == "blocked":
        return 2
    return 0 if verdict["ok"] else 1


def cmd_status(_args: argparse.Namespace) -> int:
    report = {c: len(discover(c)) for c in CATEGORIES}
    report["uncategorized"] = len(discover_uncategorized())
    report["total"] = sum(report.values())
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    if args.category == "uncategorized":
        files = discover_uncategorized()
    else:
        files = discover(args.category)
    for f in files:
        print(f)
    return 0


def cmd_selftest(_args: argparse.Namespace) -> int:
    all_files = _list_test_files(TESTS_DIR)
    per_category = {c: discover(c) for c in CATEGORIES}
    uncategorized = discover_uncategorized()

    # every file is accounted for exactly once across the 4 categories + uncategorized
    accounted = sum(len(v) for v in per_category.values()) + len(uncategorized)
    assert accounted == len(all_files), (accounted, len(all_files))

    seen = set()
    for c, files in per_category.items():
        for f in files:
            assert f not in seen, f"{f} matched more than one category"
            seen.add(f)
    for f in uncategorized:
        assert f not in seen, f"{f} both categorized and uncategorized"

    # every known category currently has at least one real test file in this repo (if this ever
    # goes to zero, `run` correctly reports `blocked` rather than a false pass -- see run_category)
    for c in CATEGORIES:
        assert len(per_category[c]) > 0, f"category {c!r} has zero test files"

    # discover() against a nonexistent tests dir returns [] -> run_category must report `blocked`,
    # never a false "pass" for a category with nothing to run.
    empty_verdict = run_category("unit", repo=os.path.join(REPO, "scripts"))
    assert empty_verdict["status"] == "blocked"
    assert empty_verdict["ok"] is False
    assert empty_verdict["files"] == []

    print(
        "[test-categories] selftest OK: "
        f"{len(all_files)} test files total, "
        f"{ {c: len(v) for c, v in per_category.items()} }, "
        f"uncategorized={len(uncategorized)}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="test_categories", description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="run one category's tests/*_<category>.py files")
    p_run.add_argument("--category", choices=CATEGORIES, required=True)
    p_run.add_argument("--emit-json", default=None, help="write the raw verdict dict to this path")
    p_run.add_argument("pytest_args", nargs=argparse.REMAINDER,
                        help="extra args forwarded to pytest verbatim (after --)")
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="counts per category + uncategorized, as JSON")
    p_status.set_defaults(func=cmd_status)

    p_list = sub.add_parser("list", help="list the test files in one category")
    p_list.add_argument("--category", choices=CATEGORIES + ("uncategorized",), required=True)
    p_list.set_defaults(func=cmd_list)

    p_self = sub.add_parser("selftest", help="prove the discovery/partition logic is correct")
    p_self.set_defaults(func=cmd_selftest)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
