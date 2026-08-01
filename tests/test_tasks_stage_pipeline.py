from types import SimpleNamespace

from simplicio_loop.tasks_stage_pipeline import CommandPipelineCoordinator

class FakeCoordinator:
    instances = []
    pr_url = "https://example.test/pr/1"
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.cancelled = []
        self.__class__.instances.append(self)
    def run_all(self):
        instance = SimpleNamespace(receipt={"schema": "receipt"}, output={"pr_url": self.pr_url})
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
    result = pipeline({"workers": [{"run_id": "run-1", "task_id": "issue-1"}]})
    assert result["passed"] is True
    assert result["evidence"][0]["pr"] == "https://example.test/pr/1"
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
def test_pipeline_fails_closed_without_pr_evidence(tmp_path):
    previous = FakeCoordinator.pr_url
    FakeCoordinator.pr_url = ""
    try:
        result = CommandPipelineCoordinator(["python"], str(tmp_path), coordinator_factory=FakeCoordinator)({"workers": [{"task_id": "issue-1"}]})
    finally:
        FakeCoordinator.pr_url = previous
    assert result["passed"] is False
    assert result["evidence"][0]["pr"] is None

def test_cancel_propagates_to_active_coordinator(tmp_path):
    pipeline = CommandPipelineCoordinator(["python"], str(tmp_path), coordinator_factory=FakeCoordinator)
    active = FakeCoordinator()
    pipeline.active.append(active)
    assert pipeline.cancel_all(reason="stop") == ["delivery"]
    assert active.cancelled == ["stop"]
