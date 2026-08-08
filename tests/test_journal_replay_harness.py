from __future__ import annotations

import copy
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import journal_replay


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "journal_replay" / "scenarios.json"
EXPECTED = {
    "stall": "STALLED",
    "blocked": "BLOCKED",
    "stop": "STOP",
    "max_iterations": "MAX_ITERATIONS",
    "invalid_promise": "INVALID_PROMISE",
    "crash_recovered": "CRASH_RECOVERED",
    "lease_recovery": "LEASE_RECOVERY_REQUIRED",
    "converged": "CONVERGED",
}


def load_suite():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_replay_suite_classifies_every_scenario_and_is_byte_deterministic():
    suite = load_suite()
    first = journal_replay.replay_suite(suite, check_expected=True)
    second = journal_replay.replay_suite(suite, check_expected=True)

    assert first == second
    assert journal_replay.encode_receipt(first) == journal_replay.encode_receipt(second)
    assert {row["id"]: row["outcome"] for row in first["scenarios"]} == EXPECTED
    assert all(row["replay_stable"] for row in first["scenarios"])
    assert len(first["receipt_hash"]) == 64
    assert all(len(row["receipt_hash"]) == 64 for row in first["scenarios"])


def test_every_committed_repro_is_red_capable():
    suite = load_suite()
    for index, scenario in enumerate(suite["scenarios"]):
        wrong = copy.deepcopy(suite)
        wrong["scenarios"][index]["expected_outcome"] = "WRONG"
        with pytest.raises(journal_replay.ReplayError, match=scenario["id"]):
            journal_replay.replay_suite(wrong, check_expected=True)


def test_public_cli_replays_all_fixtures_offline():
    command = [sys.executable, str(ROOT / "scripts" / "journal_replay.py"), str(FIXTURE), "--check"]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    receipt = json.loads(completed.stdout)
    assert receipt["schema"] == "simplicio.journal-replay-receipt/v1"
    assert receipt["scenario_count"] == len(EXPECTED)
    assert receipt["outcomes"] == sorted(EXPECTED.values())

    help_result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "journal_replay.py"), "--help"],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert help_result.returncode == 0, help_result.stdout + help_result.stderr
    assert "--check" in help_result.stdout


def test_main_emits_the_same_canonical_receipt(capsys):
    assert journal_replay.main([str(FIXTURE), "--check"]) == 0
    first = capsys.readouterr()
    assert not first.err

    assert journal_replay.main([str(FIXTURE), "--check"]) == 0
    second = capsys.readouterr()
    assert first.out == second.out


def test_replay_harness_stays_fast_and_has_no_temporary_instrumentation():
    suite = load_suite()
    started = time.perf_counter()
    for _ in range(10):
        journal_replay.replay_suite(suite, check_expected=True)
    elapsed = time.perf_counter() - started

    assert elapsed < 10.0
    source = (ROOT / "scripts" / "journal_replay.py").read_text(encoding="utf-8")
    assert "[DEBUG-journal-replay-1139]" not in source
