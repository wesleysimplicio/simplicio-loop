import hashlib
import json

import pytest

from simplicio_loop.phase_events import build_phase_event
from simplicio_loop.recovery import (
    AC_RECEIPT_SCHEMA,
    RecoveryError,
    build_ac_evidence_receipt,
    build_cursor,
    persist_cursor,
    reconcile_after_crash,
    validate_ac_evidence_receipt,
)


IDENTITY = {"run_id": "run-1", "work_item_id": "wi-1", "attempt_id": "a-1", "actor": "codex@host-a"}
CURSOR_IDENTITY = {**IDENTITY, "environment_id": "host-a/python-3.12"}


def event(sequence, before, after, event_id=None):
    return build_phase_event(
        **IDENTITY, cause="worker", sequence=sequence, event_id=event_id or "e-%d" % sequence,
        from_phase=before, to_phase=after,
    )


def test_recovery_cursor_is_idempotent_and_terminal_work_is_not_reexecuted():
    first = event(1, None, "intake")
    second = event(2, "intake", "mapping")
    cursor = build_cursor(**CURSOR_IDENTITY)
    cursor, first_diag = reconcile_after_crash([first, second], cursor)
    assert first_diag["status"] == "resumed"
    cursor, replay_diag = reconcile_after_crash([first, second], cursor)
    assert replay_diag["status"] == "unchanged"
    assert replay_diag["execution_allowed"] is True
    tail = event(3, "mapping", "planning")
    tail = event(3, "mapping", "planning")
    cursor, _ = reconcile_after_crash([tail], cursor)
    for seq, before, after in [(4, "planning", "executing"), (5, "executing", "validating"),
                               (6, "validating", "watching"), (7, "watching", "delivering"),
                               (8, "delivering", "done")]:
        cursor, _ = reconcile_after_crash([event(seq, before, after)], cursor)
    assert cursor["terminal"] is True
    _, diag = reconcile_after_crash([], cursor)
    assert diag["status"] == "complete"
    assert diag["execution_allowed"] is False


def test_recovery_rejects_gap_identity_drift_and_tampered_duplicate():
    cursor = build_cursor(**CURSOR_IDENTITY)
    with pytest.raises(RecoveryError, match="sequence gap"):
        reconcile_after_crash([event(2, "intake", "mapping")], cursor)
    with pytest.raises(RecoveryError, match="identity mismatch"):
        reconcile_after_crash([dict(event(1, None, "intake", event_id="foreign"), actor="claude@host-b")], cursor)
    first = event(1, None, "intake")
    with pytest.raises(RecoveryError, match="conflicting duplicate"):
        reconcile_after_crash([first, dict(first, cause="tampered")], cursor)


def test_cursor_persistence_is_valid_json_and_replaces_atomically(tmp_path):
    path = tmp_path / "cursor.json"
    persist_cursor(path, build_cursor(**CURSOR_IDENTITY))
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["schema"] == "simplicio.loop-cursor/v1"
    assert not list(tmp_path.glob(".*cursor.json.*"))


def evidence_item(claim_type="measured"):
    return {"command": "pytest -q tests/test_recovery.py", "exit_code": 0,
            "artifact_hash": hashlib.sha256(b"artifact").hexdigest(),
            "provenance": "stdout:/tmp/gate.log", "claim_type": claim_type}


def test_ac_receipt_requires_every_criterion_and_hashes_identity():
    receipt = build_ac_evidence_receipt(
        **IDENTITY, environment_id="host-a/python-3.12", observed_at="2026-07-11T00:00:00Z",
        criteria=[{"id": "AC1", "status": "verified", "evidence": [evidence_item()]},
                   {"id": "AC2", "status": "verified", "evidence": [evidence_item("replayed")] }],
    )
    assert receipt["schema"] == AC_RECEIPT_SCHEMA
    assert validate_ac_evidence_receipt(receipt, required_criteria=["AC1", "AC2"],
                                        expected_identity={"work_item_id": "wi-1"}) == receipt
    with pytest.raises(RecoveryError, match="AC set mismatch"):
        validate_ac_evidence_receipt(receipt, required_criteria=["AC1", "AC3"])
    altered = dict(receipt, actor="other@host")
    with pytest.raises(RecoveryError, match="receipt hash mismatch"):
        validate_ac_evidence_receipt(altered)


def test_ac_receipt_rejects_estimated_only_or_nonzero_evidence():
    with pytest.raises(RecoveryError, match="no reproducible"):
        build_ac_evidence_receipt(
            **IDENTITY, environment_id="host-a", observed_at="now",
            criteria=[{"id": "AC1", "status": "verified", "evidence": [evidence_item("estimated")]}],
        )
    with pytest.raises(RecoveryError, match="did not exit zero"):
        build_ac_evidence_receipt(
            **IDENTITY, environment_id="host-a", observed_at="now",
            criteria=[{"id": "AC1", "status": "verified", "evidence": [{**evidence_item(), "exit_code": 1}]}],
        )
