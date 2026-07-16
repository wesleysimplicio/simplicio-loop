"""Issue #432 — stage-agent conformance suite: prove the 12 canonical roles
materialize and produce equivalent receipts on every supported runtime.

Run: python3 -m pytest tests/test_conformance_suite.py -q
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUITE = REPO / "scripts" / "conformance_suite.py"


def _run(args):
    return subprocess.run(
        [sys.executable, str(SUITE), *args],
        cwd=str(REPO), capture_output=True, text=True,
    )


def test_suite_runs_all_runtimes_and_passes_gate():
    """The full suite must exit 0 (every available runtime passes its gate)."""
    proc = _run([])
    assert proc.returncode == 0, proc.stderr
    assert "gate=pass" in proc.stdout, proc.stdout


def test_json_report_has_canonical_roles_and_results():
    out = REPO / "conformance-report.json"
    if out.exists():
        out.unlink()
    proc = _run(["--json", str(out)])
    assert proc.returncode == 0, proc.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["schema"] == "simplicio.conformance/v1"
    assert data["issue"] == 432
    # All 12 canonical roles must be present.
    assert len(data["roles_canonical"]) == 12
    # Every runtime produces a result entry.
    assert len(data["results"]) == 15
    # Simplicio Agent must be installed + pass all three modes.
    sa = next(r for r in data["results"] if r["runtime"] == "simplicio_agent")
    assert sa["installed"] is True
    assert sa["sandbox_passed"] is True
    assert sa["receipt_equivalent"] is True
    out.unlink()


def test_unknown_runtime_is_rejected():
    proc = _run(["not_a_runtime"])
    assert proc.returncode == 2
    assert "unknown runtime" in proc.stderr
