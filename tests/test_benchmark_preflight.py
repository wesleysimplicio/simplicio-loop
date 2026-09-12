from __future__ import annotations

import copy
import hashlib

import pytest

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


@pytest.mark.parametrize(
    ("check_name", "mutate"),
    [
        ("task_count", lambda d: d["task_count"].update(actual=11)),
        ("source_files", lambda d: d["source_files"].update(paths=["../outside.py"])),
        ("arm_config", lambda d: d["arm_config"].update(alternation=False)),
        ("capture_proxy", lambda d: d["capture_proxy"].update(artifact_digest="short")),
        ("executable", lambda d: d["executable"].update(sha256="short")),
        ("authenticated_mcp_call", lambda d: d["authenticated_mcp_call"].update(authenticated=False)),
        ("es_module_import", lambda d: d["es_module_import"].update(node_check_only=True)),
        ("treatment_routing", lambda d: d["treatment_routing"].update(observed="off")),
        ("off_routing", lambda d: d["off_routing"].update(receipt="")),
        ("pinned_model_response", lambda d: d["pinned_model_response"].update(model="provider/drifted")),
        ("captured_priced_generation", lambda d: d["captured_priced_generation"].update(priced=False)),
        ("actual_upstream", lambda d: d["actual_upstream"].update(observed="provider/drifted")),
        ("stdin_safe_loop", lambda d: d["stdin_safe_loop"].update(completed_tasks=11)),
        ("no_stale_tool_names", lambda d: d["no_stale_tool_names"].update(stale=["legacy_tool"])),
        ("tools_list_capability", lambda d: d["tools_list_capability"].update(available=["tools/list"])),
    ],
)
def test_each_preflight_check_fails_closed(check_name: str, mutate) -> None:
    descriptor = copy.deepcopy(_descriptor())
    mutate(descriptor)
    report = evaluate_preflight(descriptor)
    assert report["ready"] is False
    assert check_name in report["failed_or_unknown"]
    check = next(item for item in report["checks"] if item["name"] == check_name)
    assert check["status"] == "fail"


def _valid_trial() -> dict[str, object]:
    return {
        "completed_tasks": 12,
        "quality_passed": True,
        "baseline_completed_tasks": 12,
        "treatment_completed_tasks": 12,
        "priced": True,
        "generation_id": "generation-1",
        "provider_expected": "provider/pinned",
        "provider_observed": "provider/pinned",
        "route_expected": "treatment",
        "route_observed": "treatment",
        "timeout_output_bytes": 10,
        "timeout_output_limit": 100,
    }


@pytest.mark.parametrize(
    ("reason", "field", "value"),
    [
        ("incomplete_turns", "completed_tasks", 11),
        ("quality_failed", "quality_passed", False),
        ("unequal_completed_work", "treatment_completed_tasks", 11),
        ("unpriced_generation", "priced", False),
        ("provider_drift", "provider_observed", "provider/other"),
        ("unknown_or_mismatched_route", "route_observed", "off"),
        ("timeout_output_limit_exceeded", "timeout_output_limit", 5),
        ("provider_drift", "provider_observed", None),
        ("unknown_or_mismatched_route", "route_observed", None),
        ("unequal_completed_work", "baseline_completed_tasks", None),
        ("timeout_output_limit_exceeded", "timeout_output_limit", None),
    ],
)
def test_each_trial_invalidation_reason_is_deterministic(reason: str, field: str, value) -> None:
    trial = _valid_trial()
    trial[field] = value
    result = evaluate_trial(trial, expected_tasks=12)
    assert result["valid"] is False
    assert reason in result["reasons"]


def test_trial_with_complete_observed_evidence_is_valid() -> None:
    result = evaluate_trial(_valid_trial(), expected_tasks=12)
    assert result["status"] == "VALID"
    assert result["reasons"] == []
