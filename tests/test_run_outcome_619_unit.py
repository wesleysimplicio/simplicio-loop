import json
from datetime import datetime, timezone

import pytest

from simplicio_loop.run_outcome import persist_run_outcome, resolve_run_outcome
from simplicio_loop.extension_manifest import extension_handshake


def _status(tmp_path, phase="done", receipt=True, **changes):
    run = tmp_path / "run-1"
    run.mkdir(exist_ok=True)
    manifest = {"run_id": "run-1", "source_kind": "github", "issue_ref": "#619", "task_contract_hash": "abc"}
    if receipt:
        payload = {"run_id": "run-1", "ready": True, "verdict": "COMPLETE", "tag": "MEASURED",
                   "generated_at": "2026-07-22T00:00:00Z",
                   "source_binding": {"kind": "github", "identity": "#619", "digest": "abc"}}
        payload.update(changes)
        (run / "completion-receipt.json").write_text(json.dumps(payload), encoding="utf-8")
    return {"run_dir": str(run), "manifest": manifest, "state": {"phase": phase}}


@pytest.mark.parametrize("phase,expected,code", [
    ("blocked", "BLOCKED", 20), ("cancelled", "CANCELLED", 21),
    ("partial", "PARTIAL", 22), ("delivering", "PARTIAL", 22),
])
def test_terminal_decision_table(tmp_path, phase, expected, code):
    result = resolve_run_outcome(_status(tmp_path, phase=phase, receipt=False))
    assert (result["outcome"], result["exit_code"]) == (expected, code)


def test_only_oracle_authorized_receipt_succeeds(tmp_path):
    result = resolve_run_outcome(_status(tmp_path), now=datetime(2026, 7, 22, 1, tzinfo=timezone.utc))
    assert result["outcome"] == "COMPLETE" and result["exit_code"] == 0
    assert result["completion_receipt"]["sha256"]


@pytest.mark.parametrize("changes,reason", [
    ({"run_id": "other"}, "cross_run"),
    ({"source_binding": {"kind": "github", "identity": "#620", "digest": "abc"}}, "source_mismatch"),
    ({"generated_at": "2000-01-01T00:00:00Z"}, "stale"),
    ({"ready": False}, "oracle_not_authorized"),
    ({"verdict": "BLOCKED"}, "oracle_not_authorized"),
    ({"tag": "UNVERIFIED"}, "oracle_not_authorized"),
])
def test_tampered_stale_cross_run_receipts_fail_closed(tmp_path, changes, reason):
    result = resolve_run_outcome(_status(tmp_path, **changes), now=datetime(2026, 7, 22, 1, tzinfo=timezone.utc))
    assert result["exit_code"] == 23
    assert reason in result["completion_receipt"]["validation"]


def test_done_without_receipt_is_invalid(tmp_path):
    result = resolve_run_outcome(_status(tmp_path, receipt=False))
    assert (result["outcome"], result["exit_code"]) == ("INVALID_RECEIPT", 23)


def test_extension_handshake_exposes_read_only_outcome_capability():
    handshake = extension_handshake()
    assert "run-outcome/v1" in handshake["capabilities"]
    assert handshake["completion_authority"] == "core-completion-oracle-only"


def test_missing_run_directory_does_not_write_into_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = persist_run_outcome({"manifest": {}, "state": {"phase": "blocked"}})
    assert result["exit_code"] == 20
    assert not (tmp_path / "run-outcome.json").exists()
