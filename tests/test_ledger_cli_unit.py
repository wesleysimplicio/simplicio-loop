import json

from simplicio_loop.cli import main
from simplicio_loop.ops_ledger import EventLedger, HANDSHAKE_SCHEMA


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


def _handshake(**overrides):
    value = {
        "schema": HANDSHAKE_SCHEMA,
        "executor_id": "executor-1",
        "executor_version": "3.24.0",
        "protocol": "operator/v1",
        "concurrency_budget": 6,
        "context": _context(),
    }
    value.update(overrides)
    return value


def test_ledger_cli_strict_replay_requires_handshake(tmp_path, capsys):
    path = tmp_path / "events.jsonl"
    EventLedger(path).append("claimed", {"task": "T1"}, event_id="e1",
                           context=_context())

    assert main(["ledger", "replay", "--path", str(path)]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert "handshake" in output["error"]["message"]


def test_ledger_cli_strict_replay_is_canonical_and_hash_bound(tmp_path, capsys):
    path = tmp_path / "events.jsonl"
    EventLedger(path).append("claimed", {"task": "T1"}, event_id="e1",
                           context=_context())
    handshake = json.dumps(_handshake(), ensure_ascii=False)
    args = ["ledger", "validate", "--path", str(path), "--handshake-json", handshake]

    assert main(args) == 0
    first = capsys.readouterr().out
    assert main(args) == 0
    second = capsys.readouterr().out

    assert first == second
    result = json.loads(first)
    assert result["ok"] is True
    assert result["command"] == "ledger.validate"
    assert result["event_count"] == 1
    assert result["handshake"]["concurrency_budget"] == 6
    assert result["required_context"] == [
        "run_id", "wave_id", "lane", "owner", "session", "reason_code"
    ]


def test_ledger_cli_legacy_requires_explicit_compatibility(tmp_path, capsys):
    path = tmp_path / "legacy.jsonl"
    ledger = EventLedger(path, compatibility=True)
    ledger.append("claimed", {"task": "T1"}, event_id="legacy-1")

    assert main(["ledger", "replay", "--path", str(path)]) == 2
    strict = json.loads(capsys.readouterr().out)
    assert strict["ok"] is False

    assert main(["ledger", "replay", "--path", str(path), "--compatibility"]) == 0
    compatible = json.loads(capsys.readouterr().out)
    assert compatible["ok"] is True
    assert compatible["compatibility"] is True
    assert compatible["handshake"] is None
    assert compatible["event_count"] == 1
