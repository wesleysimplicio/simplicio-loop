"""Acceptance tests for #153's worktree/conflict/merge-queue primitives."""
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
from worktree_queue import TaskSpec, WorktreeQueue  # noqa: E402


def _git(cwd, *args):
    return subprocess.run(["git"] + list(args), cwd=str(cwd), check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()


def _repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "simplicio-test")
    (root / "README").write_text("base\n", encoding="utf-8")
    _git(root, "add", "README")
    _git(root, "commit", "-qm", "base")
    return root


def _queue(tmp_path, repo, run="run1"):
    return WorktreeQueue(str(repo), str(tmp_path / "queue.json"), run_id=run)


def test_independent_tasks_get_distinct_worktrees_without_coordinator_checkout(tmp_path):
    repo = _repo(tmp_path)
    coordinator_head = _git(repo, "rev-parse", "HEAD")
    q = _queue(tmp_path, repo)
    a, b = TaskSpec("A", files_affected=["src/a.py"]), TaskSpec("B", files_affected=["src/b.py"])
    q.register_tasks([a, b])
    aa, bb = q.allocate(a), q.allocate(b)
    assert aa.path != str(repo) and bb.path != str(repo) and aa.path != bb.path
    assert aa.branch != bb.branch
    assert _git(repo, "rev-parse", "HEAD") == coordinator_head
    assert _git(aa.path, "rev-parse", "HEAD") == coordinator_head
    assert _git(bb.path, "rev-parse", "HEAD") == coordinator_head


def test_conflict_lanes_include_contracts_and_are_persisted(tmp_path):
    repo = _repo(tmp_path)
    q = _queue(tmp_path, repo)
    a = TaskSpec("A", files_affected=["src/shared.py"])
    b = TaskSpec("B", public_contracts=["api.v1"])
    c = TaskSpec("C", public_contracts=["api.v1"])
    q.register_tasks([a, b, c])
    graph = q.conflict_graph([a, b, c])
    assert graph["B"] == ["C"]
    state = q.state()
    assert state["lanes"]["B"] == state["lanes"]["C"]
    assert state["lanes"]["A"] != state["lanes"]["B"]


def test_restart_reattaches_by_persisted_run_and_task_id(tmp_path):
    repo = _repo(tmp_path)
    q1 = _queue(tmp_path, repo)
    original = q1.allocate(TaskSpec("A", files_affected=["a.py"]))
    # No run_id: the persisted state supplies the original namespace.
    q2 = WorktreeQueue(str(repo), str(tmp_path / "queue.json"))
    attached = q2.allocate(TaskSpec("A", files_affected=["a.py"]))
    assert attached.reattached
    assert attached.path == original.path
    assert attached.branch == original.branch


def test_merge_queue_reports_base_drift_with_repair_handoff(tmp_path):
    repo = _repo(tmp_path)
    q = _queue(tmp_path, repo)
    q.allocate(TaskSpec("A", files_affected=["a.py"]))
    (repo / "drift").write_text("drift\n", encoding="utf-8")
    _git(repo, "add", "drift")
    _git(repo, "commit", "-qm", "move base")
    candidate = q.enqueue_merge("A")
    assert candidate["status"] == "repair-required"
    handoff = json.loads(open(candidate["repair_handoff"], encoding="utf-8").read())
    assert handoff["frozen_base_sha"] != handoff["current_base_sha"]
    assert handoff["branch"] == candidate["branch"]


def test_composed_verification_receipt_is_hash_linked_and_not_delivery(tmp_path):
    repo = _repo(tmp_path)
    q = _queue(tmp_path, repo)
    q.allocate(TaskSpec("A"))
    q.enqueue_merge("A")
    command = [sys.executable, "-c", "print('composed green')"]
    receipt = q.run_composed_verification("A", [command], suite="suite+flow+impact")
    assert receipt["passed"] is True
    assert receipt["previous_receipt_sha"] == ""
    assert receipt["worktree_path"].endswith(os.path.join("run1", "A"))
    assert receipt["lane"].startswith("lane-")
    assert receipt["tree_sha"]
    assert q.state()["tasks"]["A"]["status"] == "accepted"
    assert q.state()["merge_queue"][0]["status"] == "accepted"
    assert q.state()["tasks"]["A"]["status"] != "delivered"


def test_shared_checkout_requires_policy_and_one_owned_lock(tmp_path):
    repo = _repo(tmp_path)
    q = _queue(tmp_path, repo)
    with pytest.raises(ValueError):
        q.allocate(TaskSpec("A"), isolation="shared")
    q.allocate(TaskSpec("A"), isolation="shared", shared_policy=True)
    with pytest.raises(RuntimeError):
        q.allocate(TaskSpec("B"), isolation="shared", shared_policy=True)
    report = q.teardown("A")
    assert report.removed is True
    second = q.allocate(TaskSpec("B"), isolation="shared", shared_policy=True)
    assert second.mode == "shared"


def test_teardown_does_not_remove_unowned_path_and_reports_failure(tmp_path):
    repo = _repo(tmp_path)
    q = _queue(tmp_path, repo)
    q.allocate(TaskSpec("A"))
    state = q.state()
    state["tasks"]["A"]["path"] = str(tmp_path / "unrelated")
    with open(tmp_path / "queue.json", "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    report = q.teardown("A")
    assert report.removed is False
    assert "path-not-owned" in report.failures
