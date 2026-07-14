"""System tests: the quality-matrix gate driven through real subprocess CLI boundaries (#278).

Exercises `scripts/quality_matrix.py` (build/check/selftest) and the end-to-end
`simplicio_loop.cli` `oracle --write-receipt` path so the persisted completion receipt and
process exit codes — the actual interface an operator or another tool observes — are proven,
not just the in-process Python functions.
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUALITY_MATRIX_CLI = os.path.join(REPO, "scripts", "quality_matrix.py")
CLI = [sys.executable, "-m", "simplicio_loop.cli"]


def _run(cmd, cwd=REPO, env=None):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=30,
                          stdin=subprocess.DEVNULL, env=full_env)


def test_quality_matrix_cli_selftest_passes():
    r = _run([sys.executable, QUALITY_MATRIX_CLI, "selftest"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS quality-matrix" in r.stdout


def test_quality_matrix_cli_build_then_check_round_trip(tmp_path):
    built = _run([sys.executable, QUALITY_MATRIX_CLI, "build", "--run-dir", str(tmp_path)])
    assert built.returncode == 0, built.stdout + built.stderr
    receipt_path = tmp_path / "quality-matrix.json"
    assert receipt_path.exists()
    template = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert template["schema"] == "simplicio.quality-matrix/v1"

    # A freshly built template is all-unset — check must fail closed, never pass by default.
    checked = _run([sys.executable, QUALITY_MATRIX_CLI, "check", "--run-dir", str(tmp_path)])
    assert checked.returncode == 1, checked.stdout + checked.stderr
    payload = json.loads(checked.stdout)
    assert payload["ready"] is False

    for name in template["requirements"]:
        template["requirements"][name] = {"status": "pass", "proof_ref": f"tests/{name}"}
    template["coverage"] = {"measured": 92.0}
    receipt_path.write_text(json.dumps(template), encoding="utf-8")
    checked_ok = _run([sys.executable, QUALITY_MATRIX_CLI, "check", "--run-dir", str(tmp_path)])
    assert checked_ok.returncode == 0, checked_ok.stdout + checked_ok.stderr
    assert json.loads(checked_ok.stdout)["ready"] is True


def test_quality_matrix_build_rejects_invalid_coverage_threshold(tmp_path):
    r = _run([sys.executable, QUALITY_MATRIX_CLI, "build", "--run-dir", str(tmp_path),
             "--coverage-threshold", "150"])
    assert r.returncode != 0, r.stdout + r.stderr


def _seed_loop_and_run(loop, run_dir, *, quality_matrix):
    loop.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    (loop / "scratchpad.md").write_text("---\ncompletion_promise: \"DONE\"\n---\ngoal\n", encoding="utf-8")
    (loop / "anchor.json").write_text(json.dumps({"criteria": [{"id": "AC1", "status": "done"}]}), encoding="utf-8")
    (loop / "watcher_challenge.json").write_text(json.dumps({
        "challenge": "c", "written_at": "2026-07-10T00:00:00Z"
    }), encoding="utf-8")
    (loop / "watcher_state.json").write_text(json.dumps({
        "match": True, "status": "MEASURED", "challenge": "c", "checked_at": "2026-07-10T00:00:01Z"
    }), encoding="utf-8")
    files = {
        "manifest.json": {"delivery_target": "verified"},
        "task-contract.json": {"schema": "simplicio.task-contract-collection/v1"},
        "mapper-context.json": {"handoff": {}},
        "operator-receipt.json": {"schema": "simplicio.operator-receipt/v0"},
        "evidence-receipt.json": {"schema": "simplicio.evidence-receipt/v1", "status": "VERIFIED",
                                  "criteria": [{"id": "AC1", "verification_state": "verified"}]},
        "delivery-receipt.json": {"schema": "simplicio.delivery-receipt/v1", "target": "verified",
                                  "current_state": "verified", "ready": True, "source_kind": "local",
                                  "source_payload": {"evidence_receipt": "evidence-receipt.json", "criteria_verified": 1}},
    }
    for name, payload in files.items():
        (run_dir / name).write_text(json.dumps(payload), encoding="utf-8")
    if quality_matrix is not None:
        (run_dir / "quality-matrix.json").write_text(json.dumps(quality_matrix), encoding="utf-8")


def test_cli_oracle_write_receipt_blocks_close_without_quality_matrix(tmp_path):
    loop = tmp_path / "loop"
    run_dir = tmp_path / "run"
    _seed_loop_and_run(loop, run_dir, quality_matrix=None)
    r = _run(CLI + ["oracle", "--loop-dir", str(loop), "--run-dir", str(run_dir),
                    "--response-text", "<promise>DONE</promise>", "--write-receipt"])
    assert r.returncode == 1, r.stdout + r.stderr
    receipt = json.loads((run_dir / "completion-receipt.json").read_text(encoding="utf-8"))
    assert receipt["ready"] is False
    assert receipt["reason_code"] == "quality_matrix_missing"


def test_cli_oracle_write_receipt_reports_coverage_fields_and_succeeds_when_complete(tmp_path):
    loop = tmp_path / "loop"
    run_dir = tmp_path / "run"
    quality_matrix = {
        "schema": "simplicio.quality-matrix/v1",
        "coverage_threshold": 85,
        "requirements": {
            name: {"status": "pass", "proof_ref": f"tests/{name}"}
            for name in ("implementation", "unit", "integration", "system", "regression", "benchmark")
        },
        "coverage": {"measured": 88.5},
    }
    _seed_loop_and_run(loop, run_dir, quality_matrix=quality_matrix)
    r = _run(CLI + ["oracle", "--loop-dir", str(loop), "--run-dir", str(run_dir),
                    "--response-text", "<promise>DONE</promise>", "--write-receipt"])
    assert r.returncode == 0, r.stdout + r.stderr
    receipt = json.loads((run_dir / "completion-receipt.json").read_text(encoding="utf-8"))
    assert receipt["ready"] is True
    assert receipt["verdict"] == "COMPLETE"
    assert receipt["coverage_threshold"] == 85.0
    assert receipt["coverage_measured"] == 88.5
    assert receipt["artifacts"]["quality_matrix"].endswith("quality-matrix.json")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_quality_matrix_system")
