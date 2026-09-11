import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location("diagnostic_summary", Path(__file__).resolve().parents[1] / "bench/summarize_queue_diagnostics.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_nearest_rank_small_sample_is_not_interpolated():
    assert module.quantile([3, 1, 2], .5) == 2
    assert module.quantile([3, 1, 2], .99) == 3
    assert module.quantile([], .99) is None


def test_provider_deduplication_missing_fields_and_reasoning_subset(tmp_path):
    probe = {"status": "response_received", "task": "GH-102.md", "provider_wall_ns": 1_000_000_000,
             "response": {"id": "unique-real-receipt-id", "usage": {"prompt_tokens": 10,
                 "completion_tokens": 5, "completion_tokens_details": {"reasoning_tokens": 3}, "cost": .1}}}
    (tmp_path / "openrouter-worker-GH-102.json").write_text(json.dumps(probe))
    duplicate = tmp_path / "diagnostic-GH102"
    duplicate.mkdir()
    (duplicate / "provider.json").write_text(json.dumps(probe))
    (duplicate / "summary.json").write_text(json.dumps({"task": "GH-102", "verified": False, "stages": []}))
    result = module.summarize(tmp_path)
    assert len(result["provider_calls"]) == 1
    assert result["provider_totals"]["output_tokens"]["observed_sum"] == 5
    assert result["provider_totals"]["reasoning_tokens"]["observed_sum"] == 3
    assert result["provider_totals"]["cached_tokens"]["observed_sum"] is None
    assert not result["provider_totals"]["cached_tokens"]["complete"]
    assert result["winner"] is None
    assert result["eligible_loop_cli_arm"] is False
    assert result["provider_totals"]["cost_usd"]["observed_sum"] == .1
