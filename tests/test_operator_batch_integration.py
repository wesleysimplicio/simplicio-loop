import json
import threading
import time

from simplicio_loop import runner


def test_dispatch_operator_batch_refills_without_wave_barrier(monkeypatch, tmp_path):
    active = 0
    peak = 0
    calls = []
    lock = threading.Lock()

    def fake_execute(repo, run_id, task_index):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            calls.append(task_index)
        # The first item takes longer, which proves a freed slot is refilled before it ends.
        time.sleep(0.08 if task_index == 1 else 0.01)
        with lock:
            active -= 1
        return {
            "state": {
                "phase": "validating",
                "attempts": 1,
                "operator": {
                    "execution_state": "applied",
                    "receipt": str(tmp_path / f"receipt-{task_index}.json"),
                },
            }
        }

    monkeypatch.setattr(runner, "execute_operator", fake_execute)
    items = [
        {"repo": str(tmp_path / f"tree-{index}"), "run_id": "r1", "task_index": index}
        for index in range(1, 5)
    ]
    result = runner.dispatch_operator_batch(items, max_workers=2, retry_budget=0, journal_dir=str(tmp_path))

    assert result["max_workers"] == 2
    assert result["refill_count"] == 2
    assert peak == 2
    assert sorted(calls) == [1, 2, 3, 4]
    assert result["completed_task_indices"] == [1, 2, 3, 4]
    assert (tmp_path / "operator-batch.jsonl").exists()
    assert len((tmp_path / "operator-batch.json").read_text(encoding="utf-8")) > 0


def test_dispatch_operator_batch_serializes_shared_run_state(monkeypatch, tmp_path):
    calls = []

    def fake_execute(repo, run_id, task_index):
        calls.append(task_index)
        return {
            "state": {
                "phase": "validating",
                "attempts": 1,
                "operator": {"execution_state": "applied", "receipt": "receipt.json"},
            }
        }

    monkeypatch.setattr(runner, "execute_operator", fake_execute)
    result = runner.dispatch_operator_batch(
        [
            {"repo": str(tmp_path), "run_id": "shared", "task_index": 1},
            {"repo": str(tmp_path), "run_id": "shared", "task_index": 2},
        ],
        max_workers=2,
        retry_budget=0,
    )

    assert result["max_workers"] == 1
    assert result["serial_fallback_reason"] == "shared_run_state"
    assert result["completed_task_indices"] == [1, 2]
    assert calls == [1, 2]


def test_dispatch_operator_batch_resumes_successful_journal_entries(monkeypatch, tmp_path):
    calls = []

    def fake_execute(repo, run_id, task_index):
        calls.append(task_index)
        return {
            "state": {
                "phase": "validating",
                "attempts": 1,
                "operator": {"execution_state": "applied", "receipt": "receipt.json"},
            }
        }

    monkeypatch.setattr(runner, "execute_operator", fake_execute)
    journal = tmp_path / "operator-batch.jsonl"
    journal.write_text(
        json.dumps(
            {
                "repo": str((tmp_path / "tree-1").resolve()),
                "run_id": "r1",
                "task_index": 1,
                "status": "succeeded",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    result = runner.dispatch_operator_batch(
        [
            {"repo": str(tmp_path / "tree-1"), "run_id": "r1", "task_index": 1},
            {"repo": str(tmp_path / "tree-2"), "run_id": "r1", "task_index": 2},
        ],
        max_workers=2,
        retry_budget=0,
        journal_dir=str(tmp_path),
    )

    assert result["skipped_completed"] == 1
    assert calls == [2]
    assert result["completed_task_indices"] == [1, 2]


def test_fan_out_receipts_and_retries_are_worker_scoped(monkeypatch, tmp_path):
    """A retry must stay on its lane and every successful lane exposes both receipts."""
    calls = []
    attempts = {1: 0, 2: 0}

    def fake_execute(repo, run_id, task_index):
        attempts[task_index] += 1
        calls.append(task_index)
        if task_index == 1 and attempts[task_index] == 1:
            return {"state": {"phase": "blocked", "attempts": 1,
                               "operator": {"execution_state": "failed"}}}
        operator_receipt = tmp_path / f"operator-{task_index}.json"
        evidence_receipt = tmp_path / f"evidence-{task_index}.json"
        measured_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        operator_receipt.write_text(json.dumps({
            "schema": "simplicio.operator-receipt/v0",
            "execution_state": "applied",
            "target": "file.py",
            "measured_at": measured_at,
            "source": "live_cli",
            "tool": "simplicio-dev-cli",
            "repo_state_before": {"commit_sha": "deadbeef"},
        }), encoding="utf-8")
        evidence_receipt.write_text(json.dumps({
            "schema": "simplicio.evidence-receipt/v1",
            "run_id": "r1",
            "status": "VERIFIED",
            "measured_at": measured_at,
            "run": {"commit_sha": "deadbeef"},
            "operator": {"execution_state": "applied", "receipt_path": str(operator_receipt)},
        }), encoding="utf-8")
        return {
            "state": {
                "phase": "validating",
                "attempts": attempts[task_index],
                "operator": {
                    "execution_state": "applied",
                    "receipt": str(operator_receipt),
                },
                "evidence": {"receipt": str(evidence_receipt)},
            }
        }

    monkeypatch.setattr(runner, "execute_operator", fake_execute)
    result = runner.dispatch_operator_batch(
        [
            {"repo": str(tmp_path / "tree-1"), "run_id": "r1", "task_index": 1},
            {"repo": str(tmp_path / "tree-2"), "run_id": "r1", "task_index": 2},
        ],
        max_workers=2,
        retry_budget=1,
        journal_dir=str(tmp_path),
    )

    rows = {row["task_index"]: row for row in result["workers"]}
    assert attempts == {1: 2, 2: 1}
    assert calls.count(1) == 2
    assert calls.count(2) == 1
    assert rows[1]["retry_scope"] == "worker"
    assert rows[1]["attempt_count"] == 2
    assert [entry["status"] for entry in rows[1]["attempt_history"]] == ["failed", "succeeded"]
    assert rows[2]["attempt_count"] == 1
    assert rows[1]["operator_receipt"].endswith("operator-1.json")
    assert rows[1]["evidence_receipt"].endswith("evidence-1.json")
    assert result["receipt_contract"]["ready"] is True
    assert result["receipt_contract"]["missing_task_indices"] == []
    assert result["retry_contract"] == {
        "scope": "worker", "independent": True, "attempts_by_task": {"1": 2, "2": 1}
    }
