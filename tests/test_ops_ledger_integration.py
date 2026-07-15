import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from simplicio_loop.ops_ledger import CONTEXT_SCHEMA, EventLedger, LedgerError


def _legacy(path):
    """The pre-context v1 API remains available only by explicit opt-in."""
    return EventLedger(path, compatibility=True)


def _context(**overrides):
    value = {
        "run_id": "run-1",
        "wave_id": "wave-1",
        "lane": "lane-a",
        "owner": "worker-1",
        "session": "session-1",
        "reason_code": "claim",
    }
    value.update(overrides)
    return value


def test_ledger_append_replay_is_hash_chained(tmp_path):
    ledger = _legacy(tmp_path / "events.jsonl")
    first = ledger.append("claimed", {"task": "T1"}, event_id="e1")
    second = ledger.append("completed", {"task": "T1"}, event_id="e2")
    events = ledger.replay()
    assert [event["sequence"] for event in events] == [1, 2]
    assert second["prev_hash"] == first["hash"]


def test_duplicate_event_id_is_idempotent(tmp_path):
    ledger = _legacy(tmp_path / "events.jsonl")
    first = ledger.append("heartbeat", {"worker": "w1"}, event_id="same")
    again = ledger.append("heartbeat", {"worker": "w1"}, event_id="same")
    assert again == first
    assert len(ledger.replay()) == 1


def test_duplicate_event_id_with_different_payload_is_rejected(tmp_path):
    ledger = _legacy(tmp_path / "events.jsonl")
    ledger.append("heartbeat", {"worker": "w1"}, event_id="same")
    try:
        ledger.append("heartbeat", {"worker": "w2"}, event_id="same")
    except LedgerError as exc:
        assert "different payload" in str(exc)
    else:
        raise AssertionError("conflicting duplicate event was accepted")


def test_tampering_fails_closed(tmp_path):
    path = tmp_path / "events.jsonl"
    ledger = _legacy(path)
    ledger.append("claimed", {"task": "T1"}, event_id="e1")
    value = json.loads(path.read_text(encoding="utf-8"))
    value["payload"]["task"] = "T2"
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")
    try:
        ledger.replay()
    except LedgerError as exc:
        assert "hash" in str(exc)
    else:
        raise AssertionError("tampered ledger unexpectedly replayed")


def test_recover_torn_trailing_line_and_continue(tmp_path):
    path = tmp_path / "events.jsonl"
    ledger = _legacy(path)
    ledger.append("claimed", {"task": "T1"}, event_id="e1")
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"schema":"simplicio.ops-event/v1"')
    try:
        ledger.replay()
    except LedgerError:
        pass
    else:
        raise AssertionError("torn tail unexpectedly passed strict replay")
    assert len(ledger.replay(recover_trailing=True)) == 1
    ledger.append("completed", {"task": "T1"}, event_id="e2")
    assert len(ledger.replay()) == 2


def test_concurrent_append_preserves_sequence(tmp_path):
    ledger = _legacy(tmp_path / "events.jsonl")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: ledger.append("heartbeat", {"i": i}, event_id="e%d" % i), range(32)))
    events = ledger.replay()
    assert len(events) == 32
    assert [event["sequence"] for event in events] == list(range(1, 33))


def test_strict_context_is_hash_bound_and_replayable(tmp_path):
    path = tmp_path / "events.jsonl"
    ledger = EventLedger(path)
    event = ledger.append("claimed", {"task": "T1"}, event_id="strict-1",
                          context=_context())
    assert event["context_schema"] == CONTEXT_SCHEMA
    assert event["context"]["run_id"] == "run-1"
    assert ledger.replay() == [event]
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["context"]["run_id"] = "run-evil"
    path.write_text(json.dumps(tampered) + "\n", encoding="utf-8")
    try:
        ledger.replay()
    except LedgerError as exc:
        assert "hash" in str(exc)
    else:
        raise AssertionError("tampered context unexpectedly replayed")


def test_strict_context_rejects_missing_required_fields(tmp_path):
    ledger = EventLedger(tmp_path / "events.jsonl")
    value = _context()
    del value["owner"]
    try:
        ledger.append("claimed", {"task": "T1"}, context=value)
    except LedgerError as exc:
        assert "owner" in str(exc)
    else:
        raise AssertionError("context without owner unexpectedly accepted")


def test_receipt_events_require_non_empty_receipts(tmp_path):
    ledger = EventLedger(tmp_path / "events.jsonl")
    try:
        ledger.append("evidence_receipt", {"receipt_id": "r1"},
                      context=_context())
    except LedgerError as exc:
        assert "receipts" in str(exc)
    else:
        raise AssertionError("receipt event without receipts unexpectedly accepted")
    event = ledger.append(
        "evidence_receipt", {"receipt_id": "r1"}, event_id="receipt-1",
        context=_context(receipts=[{"receipt_id": "r1", "path": "evidence.json"}]),
    )
    assert ledger.replay() == [event]


def test_legacy_replay_requires_explicit_compatibility(tmp_path):
    path = tmp_path / "events.jsonl"
    compatibility = _legacy(path)
    compatibility.append("claimed", {"task": "T1"}, event_id="legacy-1")
    raw = json.loads(path.read_text(encoding="utf-8"))
    # Simulate a pre-context v1 row written before the compatibility marker
    # existed, then recompute its hash exactly as an old writer would.
    raw.pop("compatibility")
    from simplicio_loop.ops_ledger import _digest
    raw["hash"] = _digest({key: value for key, value in raw.items() if key != "hash"})
    path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")
    try:
        EventLedger(path).replay()
    except LedgerError as exc:
        assert "compatibility" in str(exc)
    else:
        raise AssertionError("legacy row replayed without explicit compatibility")
    assert len(_legacy(path).replay()) == 1
