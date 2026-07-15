import copy

import pytest

from simplicio_loop.model_registry import ModelCapabilityRegistry
from simplicio_loop.model_router import ModelRouterError, route


def _entry(**overrides):
    base = {
        "runtime": "local-devcli",
        "provider": "openai-compatible",
        "model_id": "local/q4",
        "aliases": [],
        "capabilities": ["coding", "patch", "tests"],
        "context_window": 8192,
        "os": [],
        "arch": [],
        "probe": {"kind": "stub", "target": ""},
    }
    base.update(overrides)
    return base


def _basic_entries():
    return [
        _entry(runtime="claude", provider="anthropic", model_id="claude-sonnet-5",
               capabilities=["coding", "patch", "tests", "review"], context_window=200000),
        _entry(runtime="codex", provider="openai", model_id="gpt-5.4",
               capabilities=["coding", "patch", "tests"], context_window=128000),
        _entry(runtime="local-devcli", provider="openai-compatible", model_id="local/q4",
               capabilities=["coding", "patch"], context_window=8192),
    ]


def test_route_is_deterministic_across_repeated_calls_and_registry_reorder():
    entries = _basic_entries()
    reg = ModelCapabilityRegistry(entries)
    reg_reordered = ModelCapabilityRegistry(list(reversed(entries)))
    requirements = {
        "role": "executor",
        "required_capabilities": ["coding", "patch"],
        "preferred_capabilities": ["tests", "review"],
    }
    receipt1 = route(requirements, reg)
    receipt2 = route(requirements, reg)
    receipt_reordered = route(requirements, reg_reordered)

    def strip_timestamp(receipt):
        clone = copy.deepcopy(receipt)
        clone.pop("timestamp", None)
        return clone

    assert strip_timestamp(receipt1) == strip_timestamp(receipt2)
    assert strip_timestamp(receipt1) == strip_timestamp(receipt_reordered)
    assert receipt1["selected"]["model_id"] == "claude-sonnet-5"
    assert receipt1["registry_hash"] == reg.registry_hash


def test_route_rejects_unknown_role():
    reg = ModelCapabilityRegistry(_basic_entries())
    with pytest.raises(ModelRouterError, match="role"):
        route({"role": "wizard"}, reg)


def test_independent_review_rejects_executors_route():
    reg = ModelCapabilityRegistry(_basic_entries())
    executor_requirements = {"role": "executor", "required_capabilities": ["coding", "patch"]}
    executor_receipt = route(executor_requirements, reg)
    executor_route = executor_receipt["selected"]
    assert executor_route is not None

    reviewer_requirements = {
        "role": "reviewer",
        "required_capabilities": ["coding", "patch"],
        "independent_review": True,
    }
    reviewer_receipt = route(reviewer_requirements, reg, executor_route=executor_route)
    assert reviewer_receipt["selected"] is not None
    assert reviewer_receipt["selected"]["model_id"] != executor_route["model_id"]
    rejected = {c["model_id"]: c for c in reviewer_receipt["candidates"] if c["status"] == "rejected"}
    assert rejected[executor_route["model_id"]]["reason_code"] == "policy_denied"


def test_independent_review_blocks_when_only_candidate_matches_executor():
    single_entry = [_entry(runtime="codex", provider="openai", model_id="gpt-5.4",
                            capabilities=["coding", "patch"], context_window=128000)]
    reg = ModelCapabilityRegistry(single_entry)
    executor_route = {"runtime": "codex", "provider": "openai", "model_id": "gpt-5.4"}
    reviewer_requirements = {
        "role": "reviewer",
        "required_capabilities": ["coding", "patch"],
        "independent_review": True,
    }
    receipt = route(reviewer_requirements, reg, executor_route=executor_route)
    assert receipt["blocked"] is True
    assert receipt["selected"] is None
    assert receipt["block_reason"]
    rejected = [c for c in receipt["candidates"] if c["status"] == "rejected"]
    assert rejected and rejected[0]["reason_code"] == "policy_denied"


def test_route_blocked_with_diagnostics_when_no_candidate_qualifies():
    entries = _basic_entries()
    reg = ModelCapabilityRegistry(entries)
    requirements = {
        "role": "executor",
        "required_capabilities": ["vision"],  # no entry declares this capability
    }
    receipt = route(requirements, reg)
    assert receipt["blocked"] is True
    assert receipt["selected"] is None
    assert receipt["block_reason"] == "no candidate satisfies mandatory requirements"
    assert len(receipt["candidates"]) == len(entries)
    assert all(c["status"] == "rejected" for c in receipt["candidates"])
    assert all(c["reason_code"] == "missing_capability" for c in receipt["candidates"])


def test_scoring_prefers_more_preferred_capabilities_and_receipt_has_all_fields():
    reg = ModelCapabilityRegistry(_basic_entries())
    requirements = {
        "role": "planner",
        "required_capabilities": ["coding"],
        "preferred_capabilities": ["tests", "review"],
    }
    receipt = route(requirements, reg)
    assert receipt["schema"] == "simplicio.routing-decision-receipt/v1"
    assert receipt["policy_version"]
    assert receipt["registry_hash"]
    assert receipt["timestamp"]
    assert receipt["selected"]["model_id"] == "claude-sonnet-5"
    selected_candidate = next(c for c in receipt["candidates"] if c["status"] == "selected")
    assert selected_candidate["score"] == 2
