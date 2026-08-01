import hashlib
import json

import pytest
from types import SimpleNamespace

from simplicio_loop.tasks_stage_pipeline import CommandPipelineCoordinator

class FakeCoordinator:
    instances = []
    pr_url = "https://github.com/acme/widgets/pull/1"
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.cancelled = []
        self.__class__.instances.append(self)
    def run_all(self):
        merge = {"schema": "simplicio.tasks-merge-receipt/v1", "merged": True, "pr_url": self.pr_url, "merge_commit_sha": "a" * 40}
        merge["receipt_sha"] = hashlib.sha256(json.dumps(merge, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        delivery = {"pr_url": self.pr_url, "pr_repo": "acme/widgets", "pr_head": "feature/1", "source_issue": "1", "checks": [{"name": "test", "conclusion": "SUCCESS"}], "operation": "merge", "merge_receipt": merge}
        instance = SimpleNamespace(receipt={"schema": "receipt"}, output=delivery)
        return {"delivery": SimpleNamespace(status="passed", instance=instance)}
    def terminal_reached(self):
        return True
    def status_report(self):
        return {"terminal_reached": True}
    def cancel_all(self, *, reason):
        self.cancelled.append(reason)
        return ["delivery"]

def test_pipeline_collects_pr_and_verification_receipts(tmp_path):
    pipeline = CommandPipelineCoordinator(["python", "agent.py"], str(tmp_path), coordinator_factory=FakeCoordinator)
    result = pipeline({"workers": [{"run_id": "run-1", "task_id": "issue-1", "expected_pr_repo": "acme/widgets", "branch": "feature/1"}]})
    assert result["passed"] is True
    assert result["evidence"][0]["pr"] == "https://github.com/acme/widgets/pull/1"
    assert result["evidence"][0]["verification"] == "passed"
    assert FakeCoordinator.instances[-1].kwargs["journal"] is not None


def test_pipeline_binds_worker_worktree_context(tmp_path):
    worktree = tmp_path / "allocated"
    worktree.mkdir()
    pipeline = CommandPipelineCoordinator(["python"], str(tmp_path), coordinator_factory=FakeCoordinator)
    pipeline({"workers": [{"run_id": "run-1", "task_id": "issue-1", "worktree_path": str(worktree), "branch": "feature/1", "head_sha": "abc123"}]})
    adapter = FakeCoordinator.instances[-1].kwargs["adapters"][0]
    assert adapter.cwd == worktree.resolve()
    assert adapter.extra_env == {"SIMPLICIO_TASK_WORKTREE": str(worktree), "SIMPLICIO_TASK_BRANCH": "feature/1", "SIMPLICIO_TASK_HEAD": "abc123"}

def test_pipeline_redacts_nested_secrets_from_evidence(tmp_path):
    previous = FakeCoordinator.pr_url
    class SecretCoordinator(FakeCoordinator):
        def run_all(self):
            instance = SimpleNamespace(receipt={"token": "secret", "nested": {"password": "hidden"}}, output={"pr_url": previous})
            return {"delivery": SimpleNamespace(status="passed", instance=instance)}
    result = CommandPipelineCoordinator(["python"], str(tmp_path), coordinator_factory=SecretCoordinator)({"workers": [{"task_id": "issue-1"}]})
    receipt = result["evidence"][0]["receipts"][0]
    assert receipt == {"token": "[REDACTED]", "nested": {"password": "[REDACTED]"}}
def test_pipeline_fails_closed_without_pr_evidence(tmp_path):
    previous = FakeCoordinator.pr_url
    FakeCoordinator.pr_url = ""
    try:
        result = CommandPipelineCoordinator(["python"], str(tmp_path), coordinator_factory=FakeCoordinator)({"workers": [{"task_id": "issue-1"}]})
    finally:
        FakeCoordinator.pr_url = previous
    assert result["passed"] is False
    assert result["evidence"][0]["pr"] is None


def test_pipeline_fails_closed_on_repo_head_source_or_check_mismatch(tmp_path):
    class InvalidDelivery(FakeCoordinator):
        def run_all(self):
            delivery = {"pr_url": self.pr_url, "pr_repo": "wrong/repo", "pr_head": "wrong", "source_issue": "2", "checks": [{"conclusion": "FAILURE"}]}
            instance = SimpleNamespace(receipt={}, output=delivery)
            return {"delivery": SimpleNamespace(status="passed", instance=instance)}
    worker = {"task_id": "issue-1", "expected_pr_repo": "acme/widgets", "branch": "feature/1"}
    result = CommandPipelineCoordinator(["python"], str(tmp_path), coordinator_factory=InvalidDelivery)({"workers": [worker]})
    assert result["passed"] is False
    assert result["evidence"][0]["delivery_errors"] == ["checks_not_successful", "merge_receipt_invalid", "pr_head_mismatch", "pr_repo_mismatch", "pr_url_mismatch", "source_issue_mismatch"]


def test_pipeline_rejects_non_github_pr_malformed_checks_and_missing_merge(tmp_path):
    class ForgedDelivery(FakeCoordinator):
        def run_all(self):
            delivery = {"pr_url": "file:///not-a-pr", "pr_repo": "acme/widgets", "pr_head": "feature/1", "source_issue": "1", "checks": [{"conclusion": "SUCCESS"}, "garbage"]}
            return {"delivery": SimpleNamespace(status="passed", instance=SimpleNamespace(receipt={}, output=delivery))}
    worker = {"task_id": "issue-1", "expected_pr_repo": "acme/widgets", "branch": "feature/1"}
    result = CommandPipelineCoordinator(["python"], str(tmp_path), coordinator_factory=ForgedDelivery)({"workers": [worker]})
    assert result["passed"] is False
    assert result["evidence"][0]["delivery_errors"] == ["checks_not_successful", "merge_receipt_invalid", "pr_url_mismatch"]

def test_cancel_propagates_to_active_coordinator(tmp_path):
    pipeline = CommandPipelineCoordinator(["python"], str(tmp_path), coordinator_factory=FakeCoordinator)
    active = FakeCoordinator()
    pipeline.active.append(active)
    assert pipeline.cancel_all(reason="stop") == ["delivery"]
    assert active.cancelled == ["stop"]


def test_cancel_persists_across_restart_and_finally_clears_active(tmp_path):
    pipeline = CommandPipelineCoordinator(["python"], str(tmp_path), coordinator_factory=FakeCoordinator)
    pipeline.cancel_all(reason="operator_stop")
    restarted = CommandPipelineCoordinator(["python"], str(tmp_path), coordinator_factory=FakeCoordinator)
    result = restarted({"workers": [{"task_id": "issue-1"}]})
    assert (result["cancelled"], result["reason"], restarted.active) == (True, "operator_stop", [])

    class FailingCoordinator(FakeCoordinator):
        def run_all(self):
            raise RuntimeError("boom")
    clean = CommandPipelineCoordinator(["python"], str(tmp_path / "clean"), coordinator_factory=FailingCoordinator)
    with pytest.raises(RuntimeError, match="boom"):
        clean({"workers": [{"task_id": "issue-1"}]})
    assert clean.active == []
