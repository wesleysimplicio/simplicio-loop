import json
import pytest

from simplicio_loop.inference_benchmark import (
    LocalModelSmokeError,
    SyntheticTask,
    run_benchmark,
    run_local_model_smoke,
    run_trial,
)


def test_scenarios_are_deterministic_and_cache_does_not_equal_quality():
    tasks = (SyntheticTask("a", 2, "same", 1.0), SyntheticTask("b", 2, "same", 0.5))
    l0 = run_trial("L0", tasks, seed=4)
    l3 = run_trial("L3", tasks, seed=4)
    assert l0["raw_hash"] != l3["raw_hash"]
    assert l3["metrics"]["deduplicated"] == 1
    assert l3["metrics"]["verified_deliveries"] == 1
    json.dumps(l3)


def test_unobserved_resource_metrics_are_null_with_reasons():
    metrics = run_trial("L1")["metrics"]
    assert metrics["rss_mb"] is None and metrics["rss_reason"] == "not_observed"
    assert metrics["vram_mb"] is None and metrics["vram_reason"] == "not_observed"


def test_interrupted_resume_does_not_duplicate_samples():
    interrupted = run_trial("L4", seed=8, stop_after=2)
    resumed = run_trial("L4", seed=8, existing_samples=interrupted["raw_samples"])
    assert interrupted["interrupted"] is True
    assert resumed["interrupted"] is False
    assert len(resumed["raw_samples"]) == 5
    assert len({sample["task_id"] for sample in resumed["raw_samples"]}) == 5


def test_benchmark_manifest_labels_scenarios_and_repeats():
    report = run_benchmark(scenarios=("L0", "L4"), repeats=2, seed=2, commit="abc", model="m", backend="b")
    assert report["scenarios"] == ["L0", "L4"]
    assert len(report["trials"]) == 4
    assert report["trials"][0]["manifest"]["commit"] == "abc"


def test_local_model_smoke_records_measured_success_without_shell(tmp_path, monkeypatch):
    model = tmp_path / "qwen.gguf"
    binary = tmp_path / "llama-completion.exe"
    model.write_bytes(b"GGUF")
    binary.write_bytes(b"binary")

    class Result:
        returncode = 0
        stdout = "OK"
        stderr = ""

    calls = []
    monkeypatch.setattr(
        "simplicio_loop.inference_benchmark.subprocess.run",
        lambda command, **kwargs: (calls.append((command, kwargs)) or Result()),
    )
    receipt = run_local_model_smoke(str(model), llama_binary=str(binary))
    assert receipt["status"] == "MEASURED"
    assert receipt["inference_ran"] is True
    assert receipt["model_size_bytes"] == 4
    assert calls and calls[0][0][0] == str(binary.resolve())
    assert calls[0][1].get("shell") is not True


def test_local_model_smoke_rejects_missing_or_non_gguf(tmp_path):
    with pytest.raises(LocalModelSmokeError):
        run_local_model_smoke(str(tmp_path / "missing.gguf"), llama_binary=str(tmp_path / "llama"))
    model = tmp_path / "model.bin"
    binary = tmp_path / "llama"
    model.write_bytes(b"x")
    binary.write_bytes(b"x")
    with pytest.raises(LocalModelSmokeError):
        run_local_model_smoke(str(model), llama_binary=str(binary))
