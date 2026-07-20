"""Example mandatory quality provider for the Simplicio Loop (issue #613).

This is a REAL, working provider (not a mock) that the `conduct_run` quality
gate can load via `simplicio_loop.quality_providers.simplicio_loop_quality`.
It exercises the full contract: capability_negotiate() -> version + caps, and
run() -> structured QualityResult written to quality-matrix.json.

The provider runs the project's own test/check suite as the quality signal and
never spawns its own scheduler/queue/process-pool -- it only shells out to the
repo's own `scripts/check.py` using the Loop-provided repo/worktree paths.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROVIDER_VERSION = "1.0.0"


def capability_negotiate() -> dict:
    """Declare protocol version and capabilities.

    Returns a dict with at least ``version`` (semver string) and any
    capability flags the Loop may probe via ``QualityProviderSpec.supports``.
    """
    return {
        "version": PROVIDER_VERSION,
        "capabilities": {
            "structured_findings": True,
            "cancel_token": True,
            "per_run_matrix": True,
        },
    }


def run(
    *,
    run_id: str,
    tasks: list,
    attempt: int,
    repo: str,
    worktree: str,
    head: str,
    diff_hash: str,
    policy: str,
    cancel_token=None,
) -> dict:
    """Execute the quality layer for one run.

    Runs the repository's own ``scripts/check.py`` as the quality signal.
    Returns a dict with ``status`` in {PASS, FAIL, BLOCKED} plus findings and
    receipts. A non-zero check => FAIL (returns to Loop recovery), never a
    silent pass.
    """
    repo_path = Path(repo).resolve()
    check_script = repo_path / "scripts" / "check.py"
    findings: list = []
    receipts: list = []

    if not check_script.exists():
        return {
            "status": "BLOCKED",
            "findings": [{"level": "error", "message": "scripts/check.py not found"}],
            "receipts": [],
            "detail": "quality provider could not locate scripts/check.py",
        }

    proc = subprocess.run(
        [sys.executable, str(check_script)],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        timeout=120,
    )
    receipts.append(str(repo_path / "scripts" / "check.py"))

    if proc.returncode == 0:
        status = "PASS"
        detail = "scripts/check.py passed"
    else:
        status = "FAIL"
        detail = (proc.stdout or proc.stderr or "check.py failed").strip()[:2000]
        findings.append({"level": "fail", "message": detail})

    return {
        "status": status,
        "findings": findings,
        "receipts": receipts,
        "detail": detail,
    }
