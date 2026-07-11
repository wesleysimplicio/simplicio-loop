from simplicio_loop.plan_contract import PLAN_SCHEMA, validate_plan


def _task():
    return [{
        "scenarios": [{"id": "S1"}],
        "rules": [{"id": "RN01"}],
    }]


def _plan(repo):
    return {
        "schema": PLAN_SCHEMA,
        "task_contract_hash": "contract-1",
        "mapper_pack_hash": "pack-1",
        "repo_state": {"head": "head-1", "tree_hash": "tree-1"},
        "freshness": {"verified": True, "current_state": {"head": "head-1", "tree_hash": "tree-1"}},
        "steps": [{
            "candidate_targets": ["src/app.py"],
            "to_create": [],
            "rule_ids": ["RN01"],
            "steps": [{"scenario_id": "S1"}],
        }],
    }


def test_plan_v1_covers_task_and_authorized_existing_target(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("pass\n", encoding="utf-8")
    result = validate_plan(_plan(tmp_path), _task(), tmp_path,
                           contract_hash="contract-1")
    assert result["valid"] is True
    assert result["errors"] == []


def test_plan_rejects_stale_mapper_state_and_unplanned_rule(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("pass\n", encoding="utf-8")
    plan = _plan(tmp_path)
    plan["steps"][0]["rule_ids"] = []
    result = validate_plan(plan, _task(), tmp_path,
                           contract_hash="contract-1",
                           current_state={"head": "head-2", "tree_hash": "tree-2"})
    assert result["valid"] is False
    assert "plan_repo_state_stale" in result["errors"]
    assert "task[1] rule_unplanned:RN01" in result["errors"]


def test_plan_rejects_escape_and_requires_to_create_for_new_target(tmp_path):
    plan = _plan(tmp_path)
    plan["steps"][0]["candidate_targets"] = ["../escape.py", "src/new.py"]
    result = validate_plan(plan, _task(), tmp_path,
                           contract_hash="contract-1")
    assert result["valid"] is False
    assert "task[1] target_outside_repo:../escape.py" in result["errors"]
    assert "task[1] target_missing_without_to_create:src/new.py" in result["errors"]
