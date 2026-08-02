from __future__ import annotations

from dataclasses import dataclass

from simplicio_loop.mapper_run_journal import MapperRunJournal
from simplicio_loop.run_journal import JournalIntegrityError, RunJournal


@dataclass
class FakeAdapter:
    events_by_run: dict[str, list[dict]]

    def replay(self, run_id: str) -> dict:
        events = self.events_by_run.setdefault(run_id, [])
        return {"valid": True, "events": list(events), "compaction": None}

    def append_event(self, run_id, event_type, payload, *, expected_seq):
        events = self.events_by_run.setdefault(run_id, [])
        assert expected_seq == len(events)
        seq = len(events) + 1
        event = {
            "run_id": run_id,
            "seq": seq,
            "event_id": f"event-{seq}",
            "event_type": event_type,
            "payload": dict(payload),
            "event_hash": f"hash-{seq}",
            "created_at": f"2026-08-02T00:00:0{seq}Z",
        }
        events.append(event)
        return {"event_id": event["event_id"]}


def test_run_journal_is_mapper_facade_and_preserves_effect_replay():
    journal = RunJournal("/tmp/operations.sqlite", adapter=FakeAdapter({}))
    assert isinstance(journal, MapperRunJournal)
    started = journal.append(
        "run-1", "run_started", {"goal": "test"}, idempotency_key="start"
    )
    assert started["status"] == "APPENDED"
    journal.checkpoint_before_effect("run-1", "effect-1", {"op": "comment"})
    committed = journal.checkpoint_after_effect(
        "run-1", "effect-1", {"remote_id": "42"}
    )
    assert committed["status"] == "APPENDED"
    assert journal.replay("run-1")["committed_effects"] == {
        "effect-1": {"remote_id": "42"}
    }


def test_legacy_integrity_name_remains_a_mapper_error_alias():
    assert JournalIntegrityError is not None
    assert issubclass(JournalIntegrityError, RuntimeError)


def test_path_only_legacy_journal_is_fail_closed():
    try:
        RunJournal("legacy.sqlite3")
    except JournalIntegrityError as error:
        assert str(error) == "LEGACY_JOURNAL_READ_ONLY"
    else:  # pragma: no cover - the compatibility writer must never return
        raise AssertionError("legacy journal unexpectedly initialized")
