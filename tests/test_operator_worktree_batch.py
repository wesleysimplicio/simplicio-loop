"""WorktreeQueue bridge tests that do not invoke Git subprocesses."""

from pathlib import Path
from types import SimpleNamespace

from simplicio_loop import runner


def test_auto_fan_out_requires_independent_plan_targets(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()

    class Queue:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.registered = []

        def register_tasks(self, specs):
            self.registered = list(specs)

        @staticmethod
        def conflict_graph(specs):
            return {spec.id: [] for spec in specs}

    import scripts.worktree_queue as worktree_queue
    monkeypatch.setattr(worktree_queue, "WorktreeQueue", Queue)
    contract = {"tasks": [{"identity": {"feature": "A"}}, {"identity": {"feature": "B"}}]}
    plan = {"steps": [{"candidate_targets": ["src/a.py"]}, {"candidate_targets": ["src/b.py"]}]}

    queue, contexts, reason = runner._auto_worktree_dispatch(
        str(tmp_path), "run-1", contract, plan, [1, 2]
    )

    assert isinstance(queue, Queue)
    assert reason == ""
    assert set(contexts) == {1, 2}
    assert {spec.files_affected[0] for spec in queue.registered} == {"src/a.py", "src/b.py"}


def test_auto_fan_out_falls_back_for_overlapping_targets(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    contract = {"tasks": [{"identity": {"feature": "A"}}, {"identity": {"feature": "B"}}]}
    plan = {"steps": [{"candidate_targets": ["src/shared.py"]}, {"candidate_targets": ["src/shared.py"]}]}

    queue, contexts, reason = runner._auto_worktree_dispatch(
        str(tmp_path), "run-1", contract, plan, [1, 2]
    )

    assert queue is None
    assert contexts == {}
    assert reason == "overlapping_task_impacts"


def test_auto_fan_out_can_be_disabled(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("SIMPLICIO_LOOP_AUTO_FAN_OUT", "0")
    queue, contexts, reason = runner._auto_worktree_dispatch(
        str(tmp_path), "run-1", {"tasks": [{}, {}]}, {"steps": [{}, {}]}, [1, 2]
    )
    assert queue is None
    assert contexts == {}
    assert reason == "auto_fan_out_disabled"


class FakeQueue:
    def __init__(self, root: Path):
        self.root = root
        self.registered = []
        self.allocations = []
        self.contexts = {}
        self.cleaned = []
        self.shared_active = False

    def register_tasks(self, specs):
        self.registered = list(specs)

    def allocate(self, spec, isolation="worktree", shared_policy=False):
        assert isolation in ("worktree", "shared")
        if isolation == "shared":
            if self.shared_active:
                raise RuntimeError("shared checkout already owned")
            self.shared_active = True
        path = self.root if isolation == "shared" else self.root / spec.id
        if isolation == "worktree":
            path.mkdir(parents=True, exist_ok=True)
        allocation = SimpleNamespace(
            task_id=spec.id,
            run_id="queue-run",
            mode=isolation,
            path=str(path),
            branch="simplicio/queue/" + spec.id,
            base_sha="base",
            head_sha="head",
            tree_sha="tree",
            lane="lane-" + ("shared" if isolation == "shared" else spec.id),
            reattached=False,
            lock_receipt="lock.json" if isolation == "shared" else None,
        )
        self.allocations.append(allocation)
        return allocation

    def record_context(self, task_id, context):
        self.contexts[task_id] = dict(context)

    def teardown(self, task_id):
        self.cleaned.append(task_id)
        self.shared_active = False


def _success(repo, run_id, task_index):
    return {
        "run_dir": str(Path(repo) / ".orchestrator" / "runs" / run_id),
        "state": {
            "phase": "validating",
            "attempts": 1,
            "operator": {
                "execution_state": "applied",
                "receipt": str(Path(repo) / "receipt.json"),
            },
        },
    }


def test_dispatch_allocates_and_persists_isolated_context_without_git(monkeypatch, tmp_path):
    queue = FakeQueue(tmp_path / "workers")
    calls = []

    def fake_execute(repo, run_id, task_index):
        calls.append((repo, run_id, task_index))
        return _success(repo, run_id, task_index)

    monkeypatch.setattr(runner, "execute_operator", fake_execute)
    result = runner.dispatch_operator_batch(
        [
            {"repo": str(tmp_path), "run_id": "run-1", "task_index": 1, "task_id": "A",
             "task_spec": {"id": "A", "files_affected": ["a.py"]}},
            {"repo": str(tmp_path), "run_id": "run-1", "task_index": 2, "task_id": "B",
             "task_spec": {"id": "B", "files_affected": ["b.py"]}},
        ],
        max_workers=2,
        retry_budget=0,
        worktree_queue=queue,
    )

    assert result["max_workers"] == 2
    assert result["serial_fallback_reason"] == ""
    assert result["completed_task_indices"] == [1, 2]
    assert {item[0] for item in calls} == {str((tmp_path / "workers" / "A").resolve()),
                                             str((tmp_path / "workers" / "B").resolve())}
    assert set(queue.contexts) == {"A", "B"}
    assert all(row["worktree_context"]["context_path"] for row in result["workers"])


def test_dispatch_serializes_explicit_shared_queue_context(monkeypatch, tmp_path):
    queue = FakeQueue(tmp_path / "shared")
    calls = []

    def fake_execute(repo, run_id, task_index):
        calls.append(task_index)
        return _success(repo, run_id, task_index)

    monkeypatch.setattr(runner, "execute_operator", fake_execute)
    result = runner.dispatch_operator_batch(
        [
            {"repo": str(tmp_path), "run_id": "run-2", "task_index": 1, "task_id": "A",
             "isolation": "shared", "task_spec": {"id": "A"}},
            {"repo": str(tmp_path), "run_id": "run-2", "task_index": 2, "task_id": "B",
             "isolation": "shared", "task_spec": {"id": "B"}},
        ],
        max_workers=2,
        retry_budget=0,
        worktree_queue=queue,
    )

    assert result["max_workers"] == 1
    assert result["serial_fallback_reason"] == "shared_run_state"
    assert calls == [1, 2]
    assert queue.cleaned == ["A", "B"]
    assert all(row["worktree_context"]["mode"] == "shared" for row in result["workers"])


def test_dispatch_queue_context_error_fails_closed(monkeypatch, tmp_path):
    class BrokenQueue(FakeQueue):
        def record_context(self, task_id, context):
            raise RuntimeError("context store offline")

    calls = []
    monkeypatch.setattr(runner, "execute_operator", lambda *args, **kwargs: calls.append(args))
    result = runner.dispatch_operator_batch(
        [{"repo": str(tmp_path), "run_id": "run-3", "task_index": 1, "task_id": "A"}],
        max_workers=1,
        retry_budget=0,
        worktree_queue=BrokenQueue(tmp_path / "workers"),
    )

    assert not calls
    assert result["failed_task_indices"] == [1]
    assert result["workers"][0]["reason_code"] == "worktree_context_unpersisted"
