"""CLI contract tests for the composed drain receipt surface."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
CLI = [sys.executable, "-m", "simplicio_loop.cli"]


def _task():
    return {
        "id": "T1",
        "state": "done",
        "delivery_satisfied": True,
        "evidence": {
            "watcher_status": "MEASURED",
            "watcher_match": True,
            "oracle_verdict": "COMPLETE",
            "fresh": True,
            "checked_at": "2026-07-10T20:00:00Z",
            "contract_hash": "contract-T1",
            "receipt_id": "receipt-T1",
            "challenge": "challenge-1",
        },
    }


def _snapshot():
    return {
        "tasks": [_task()],
        "polls": ["empty:1", "empty:1"],
        "active_leases": 0,
        "challenge": "challenge-1",
    }


def _run(*args):
    env = dict(os.environ)
    result = subprocess.run(
        CLI + list(args),
        cwd=str(REPO),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stderr == "", result.stderr
    assert result.stdout.strip(), "drain CLI must emit JSON on stdout"
    return result, json.loads(result.stdout)


def test_drain_cli_evaluate_emits_measured_json(tmp_path):
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(_snapshot()), encoding="utf-8")

    result, payload = _run("drain", "evaluate", "--snapshot", str(snapshot))

    assert result.returncode == 0
    assert payload["schema"] == "simplicio.drain-receipt/v1"
    assert payload["verdict"] == "DRAINED"
    assert payload["ready"] is True
    assert payload["tag"] == "MEASURED"


def test_drain_cli_persist_then_load_round_trip(tmp_path):
    snapshot = tmp_path / "snapshot.json"
    receipt = tmp_path / "receipt.json"
    snapshot.write_text(json.dumps(_snapshot()), encoding="utf-8")

    persist_result, persisted = _run(
        "drain", "persist", "--snapshot", str(snapshot), "--receipt", str(receipt)
    )
    load_result, loaded = _run("drain", "load", "--receipt", str(receipt))

    assert persist_result.returncode == 0
    assert load_result.returncode == 0
    assert loaded == persisted
    assert receipt.exists()


def test_drain_cli_invalid_snapshot_is_fail_closed_json(tmp_path):
    snapshot = tmp_path / "broken.json"
    snapshot.write_text("[not an object]", encoding="utf-8")

    result, payload = _run("drain", "evaluate", "--snapshot", str(snapshot))

    assert result.returncode != 0
    assert payload["verdict"] == "CONTINUE"
    assert payload["ready"] is False
    assert payload["tag"] == "UNVERIFIED"
    assert payload["reason_code"] == "snapshot_invalid"


def test_drain_cli_missing_receipt_is_fail_closed_json(tmp_path):
    result, payload = _run("drain", "load", "--receipt", str(tmp_path / "missing.json"))

    assert result.returncode != 0
    assert payload["verdict"] == "CONTINUE"
    assert payload["ready"] is False
    assert payload["reason_code"] == "receipt_missing"


def test_drain_cli_semantically_invalid_receipt_is_fail_closed_json(tmp_path):
    receipt = tmp_path / "invalid.json"
    receipt.write_text(
        json.dumps({
            "schema": "simplicio.drain-receipt/v1",
            "verdict": "DRAINED",
            "ready": True,
            # Missing tag and evidence fields must not be exposed as success.
        }),
        encoding="utf-8",
    )

    result, payload = _run("drain", "load", "--receipt", str(receipt))

    assert result.returncode != 0
    assert payload["verdict"] == "CONTINUE"
    assert payload["ready"] is False
    assert payload["reason_code"] == "receipt_invalid"


def test_drain_cli_unknown_or_missing_action_is_fail_closed_json():
    for args in (("unknown",), tuple()):
        result, payload = _run("drain", *args)
        assert result.returncode != 0
        assert payload["verdict"] == "CONTINUE"
        assert payload["ready"] is False
        assert payload["reason_code"] == "action_invalid"
