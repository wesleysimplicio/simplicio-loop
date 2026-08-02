from __future__ import annotations

from dataclasses import dataclass

import pytest

from simplicio_loop.mapper_run_journal import MapperJournalError, MapperRunJournal


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


def test_mapper_journal_preserves_idempotency_and_projection():
    journal = MapperRunJournal("/tmp/operations.sqlite", adapter=FakeAdapter({}))
    started = journal.append("run-1", "run_started", {"goal": "test"}, idempotency_key="start")
    duplicate = journal.append("run-1", "run_started", {"goal": "changed"}, idempotency_key="start")
    assert started["status"] == "APPENDED"
    assert duplicate["status"] == "DUPLICATE"
    prepared = journal.checkpoint_before_effect("run-1", "effect-1", {"op": "comment"})
    assert prepared["status"] == "APPENDED"
    committed = journal.checkpoint_after_effect("run-1", "effect-1", {"remote_id": "42"})
    assert committed["status"] == "APPENDED"
    assert journal.replay("run-1")["committed_effects"] == {"effect-1": {"remote_id": "42"}}


def test_mapper_journal_rejects_order_and_compaction():
    adapter = FakeAdapter({})
    journal = MapperRunJournal("/tmp/operations.sqlite", adapter=adapter)
    assert journal.append("run-1", "checkpoint", {}, idempotency_key="bad")["reason_code"] == "run_not_started"
    journal.append("run-1", "run_started", {}, idempotency_key="start")
    assert journal.append("run-1", "checkpoint", {}, idempotency_key="bad", expected_sequence=9)["reason_code"] == "sequence_out_of_order"
    adapter.events_by_run["compacted"] = []
    original = adapter.replay
    adapter.replay = lambda run_id: {"valid": True, "events": [], "compaction": {"through_seq": 1}} if run_id == "compacted" else original(run_id)
    with pytest.raises(MapperJournalError, match="COMPACTION"):
        journal.events("compacted")
