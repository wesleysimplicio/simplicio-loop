from __future__ import annotations

import pytest

from simplicio_loop.canonical_plan import (
    CanonicalPlanError,
    canonical_plan_metadata,
    load_canonical_plan,
)
from simplicio_loop.runtime_effect_adapter import EffectRequest, RuntimeEffectAdapter


def _payload() -> dict:
    return {
        "schema": "simplicio.plan-dag/v1",
        "plan_id": "plan-298-loop",
        "goal_id": "goal-298",
        "context_snapshot_id": "snapshot-298",
        "revision": "1",
        "nodes": [{
            "node_id": "edit",
            "capability": "edit.apply",
            "inputs": [], "outputs": ["source"], "depends_on": [],
            "read_set": ["src"], "write_set": ["src"], "risk": "medium",
            "uncertainty": "low", "estimated_cost": 1.0,
            "reason_codes": ["issue-298"], "acceptance_criteria_refs": ["AC1"],
            "requires_gate": True, "checkpoint_required": False,
            "rollback_strategy": "revert",
        }],
        "producer_id": "simplicio-dev-cli",
        "consumer_id": "simplicio-loop",
        "budget": 2.0,
        "trace_id": "trace-298",
        "context_handle": "",
    }


def test_loop_admits_dev_cli_plan_and_exports_causal_metadata():
    plan = load_canonical_plan(_payload())
    metadata = canonical_plan_metadata(plan)
    assert metadata["plan_id"] == "plan-298-loop"
    assert metadata["plan_digest"] == plan.digest


def test_loop_rejects_unknown_major_and_digest_mismatch():
    with pytest.raises(CanonicalPlanError, match="unsupported canonical plan schema"):
        load_canonical_plan({**_payload(), "schema": "simplicio.plan-dag/v2"})
    with pytest.raises(CanonicalPlanError, match="digest"):
        load_canonical_plan(_payload(), expected_digest="0" * 64)


def test_runtime_effect_transaction_carries_canonical_plan_identity():
    plan = load_canonical_plan(_payload())
    request = EffectRequest("/tmp", "run:298:effect", ("src",), "lease-1", 1, canonical_plan=plan)
    receipt = RuntimeEffectAdapter(profile="standalone").edit(request, {"path": "src"})
    assert receipt["canonical_plan"]["plan_digest"] == plan.digest
    assert receipt["transaction"]["canonical_plan"]["goal_id"] == "goal-298"
