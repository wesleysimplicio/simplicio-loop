import json

import pytest

from simplicio_loop.attempt_journal import (
    AttemptJournal,
    AttemptJournalError,
    SCHEMA,
    build_observation,
)


def _event(kind="action", event_id="e1", sequence=1):
    return build_observation(
        run_id="run-1", work_item_id="WI-1", attempt_id="A-1", actor="codex@host-a",
        kind=kind, payload={"command": "pytest"}, sequence=sequence, event_id=event_id,
        ac_ids=["AC-1"], claim_type="MEASURED" if kind == "validation" else "UNVERIFIED",
    )


def test_typed_append_is_hash_chained_and_idempotent(tmp_path):
    journal = AttemptJournal(tmp_path / "attempt.jsonl")
    first = journal.append(_event())
    assert first["schema"] == SCHEMA
    assert first["sequence"] == 1
    assert journal.append(_event()) == first
    second = journal.append(_event("failure", "e2", 1))
    assert second["sequence"] == 2
    assert second["prev_hash"] == first["hash"]
    assert [item["event_id"] for item in journal.replay()] == ["e1", "e2"]


def test_duplicate_event_id_with_changed_payload_fails_closed(tmp_path):
    journal = AttemptJournal(tmp_path / "attempt.jsonl")
    journal.append(_event())
    with pytest.raises(AttemptJournalError, match="different envelope"):
        journal.append({**_event(), "payload": {"command": "rm -rf"}})


def test_failure_fingerprint_survives_provider_handoff(tmp_path):
    first = AttemptJournal(tmp_path / "first.jsonl")
    second = AttemptJournal(tmp_path / "second.jsonl")
    event = _event("failure", "failure-1")
    event["payload"] = {"message": "timeout at line 42"}
    first.append(event)
    exported = first.export()
    second.import_events(exported)
    assert second.export()[0]["failure_fingerprint"] == exported[0]["failure_fingerprint"]


def test_replay_detects_tamper_and_import_is_deterministic(tmp_path):
    legacy = tmp_path / "legacy.jsonl"
    legacy.write_text(json.dumps({"iteration": 1, "action": "map", "gate": "pass"}) + "\n",
                      encoding="utf-8")
    journal = AttemptJournal(tmp_path / "attempt.jsonl")
    migrated = journal.import_legacy(legacy, run_id="r", work_item_id="w", attempt_id="a", actor="a@h")
    assert migrated[0]["kind"] == "validation"
    assert journal.import_legacy(legacy, run_id="r", work_item_id="w", attempt_id="a", actor="a@h") == migrated
    rows = journal.path.read_text(encoding="utf-8").splitlines()
    altered = json.loads(rows[0])
    altered["payload"]["legacy"]["action"] = "tampered"
    journal.path.write_text(json.dumps(altered) + "\n", encoding="utf-8")
    with pytest.raises(AttemptJournalError, match="hash mismatch"):
        journal.replay()


def test_validation_claim_requires_explicit_supported_kind():
    with pytest.raises(AttemptJournalError, match="unsupported observation kind"):
        _event("completion")
