import json

from simplicio_loop.inference_benchmark import SyntheticTask, run_benchmark, run_trial


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
