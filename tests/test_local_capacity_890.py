from types import SimpleNamespace

from simplicio_loop import local_capacity
from simplicio_loop import runner


def test_probe_reports_measured_signals_and_reserves_workers(monkeypatch, tmp_path):
    monkeypatch.setattr(local_capacity.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(local_capacity, "_memory_available", lambda: 4 << 30)
    monkeypatch.setattr(local_capacity.shutil, "disk_usage", lambda _path: SimpleNamespace(free=10 << 30))

    sample = local_capacity.probe_local_capacity(tmp_path, requested_workers=7, now_ns=42)

    assert sample.safe_workers == 7
    assert sample.unavailable == ()
    assert set(sample.measured) == {"cpu_count", "disk_free_bytes", "memory_available_bytes"}
    assert sample.observed_at_ns == 42


def test_probe_fails_closed_when_required_signal_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(local_capacity.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(local_capacity, "_memory_available", lambda: None)
    monkeypatch.setattr(local_capacity.shutil, "disk_usage", lambda _path: SimpleNamespace(free=10 << 30))

    sample = local_capacity.probe_local_capacity(tmp_path, requested_workers=7, now_ns=43)

    assert sample.safe_workers == 1
    assert sample.memory_available_bytes is None
    assert "memory_available_bytes" in sample.unavailable
    assert sample.null_reasons["memory_available_bytes"] == "psutil_unavailable_or_probe_failed"


def test_native_prism_records_governor_observation(monkeypatch, tmp_path):
    monkeypatch.setattr(local_capacity.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(local_capacity, "_memory_available", lambda: 4 << 30)
    monkeypatch.setattr(local_capacity.shutil, "disk_usage", lambda _path: SimpleNamespace(free=10 << 30))

    _, _, receipt = runner._build_native_prism_scheduler(
        [{"repo": str(tmp_path), "run_id": "r1", "task_index": 1, "task_id": "t1"}],
        2,
    )

    assert receipt["safe_workers"] == 2
    assert receipt["budget_governor"]["events"][-1]["reason_code"] == "INITIAL_SAMPLE"
