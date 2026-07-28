import sys
import threading

from simplicio_loop.validation_execution import (
    REQUIRED_CONTEXT_HASHES,
    TaskResult,
    ValidationCache,
    ValidationExecutor,
    ValidationTask,
    explain_execution,
)


def context(**changes):
    value = {key: key + "-v1" for key in REQUIRED_CONTEXT_HASHES}
    value.update(changes)
    return value


def command(source):
    return (sys.executable, "-c", source)


def test_cache_reuses_only_success_with_complete_identical_context(tmp_path):
    cache = ValidationCache(tmp_path / "cache.json")
    task = ValidationTask("unit", command("pass"))
    assert cache.put(task, context(), TaskResult("unit", "FAILED", 1, 1, "COMMAND_FAILED")) is False
    assert cache.get(task, context()) is None
    assert cache.put(task, context(), TaskResult("unit", "PASSED", 0, 1, "COMMAND_PASSED"))
    assert cache.get(task, context()).cached is True
    assert cache.get(task, context(config_hash="changed")) is None
    incomplete = context()
    incomplete.pop("lockfile_hash")
    assert cache.get(task, incomplete) is None


def test_bounded_parallel_serializes_conflicts_and_exclusive_tasks(tmp_path):
    executor = ValidationExecutor(max_workers=2, cache=ValidationCache(tmp_path / "cache.json"))
    tasks = [
        ValidationTask("a", command("import time; time.sleep(.04)"), resources=("db",)),
        ValidationTask("b", command("import time; time.sleep(.04)"), resources=("db",)),
        ValidationTask("c", command("pass"), independent=False),
    ]
    receipt = executor.execute(tasks, context=context(), final_gate_required=False)
    assert receipt["promotable"] is True
    assert receipt["max_observed_parallelism"] == 1
    assert [item["name"] for item in receipt["results"]] == ["a", "b", "c"]


def test_independent_tasks_are_bounded_parallel():
    tasks = [
        ValidationTask(str(index), command("import time; time.sleep(.04)"))
        for index in range(4)
    ]
    receipt = ValidationExecutor(max_workers=2).execute(
        tasks, context=context(), final_gate_required=False,
    )
    assert receipt["max_observed_parallelism"] == 2


def test_failure_timeout_and_cancellation_never_become_success():
    active = [
        ValidationTask("fail", command("raise SystemExit(3)")),
        ValidationTask("timeout", command("import time; time.sleep(1)"), timeout_seconds=.01),
    ]
    active_receipt = ValidationExecutor(max_workers=2).execute(
        active, context=context(), final_gate_required=False,
    )
    assert active_receipt["promotable"] is False
    assert {item["status"] for item in active_receipt["results"]} == {"FAILED", "TIMED_OUT"}

    cancelled = threading.Event()
    cancelled.set()
    tasks = [ValidationTask("cancel", command("pass"))]
    receipt = ValidationExecutor(max_workers=3).execute(
        tasks, context=context(), final_gate_required=False, cancel_event=cancelled,
    )
    assert receipt["promotable"] is False
    assert {item["status"] for item in receipt["results"]} == {"CANCELLED"}


def test_final_gate_is_fail_closed_and_must_execute_uncached(tmp_path):
    cache = ValidationCache(tmp_path / "cache.json")
    ordinary = ValidationTask("unit", command("pass"))
    executor = ValidationExecutor(cache=cache)
    missing = executor.execute([ordinary], context=context(), final_gate_required=True)
    assert missing["promotable"] is False
    assert "FINAL_GATE_MISSING" in missing["reason_codes"]
    gate = ValidationTask("full", command("pass"), tier="full", final_gate=True)
    passed = executor.execute([ordinary, gate], context=context(), final_gate_required=True)
    assert passed["promotable"] is True
    cached = executor.execute([ordinary, gate], context=context(), final_gate_required=True)
    assert cached["promotable"] is False
    assert "FINAL_GATE_FAILED_OR_CACHED" in cached["reason_codes"]


def test_receipt_and_explain_are_deterministic():
    task = ValidationTask("unit", command("pass"))
    first = ValidationExecutor().execute([task], context=context(), final_gate_required=False)
    second = ValidationExecutor().execute([task], context=context(), final_gate_required=False)
    # Duration is observational; normalized decisions remain byte-stable.
    for receipt in (first, second):
        receipt["results"][0]["duration_ms"] = 0
        receipt.pop("receipt_hash")
    assert explain_execution(first) == explain_execution(second)


def test_task_executes_in_pinned_worktree(tmp_path):
    (tmp_path / "marker.py").write_text("value = 1\n", encoding="utf-8")
    task = ValidationTask(
        "cwd", command("from pathlib import Path; assert Path('marker.py').is_file()"),
        cwd=str(tmp_path),
    )
    receipt = ValidationExecutor().execute([task], context=context(), final_gate_required=False)
    assert receipt["promotable"] is True
