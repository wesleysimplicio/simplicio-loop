import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from simplicio_loop.ops_ledger import EventLedger, LedgerError


def test_ledger_append_replay_is_hash_chained(tmp_path):
    ledger = EventLedger(tmp_path / "events.jsonl")
    first = ledger.append("claimed", {"task": "T1"}, event_id="e1")
    second = ledger.append("completed", {"task": "T1"}, event_id="e2")
    events = ledger.replay()
    assert [event["sequence"] for event in events] == [1, 2]
    assert second["prev_hash"] == first["hash"]


def test_duplicate_event_id_is_idempotent(tmp_path):
    ledger = EventLedger(tmp_path / "events.jsonl")
    first = ledger.append("heartbeat", {"worker": "w1"}, event_id="same")
    again = ledger.append("heartbeat", {"worker": "changed"}, event_id="same")
    assert again == first
    assert len(ledger.replay()) == 1


def test_tampering_fails_closed(tmp_path):
    path = tmp_path / "events.jsonl"
    ledger = EventLedger(path)
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


def test_concurrent_append_preserves_sequence(tmp_path):
    ledger = EventLedger(tmp_path / "events.jsonl")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: ledger.append("heartbeat", {"i": i}, event_id="e%d" % i), range(32)))
    events = ledger.replay()
    assert len(events) == 32
    assert [event["sequence"] for event in events] == list(range(1, 33))
