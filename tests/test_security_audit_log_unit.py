"""Unit tests for the #289 append-only structured audit log."""
import json

import pytest

from scripts.security_audit_log import append_event, read_events


def test_append_event_writes_one_json_line(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_event(path, event="test.decision", decision="accept", who="agent-1",
                operation="claim", reason="ok")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "test.decision"
    assert record["decision"] == "accept"
    assert record["who"] == "agent-1"
    assert record["operation"] == "claim"
    assert record["schema"] == "simplicio.security-audit-log/v1"
    assert "ts" in record


def test_append_event_is_append_only_across_multiple_calls(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_event(path, event="e1", decision="accept")
    append_event(path, event="e2", decision="reject", reason="denied")
    events = read_events(path)
    assert [e["event"] for e in events] == ["e1", "e2"]
    assert events[1]["decision"] == "reject"
    assert events[1]["reason"] == "denied"


def test_extra_fields_are_merged_without_clobbering_reserved_keys(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_event(path, event="e1", decision="accept", extra={"decision": "should-not-override", "pin_id": "p1"})
    events = read_events(path)
    assert events[0]["decision"] == "accept"
    assert events[0]["pin_id"] == "p1"


def test_read_events_on_missing_file_returns_empty_list(tmp_path):
    assert read_events(tmp_path / "does-not-exist.jsonl") == []


def test_read_events_skips_corrupt_lines(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text('{"event": "good"}\nnot-json\n{"event": "also-good"}\n', encoding="utf-8")
    events = read_events(path)
    assert [e["event"] for e in events] == ["good", "also-good"]


def test_append_event_never_raises_when_directory_is_unwritable(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "audit.jsonl"

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.mkdir", _boom)
    # Must not raise even though the write cannot succeed.
    append_event(path, event="e1", decision="accept")


def test_decision_defaults_to_reject_for_unrecognized_value(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_event(path, event="e1", decision="maybe")
    events = read_events(path)
    assert events[0]["decision"] == "reject"
