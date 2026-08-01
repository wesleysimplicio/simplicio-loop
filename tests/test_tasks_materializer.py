import json
from pathlib import Path

import pytest

from simplicio_loop.tasks_materializer import ContractMaterializationError, LoopRunContractMaterializer

def intake():
    return {"run_identity": {"run_id": "batch-1"}, "items": {"7": {"state": "planned", "title": "Do work", "acceptance_criteria": [{"statement": "the check passes"}]}, "8": {"state": "remote_closed", "title": "Skip"}}}

def test_materializes_canonical_run_and_authorized_worktree_item(tmp_path):
    calls = []
    def arm(repo, task_path, delivery, max_iterations):
        calls.append((repo, Path(task_path).read_text(encoding="utf-8"), delivery, max_iterations))
        run_dir = tmp_path / ".simplicio" / "loop-runs" / "run-7"
        run_dir.mkdir(parents=True)
        (run_dir / "plan.json").write_text(json.dumps({"steps": [{"candidate_targets": ["src/a.py"]}]}), encoding="utf-8")
        return {"manifest": {"run_id": "run-7"}, "state": {"phase": "awaiting_decision"}, "run_dir": str(run_dir)}
    rows = LoopRunContractMaterializer(str(tmp_path), arm=arm)(intake())
    assert len(rows) == 1
    assert rows[0]["task_id"] == "issue-7"
    assert rows[0]["isolation"] == "worktree"
    assert rows[0]["task_spec"]["files_affected"] == ["src/a.py"]
    assert "the check passes" in calls[0][1]

def test_materialization_fails_closed_on_blocked_preflight(tmp_path):
    def arm(*args):
        return {"state": {"phase": "blocked", "blockers": ["mapper stale"]}}
    with pytest.raises(ContractMaterializationError, match="mapper stale"):
        LoopRunContractMaterializer(str(tmp_path), arm=arm)(intake())
