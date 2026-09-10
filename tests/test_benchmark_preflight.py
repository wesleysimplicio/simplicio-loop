from __future__ import annotations

import hashlib

from scripts.benchmark_preflight import CHECK_NAMES, evaluate_preflight, evaluate_trial


def _descriptor() -> dict[str, object]:
    return {
        "expected_tasks": 12,
        "task_count": {"expected": 12, "actual": 12},
        "source_files": {"count": 2, "paths": ["src/app.py", "tests/test_app.py"]},
        "arm_config": {
            "alternation": True,
            "baseline": {"name": "off", "task_count": 12},
            "treatment": {"name": "fast", "task_count": 12},
        },
        "capture_proxy": {
            "kind": "jsonl",
            "path": "captures/session.jsonl",
            "records": 12,
            "artifact_digest": hashlib.sha256(b"capture").hexdigest(),
        },
        "executable": {
            "present": True,
            "path": "/opt/bin/simplicio-loop",
            "version": "3.38.30",
            "sha256": "a" * 64,
        },
        "authenticated_mcp_call": {
            "authenticated": True,
            "status": "success",
            "method": "tools/call",
            "receipt": "receipt-1",
        },
        "es_module_import": {"imported": True, "node_check_only": False, "module": "runner.mjs"},
        "treatment_routing": {"expected": "fast", "observed": "fast", "receipt": "route-1"},
        "off_routing": {"expected": "off", "observed": "off", "receipt": "route-2"},
        "pinned_model": "provider/model-pinned",
        "pinned_model_response": {
            "completed": True,
            "model": "provider/model-pinned",
            "response_id": "generation-1",
        },
        "captured_priced_generation": {
            "generation_id": "generation-1",
            "priced": True,
            "cost_usd": 0.01,
            "input_tokens": 100,
            "output_tokens": 30,
        },
        "actual_upstream": {"expected": "provider/upstream", "observed": "provider/upstream", "receipt": "upstream-1"},
        "stdin_safe_loop": {
            "stdin": "DEVNULL",
            "completed_tasks": 12,
            "hung": False,
            "output_limited": True,
        },
        "no_stale_tool_names": {"observed": ["tools/list", "tools/call"], "stale": []},
        "tools_list_capability": {"required": ["tools/list", "tools/call"], "available": ["tools/list", "tools/call"]},
    }


def test_preflight_has_fifteen_explicit_observed_checks() -> None:
    report = evaluate_preflight(_descriptor())
    assert report["ready"] is True
    assert [item["name"] for item in report["checks"]] == list(CHECK_NAMES)
    assert all(item["status"] == "pass" for item in report["checks"])


def test_preflight_missing_observation_blocks_instead_of_inferencing() -> None:
    descriptor = _descriptor()
    descriptor.pop("captured_priced_generation")
    descriptor["es_module_import"] = {"imported": False, "node_check_only": True, "module": "runner.mjs"}
    report = evaluate_preflight(descriptor)
    assert report["ready"] is False
    assert "captured_priced_generation" in report["failed_or_unknown"]
    assert "es_module_import" in report["failed_or_unknown"]


def test_trial_gate_rejects_known_benchmark_failure_modes() -> None:
    trial = {
        "completed_tasks": 11,
        "quality_passed": False,
        "baseline_completed_tasks": 12,
        "treatment_completed_tasks": 11,
        "priced": False,
        "generation_id": "",
        "provider_expected": "pinned",
        "provider_observed": "drifted",
        "route_expected": "fast",
        "route_observed": "unknown",
        "timeout_output_bytes": 20,
        "timeout_output_limit": 10,
        "retries": [{"attempt": 2, "reason": "timeout_output_limit_exceeded"}],
    }
    result = evaluate_trial(trial, expected_tasks=12)
    assert result["valid"] is False
    assert result["reasons"] == [
        "incomplete_turns",
        "quality_failed",
        "unequal_completed_work",
        "unpriced_generation",
        "provider_drift",
        "unknown_or_mismatched_route",
        "timeout_output_limit_exceeded",
    ]
    assert result["retries"]
