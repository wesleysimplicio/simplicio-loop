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
ISSUE_183_CRITERION_6 = "Codex numa máquina e Claude em outra podem consumir a mesma fila segura."
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
        result = runner(command, cwd=str(root), stdin=subprocess.DEVNULL, capture_output=True, text=True,
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


def attempt_external_launch(runtime: str, *,
                             prompt: str = "Reply with exactly: SIMPLICIO_RUNTIME_MATRIX_PROBE_OK",
                             timeout: int = 60) -> Dict[str, Any]:
    """Genuinely attempt a real, non-interactive launch of ``runtime``'s CLI (issue
    #287) -- never a fabricated pass. Only ``codex``/``claude`` have a real driver
    wired (``simplicio_loop/runtime_drivers.py``); every other runtime honestly
    reports ``UNVERIFIED`` (no probe implemented) rather than a guessed result. A
    missing binary or a real invocation failure (auth/policy block, timeout) is
    reported as ``UNAVAILABLE``/``FAIL`` with the actual observed detail -- this
    function's job is to *attempt* the check, not to guarantee it passes.
    """
    try:
        from simplicio_loop.runtime_drivers import driver_for_runtime
    except ImportError as exc:  # pragma: no cover - packaging edge case
        return {"runtime": runtime, "attempted": False, "status": "UNVERIFIED",
                "detail": f"runtime_drivers unavailable: {exc}"}
    driver = driver_for_runtime(runtime)
    if driver is None:
        return {"runtime": runtime, "attempted": False, "status": "UNVERIFIED",
                "detail": "no real launch driver implemented for this runtime"}
    if not driver.is_installed():
        return {"runtime": runtime, "attempted": True, "status": "UNAVAILABLE",
                "detail": f"{driver.binary} binary not found on PATH"}
    result = driver.execute(prompt, timeout=timeout)
    return {
        "runtime": runtime,
        "attempted": True,
        "status": "PASS" if result.ok else "FAIL",
        "exit_status": result.exit_status,
        "stop_reason": result.stop_reason,
        "duration_seconds": result.duration_seconds,
        "detail": result.stdout.strip()[:200] if result.ok else (result.error or "launch failed"),
    }


def build_matrix(runtimes: Iterable[str], root: Path, *, runner=subprocess.run,
                  attempt_launch: bool = False) -> Dict[str, Any]:
    selected = list(runtimes)
    unknown = sorted(set(selected) - set(install_lib.RUNTIMES))
    if unknown:
        raise ValueError("unknown runtime(s): %s" % ", ".join(unknown))
    rows = [verify_runtime(runtime, root, runner=runner) for runtime in selected]
    launch_rows: List[Dict[str, Any]] = []
    if attempt_launch:
        # #287: a real, best-effort attempt -- opt-in because it shells out to real
        # CLIs (slower, environment-dependent) unlike the rest of this fast, hermetic
        # matrix build. Default (``attempt_launch=False``) keeps the prior hardcoded
        # ``False``/``UNVERIFIED`` behavior byte-for-byte for existing callers.
        launch_rows = [attempt_external_launch(runtime) for runtime in selected
                       if runtime in {"codex", "claude"}]
    if attempt_launch:
        # Per-runtime real evidence, independent of the aggregate below. A structural
        # block on one runtime (e.g. Claude behind an org policy) must never bury the
        # genuine, individually-measured PASS/FAIL of another (e.g. Codex) -- see #287
        # comment history: "Codex real execution: done" while Claude stayed blocked.
        # Only added when ``attempt_launch`` is requested, so the default (no-flag)
        # payload stays byte-for-byte identical to the prior hardcoded contract.
        launch_by_runtime = {row["runtime"]: row for row in launch_rows}
        for row in rows:
            launch_row = launch_by_runtime.get(row["runtime"])
            if launch_row is not None:
                row["external_launch_status"] = launch_row["status"] if launch_row["attempted"] else "UNVERIFIED"
                row["external_launch_verified"] = bool(launch_row["attempted"] and launch_row["status"] == "PASS")
            else:
                row["external_launch_status"] = "UNVERIFIED"
                row["external_launch_verified"] = False
    launch_attempted = bool(launch_rows) and all(row["attempted"] for row in launch_rows)
    launch_verified = launch_attempted and all(row["status"] == "PASS" for row in launch_rows)
    payload = {
        "schema": SCHEMA,
        "repo": str(root.resolve()),
        "runtimes": rows,
        "requested": len(rows),
        "passed": sum(bool(row["contract_verified"]) for row in rows),
        "ready": bool(rows) and all(bool(row["contract_verified"]) for row in rows),
        # Aggregate: only True when every attempted runtime in this call genuinely
        # passed a real launch. This intentionally stays False while Claude is
        # structurally blocked even though Codex passes -- see the per-runtime
        # ``external_launch_verified`` on each row in ``runtimes`` (and
        # ``external_launch_verified_by_runtime`` below) for the ungrouped truth.
        "external_launch_verified": launch_verified,
        "external_launch_status": "MEASURED" if launch_rows else "UNVERIFIED",
    }
    if launch_rows:
        payload["external_launch_attempts"] = launch_rows
        payload["external_launch_verified_by_runtime"] = {
            row["runtime"]: bool(row["attempted"] and row["status"] == "PASS") for row in launch_rows
        }
    return payload


def build_issue_183_criterion6(root: Path, *, runner=subprocess.run) -> Dict[str, Any]:
    """Audit the local, machine-readable portion of issue #183 criterion 6.

    This proves the adapter/install contract for Codex + Claude against the same repo and keeps
    all truly external boundaries explicitly UNVERIFIED instead of implying cross-machine success.
    """
    matrix = build_matrix(["codex", "claude"], root, runner=runner)
    rows = {row["runtime"]: row for row in matrix["runtimes"]}
    local_ready = matrix["ready"]
    return {
        "criterion_id": 6,
        "criterion_text": ISSUE_183_CRITERION_6,
        "tag": "MEASURED" if local_ready else "UNVERIFIED",
        "local_contract_status": "PASS" if local_ready else "FAIL",
        "local_contract_verified": local_ready,
        "same_queue_adapter_contracts": {
            "codex": rows["codex"],
            "claude": rows["claude"],
        },
        "required_runtime_bind_policy": {
            "codex": bool(rows["codex"]["forced_native_bind"]),
            "claude": bool(rows["claude"]["forced_native_bind"]),
        },
        "physical_machine_verified": False,
        "physical_machine_status": "UNVERIFIED",
        "tls_deploy_verified": False,
        "tls_deploy_status": "UNVERIFIED",
        "external_release_verified": False,
        "external_release_status": "UNVERIFIED",
        "scope_note": (
            "Local repo install/bind contract is audited here; physical multi-machine execution, "
            "transport security/deploy wiring and external release evidence remain UNVERIFIED."
        ),
        "artifacts": {
            "runtime_matrix_schema": matrix["schema"],
            "runtime_count": matrix["requested"],
            "runtime_passed": matrix["passed"],
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="runtime_matrix")
    parser.add_argument("runtimes", nargs="*", choices=sorted(install_lib.RUNTIMES),
                        help="adapters to verify (default: all)")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--tier1", action="store_true", help="verify only Tier 1 adapters")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--attempt-launch", action="store_true",
                        help="genuinely attempt a real headless codex/claude CLI launch (#287) "
                             "instead of leaving external_launch_verified hardcoded UNVERIFIED")
    args = parser.parse_args(argv)
    runtimes = list(args.runtimes) or sorted(install_lib.RUNTIMES)
    if args.tier1:
        runtimes = [name for name in runtimes if name in TIER1]
    payload = build_matrix(runtimes, Path(args.repo).resolve(), attempt_launch=args.attempt_launch)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print("runtime matrix: %s (%d/%d)" %
              ("READY" if payload["ready"] else "BLOCKED", payload["passed"], payload["requested"]))
        for row in payload["runtimes"]:
            print("- %-12s %s [%s]" % (row["runtime"], row["status"], row["tier"]))
        if payload.get("external_launch_attempts"):
            for attempt in payload["external_launch_attempts"]:
                print("- launch %-8s %s (%s)" % (attempt["runtime"], attempt["status"], attempt["detail"]))
        else:
            print("- external launch: %s" % payload["external_launch_status"])
    return 0 if payload["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
