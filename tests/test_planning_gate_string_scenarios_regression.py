"""Regression: validate_plan must NOT crash on string scenarios/rules.

Discovered while running cron wi-569: a hand-written contract used
``scenarios: ["dedup"]`` (strings) but validate_plan did
``s.get("id")`` -> AttributeError on a str. The validator must
tolerate both forms and return a clean error list, never raise.
"""
from __future__ import annotations

from simplicio_loop.plan_contract import validate_plan


def test_string_scenarios_do_not_crash_validator():
    contract_tasks = [
        {"id": "T1", "scenarios": ["dedup", "reprocess"], "rules": ["idempotent_key"]},
    ]
    plan = {
        "schema": "simplicio.plan/v1",
        "mapper_pack_hash": "mp1",
        "context_pack_hash": "mp1",
        "repo_state": {"head": "h1", "tree_hash": "t1"},
        "freshness": {"verified": True, "current_state": {"head": "h1", "tree_hash": "t1"}},
        "steps": [
            {"candidate_targets": ["a.py"], "to_create": ["a.py"], "rule_ids": ["idempotent_key"],
             "steps": [{"scenario_id": "dedup", "plan": {"read_paths": ["a.py"], "change_paths": ["a.py"], "test_commands": ["true"]}}]},
        ],
    }
    # Must not raise; string scenarios tolerated as ids.
    result = validate_plan(plan, contract_tasks, ".", current_state={"head": "h1", "tree_hash": "t1"})
    assert isinstance(result, dict)
    # "dedup" planned, "reprocess" unplanned -> clean error, not a crash
    assert any("reprocess" in e for e in result["errors"]), result["errors"]
