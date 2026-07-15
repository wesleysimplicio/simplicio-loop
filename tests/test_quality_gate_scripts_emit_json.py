"""#283: scripts/regression_test_gate.py, scripts/perf_gate.py and scripts/coverage_gate.py all
gained an `--emit-json` path so `scripts/quality_matrix.py populate` (and an independent
re-verifier) can consume the exact structured verdict each gate computed, instead of re-deriving
pass/fail from prose stdout. Coverage-gate's own full-suite run is exercised elsewhere/manually
(too slow for this fast unit test); here we only assert its CLI wiring is correct.
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def test_regression_gate_emit_json_matches_stdout_verdict(tmp_path):
    out = tmp_path / "regression-report.json"
    proc = subprocess.run(
        [PY, os.path.join(REPO, "scripts", "regression_test_gate.py"), "--base", "HEAD", "--emit-json", str(out)],
        cwd=REPO, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30,
    )
    assert proc.returncode == 0  # no diff vs HEAD itself
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema"] == "simplicio.regression-gate/v1"
    assert payload["ok"] is True
    assert payload["base"] == "HEAD"
    assert payload["changed_files"] == []


def test_regression_gate_evaluate_function_is_importable_and_reusable():
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    from regression_test_gate import evaluate_regression_gate

    verdict = evaluate_regression_gate("HEAD", [])
    assert verdict["ok"] is True
    assert verdict["schema"] == "simplicio.regression-gate/v1"


def test_perf_gate_emit_json_writes_full_report(tmp_path):
    out = tmp_path / "perf-report.json"
    proc = subprocess.run(
        [PY, os.path.join(REPO, "scripts", "perf_gate.py"), "--cycles", "5", "--emit-json", str(out)],
        cwd=REPO, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60,
    )
    assert proc.returncode in (0, 1)
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema"] == "simplicio.perf-gate/v1"
    assert "report" in payload and "convergence" in payload["report"]
    assert isinstance(payload["ok"], bool)


def test_coverage_gate_cli_declares_emit_json_flag():
    proc = subprocess.run(
        [PY, os.path.join(REPO, "scripts", "coverage_gate.py"), "--help"],
        cwd=REPO, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=15,
    )
    assert proc.returncode == 0
    assert "--emit-json" in proc.stdout


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_quality_gate_scripts_emit_json")
