"""#283 remaining work: independent watcher + receipt auto-population + full envelope.

RED tests — these must FAIL before the implementation lands (GREEN).

Three gaps remain open on issue #283 (per the issue's own comment thread):
  1. an *independent* watcher that re-derives each quality-matrix lane's verdict
     from the raw gate scripts' output instead of trusting the receipt's
     self-reported status;
  2. auto-populating the receipt from coverage_gate.py / regression_test_gate.py /
     perf_gate.py / quality_matrix_bench.py (which already run in CI but are not
     yet wired into quality-matrix.json automatically);
  3. the full simplicio.quality-gate/v1 envelope (run_id, work_item, nested
     per-category `tests` object) — the schema doc currently lands a subset.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from simplicio_loop import quality_matrix as qm


# ---------------------------------------------------------------------------
# 1. Independent watcher — does NOT trust the receipt's self-reported status
# ---------------------------------------------------------------------------

def test_watcher_rejects_fabricated_passing_receipt(tmp_path, monkeypatch):
    """A receipt that self-reports 'pass' for every lane but whose underlying
    gates actually fail must be flagged BLOCKED by the independent watcher.

    The watcher re-derives each lane from the raw gate scripts, so a fabricated
    receipt cannot sneak through — when the raw gates fail the watcher blocks.
    """
    receipt = {
        "schema": qm.SCHEMA,
        "coverage_threshold": 85.0,
        "requirements": {
            name: {"status": "pass", "proof_ref": "fake", "detail": "lied"}
            for name in qm.REQUIRED_REQUIREMENTS
        },
        "coverage": {"measured": 99.0},
    }
    run = tmp_path / "run"
    run.mkdir()
    (run / qm.RECEIPT_FILENAME).write_text(json.dumps(receipt), encoding="utf-8")

    # Simulate the underlying raw gates actually failing.
    def _fake_probe(gate_name, run_dir):
        if gate_name == "coverage":
            return {"status": "fail", "measured": None, "proof_ref": "cov", "detail": "raw fail"}
        return {"status": "fail", "measured": None, "proof_ref": f"{gate_name}", "detail": "raw fail"}

    monkeypatch.setattr(qm, "_probe_raw_gate", _fake_probe)
    verdict = qm.watchdog_verify(run_dir=str(run), trust_receipt=False)
    # Watcher recomputes from raw scripts; since they fail it must block.
    assert verdict["ready"] is False, verdict
    assert "independent" in verdict["reason_code"] or "quality_coverage" in verdict["reason_code"]


def test_watcher_verifies_real_passing_gates(tmp_path, monkeypatch):
    """When the underlying gate scripts actually pass, the watcher confirms
    VERIFIED without being handed a pre-baked receipt status."""
    receipt = qm.build_quality_matrix_template(change_type="feat")
    run = tmp_path / "run"
    run.mkdir()
    (run / qm.RECEIPT_FILENAME).write_text(json.dumps(receipt), encoding="utf-8")

    # Stub the raw gate probes to return genuine pass evidence.
    def _fake_probe(gate_name, run_dir):
        if gate_name == "coverage":
            return {"status": "pass", "measured": 92.0, "proof_ref": "cov", "detail": ""}
        return {"status": "pass", "measured": None, "proof_ref": f"{gate_name}-evidence", "detail": ""}

    monkeypatch.setattr(qm, "_probe_raw_gate", _fake_probe)
    verdict = qm.watchdog_verify(run_dir=str(run), trust_receipt=False)
    assert verdict["ready"] is True, verdict
    assert all(g["status"] == "pass" for g in verdict["gates"])


def test_watcher_detects_coverage_drift(tmp_path, monkeypatch):
    """If the receipt claims 95% but the raw coverage probe measures 70%, the
    independent watcher must block on coverage_drift, never trust the receipt."""
    receipt = qm.build_quality_matrix_template(change_type="feat")
    receipt["coverage"] = {"measured": 95.0}
    for name in qm.REQUIRED_REQUIREMENTS:
        receipt["requirements"][name] = {"status": "pass", "proof_ref": "x"}
    run = tmp_path / "run"
    run.mkdir()
    (run / qm.RECEIPT_FILENAME).write_text(json.dumps(receipt), encoding="utf-8")

    def _fake_probe(gate_name, run_dir):
        if gate_name == "coverage":
            return {"status": "pass", "measured": 70.0, "proof_ref": "cov", "detail": ""}
        return {"status": "pass", "measured": None, "proof_ref": "x", "detail": ""}

    monkeypatch.setattr(qm, "_probe_raw_gate", _fake_probe)
    verdict = qm.watchdog_verify(run_dir=str(run), trust_receipt=False)
    assert verdict["ready"] is False
    assert any(g["reason_code"] == "quality_coverage_drift" for g in verdict["gates"])


# ---------------------------------------------------------------------------
# 2. Receipt auto-population from the real gate scripts
# ---------------------------------------------------------------------------

def test_populate_fills_receipt_from_raw_gates(tmp_path, monkeypatch):
    """populate() should run the raw gate probes and write their evidence into
    quality-matrix.json, setting real proof_refs and measured coverage."""
    receipt = qm.build_quality_matrix_template(change_type="feat")
    run = tmp_path / "run"
    run.mkdir()
    (run / qm.RECEIPT_FILENAME).write_text(json.dumps(receipt), encoding="utf-8")

    calls = {}

    def _fake_probe(gate_name, run_dir):
        calls[gate_name] = True
        if gate_name == "coverage":
            return {"status": "pass", "measured": 88.0, "proof_ref": "coverage.xml", "detail": ""}
        if gate_name == "benchmark":
            return {"status": "pass", "measured": None, "proof_ref": "perf_gate.json", "detail": ""}
        return {"status": "pass", "measured": None, "proof_ref": f"{gate_name}-evidence", "detail": ""}

    monkeypatch.setattr(qm, "_probe_raw_gate", _fake_probe)
    out = qm.populate_quality_matrix(run_dir=str(run))
    assert out["coverage"]["measured"] == 88.0
    assert out["coverage"]["proof_ref"] == "coverage.xml"
    assert out["requirements"]["benchmark"]["proof_ref"] == "perf_gate.json"
    # every mandatory lane got a real proof_ref
    for name in qm.REQUIRED_REQUIREMENTS:
        assert out["requirements"][name]["proof_ref"], name
    assert "coverage" in calls and "benchmark" in calls


def test_populate_marks_blocked_when_raw_gate_fails(tmp_path, monkeypatch):
    receipt = qm.build_quality_matrix_template(change_type="feat")
    run = tmp_path / "run"
    run.mkdir()
    (run / qm.RECEIPT_FILENAME).write_text(json.dumps(receipt), encoding="utf-8")

    def _fake_probe(gate_name, run_dir):
        if gate_name == "unit":
            return {"status": "fail", "measured": None, "proof_ref": "unit.log", "detail": "1 failed"}
        if gate_name == "coverage":
            return {"status": "pass", "measured": 50.0, "proof_ref": "cov", "detail": ""}
        return {"status": "pass", "measured": None, "proof_ref": "x", "detail": ""}

    monkeypatch.setattr(qm, "_probe_raw_gate", _fake_probe)
    out = qm.populate_quality_matrix(run_dir=str(run))
    assert out["requirements"]["unit"]["status"] == "fail"
    assert out["requirements"]["unit"]["proof_ref"] == "unit.log"
    # Re-evaluating the populated receipt must now BLOCK.
    verdict = qm.evaluate_quality_matrix(str(run))
    assert verdict["ready"] is False


# ---------------------------------------------------------------------------
# 3. Full simplicio.quality-gate/v1 envelope (run_id, work_item, nested tests)
# ---------------------------------------------------------------------------

def test_envelope_carries_run_id_and_work_item(tmp_path):
    receipt = qm.build_quality_matrix_template(change_type="bug")
    receipt["run_id"] = "run-abc-123"
    receipt["work_item"] = {"source": "github", "id": "283", "type": "bug",
                            "title": "Quality Gate obrigatorio"}
    run = tmp_path / "run"
    run.mkdir()
    (run / qm.RECEIPT_FILENAME).write_text(json.dumps(receipt), encoding="utf-8")

    loaded = qm.evaluate_quality_matrix(str(run))
    # the envelope fields survive a load+evaluate round-trip
    assert loaded.get("run_id") == "run-abc-123"
    assert loaded.get("work_item", {}).get("id") == "283"


def test_envelope_nested_tests_object(tmp_path):
    """The full envelope nests per-category evidence under 'tests' so a single
    receipt maps every lane to its category (unit/integration/system/regression/
    benchmark) with its own proof_ref and status."""
    receipt = qm.build_quality_matrix_template(change_type="feat")
    receipt["tests"] = {
        "unit": {"status": "pass", "proof_ref": "tests/unit/log.xml"},
        "integration": {"status": "pass", "proof_ref": "tests/integration/log.xml"},
        "system": {"status": "pass", "proof_ref": "tests/system/log.xml"},
        "regression": {"status": "pass", "proof_ref": "tests/regression/log.xml"},
        "benchmark": {"status": "pass", "proof_ref": "perf_gate.json"},
    }
    run = tmp_path / "run"
    run.mkdir()
    (run / qm.RECEIPT_FILENAME).write_text(json.dumps(receipt), encoding="utf-8")

    verdict = qm.evaluate_quality_matrix(str(run))
    assert "tests" in verdict
    assert verdict["tests"]["unit"]["status"] == "pass"
    assert verdict["tests"]["benchmark"]["proof_ref"] == "perf_gate.json"


def test_schema_validates_full_envelope(tmp_path):
    """The committed schema must accept the full envelope (run_id, work_item,
    nested tests) without rejecting it as an unknown category."""
    import jsonschema  # dev dependency, present in CI

    schema = json.loads(Path("contracts/quality-gate/v1/schema.json").read_text(encoding="utf-8"))
    receipt = qm.build_quality_matrix_template(change_type="feat")
    receipt["run_id"] = "r1"
    receipt["work_item"] = {"source": "github", "id": "283", "type": "feat", "title": "x"}
    receipt["tests"] = {
        "unit": {"status": "pass", "proof_ref": "u"},
        "integration": {"status": "pass", "proof_ref": "i"},
        "system": {"status": "pass", "proof_ref": "s"},
        "regression": {"status": "pass", "proof_ref": "r"},
        "benchmark": {"status": "pass", "proof_ref": "b"},
    }
    jsonschema.validate(receipt, schema)  # must not raise
