"""Unit tests for simplicio_loop.finding_report (WI-466)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simplicio_loop import finding_report as fr  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_findings(tmp_path, monkeypatch):
    d = tmp_path / "findings"
    monkeypatch.setattr(fr, "_FINDINGS_DIR", d)
    return d


def test_emit_writes_valid_record():
    rec = fr.emit_finding("operate", "reg-001", "high", "simplicio_loop/cli.py:234", True, "boom")
    assert rec["schema"] == "simplicio.finding-report/v1"
    assert rec["stage"] == "operate"
    assert rec["finding_id"] == "reg-001"
    assert rec["severity"] == "high"
    assert rec["confirmed"] is True
    assert rec["fingerprint"]
    lines = (fr._FINDINGS_DIR / "findings.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["finding_id"] == "reg-001"


def test_fingerprint_deterministic():
    a = fr.fingerprint("survey", "dup", "src:x")
    b = fr.fingerprint("survey", "dup", "src:x")
    assert a == b
    c = fr.fingerprint("survey", "dup", "src:y")
    assert a != c


def test_severity_enum_validated():
    with pytest.raises(ValueError):
        fr.emit_finding("decide", "x", "urgent", "s", True)


def test_read_findings_roundtrip():
    fr.emit_finding("preflight", "p1", "low", "a", False)
    fr.emit_finding("watcher", "p2", "medium", "b", True)
    recs = fr.read_findings()
    assert len(recs) == 2
    assert {r["finding_id"] for r in recs} == {"p1", "p2"}
