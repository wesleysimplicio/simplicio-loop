#!/usr/bin/env python3
"""Machine-readable, fail-closed adapter install matrix.

This is deliberately a thin wrapper around ``verify_adapters.py``.  It verifies the
filesystem/install contract in isolated throw-away targets, but does not pretend to
launch the external host runtimes; that boundary is reported as UNVERIFIED.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, Sequence

try:
    from scripts import install_lib
except ModuleNotFoundError:  # direct ``python scripts/runtime_matrix.py`` invocation
    import install_lib  # type: ignore

SCHEMA = "simplicio.runtime-matrix/v1"
TIER1 = {"claude", "codex", "cursor"}
TIER2 = set(install_lib.RUNTIMES) - TIER1
_RESULT_RE = re.compile(r"^(PASS|FAIL)\s+(\S+)", re.MULTILINE)


def _tier(runtime: str) -> str:
    return "tier1" if runtime in TIER1 else "tier2"


def _parse_output(runtime: str, returncode: int, output: str) -> Dict[str, Any]:
    rows = {name: status == "PASS" for status, name in _RESULT_RE.findall(output or "")}
    passed = rows.get(runtime, False)
    # Missing a row is never inferred as a pass, even if the process exits zero.
    ok = bool(returncode == 0 and passed)
    return {
        "runtime": runtime,
        "tier": _tier(runtime),
        "forced_native_bind": runtime in install_lib.FORCED_BIND_RUNTIMES,
        "status": "PASS" if ok else "FAIL",
        "contract_verified": ok,
        "process_returncode": int(returncode),
        "output_row": "PASS" if passed else ("FAIL" if runtime in rows else "MISSING"),
    }


def verify_runtime(runtime: str, root: Path, *, runner=subprocess.run) -> Dict[str, Any]:
    if runtime not in install_lib.RUNTIMES:
        raise ValueError("unknown runtime: %s" % runtime)
    command = [sys.executable, str(Path(__file__).with_name("verify_adapters.py")), runtime]
    try:
        result = runner(command, cwd=str(root), capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=180)
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        return _parse_output(runtime, result.returncode, output)
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "runtime": runtime, "tier": _tier(runtime),
            "forced_native_bind": runtime in install_lib.FORCED_BIND_RUNTIMES,
            "status": "FAIL", "contract_verified": False,
            "process_returncode": 1, "output_row": "ERROR", "error": str(exc),
        }


def build_matrix(runtimes: Iterable[str], root: Path, *, runner=subprocess.run) -> Dict[str, Any]:
    selected = list(runtimes)
    unknown = sorted(set(selected) - set(install_lib.RUNTIMES))
    if unknown:
        raise ValueError("unknown runtime(s): %s" % ", ".join(unknown))
    rows = [verify_runtime(runtime, root, runner=runner) for runtime in selected]
    return {
        "schema": SCHEMA,
        "repo": str(root.resolve()),
        "runtimes": rows,
        "requested": len(rows),
        "passed": sum(bool(row["contract_verified"]) for row in rows),
        "ready": bool(rows) and all(bool(row["contract_verified"]) for row in rows),
        "external_launch_verified": False,
        "external_launch_status": "UNVERIFIED",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="runtime_matrix")
    parser.add_argument("runtimes", nargs="*", choices=sorted(install_lib.RUNTIMES),
                        help="adapters to verify (default: all)")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--tier1", action="store_true", help="verify only Tier 1 adapters")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    runtimes = list(args.runtimes) or sorted(install_lib.RUNTIMES)
    if args.tier1:
        runtimes = [name for name in runtimes if name in TIER1]
    payload = build_matrix(runtimes, Path(args.repo).resolve())
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print("runtime matrix: %s (%d/%d)" %
              ("READY" if payload["ready"] else "BLOCKED", payload["passed"], payload["requested"]))
        for row in payload["runtimes"]:
            print("- %-12s %s [%s]" % (row["runtime"], row["status"], row["tier"]))
        print("- external launch: UNVERIFIED")
    return 0 if payload["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
