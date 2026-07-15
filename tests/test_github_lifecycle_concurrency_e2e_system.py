"""#285 remaining gap: a REAL two-lease/two-device concurrency E2E.

Two independent OS **processes** (not threads in one interpreter -- a real, separate Python
process each, exactly like `tests/test_work_item_claims_chaos.py`'s crash test) race to claim
and own the SAME GitHub issue's lifecycle comment at the same time, against a shared on-disk
SQLite queue (`simplicio_loop.remote_queue.SQLiteRemoteQueue`) and a shared on-disk fake "GitHub"
comment store (a JSON file, guarded by a directory-based lock so concurrent readers/writers never
corrupt it). This proves:

  1. Only ONE of the two processes ever wins the lease for the work item.
  2. The loser gets a clean, typed rejection (`QueueConflict`) -- it NEVER calls
     `GitHubSourceAdapter.claim`/`publish_lifecycle_state`, so it can never post or corrupt a
     comment.
  3. The winner's write is confirmed end-to-end (`GitHubSourceAdapter.claim` ->
     `publish_lifecycle_state` -> publish-then-re-query, `verified: True`).
  4. The shared fake-GitHub comment store ends up with EXACTLY ONE canonical lifecycle comment,
     authored by the winner only -- no duplicate, no partial/corrupted body.

No real `gh`/network call is made -- both worker processes are given a fake `runner` callable
(same fake-transport style as `tests/test_github_lifecycle_unit.py`), but the *lease contention*
and *process boundary* are both real: two real OS processes, a real shared SQLite file, real
`BEGIN IMMEDIATE` transactional locking.
"""
import json
import os
import subprocess
import sys
import time

from simplicio_loop.remote_queue import SQLiteRemoteQueue

IDENTITY_A = {
    "agent_id": "codex@device-a", "runtime": "codex", "device_id": "device-a",
    "session_id": "session-a", "capabilities": ["claim", "heartbeat", "fencing", "receipts", "events"],
}
IDENTITY_B = {
    "agent_id": "claude@device-b", "runtime": "claude", "device_id": "device-b",
    "session_id": "session-b", "capabilities": ["claim", "heartbeat", "fencing", "receipts", "events"],
}

_WORKER_SCRIPT = r"""
import json, os, sys, time

sys.path.insert(0, %(repo_root)r)
sys.path.insert(0, %(scripts_dir)r)

from pr_evidence import publish_comment
from simplicio_loop.remote_queue import QueueConflict, SQLiteRemoteQueue
from simplicio_loop.source_adapter import GitHubSourceAdapter
from simplicio_loop.work_item_claims import AttemptCoordinator

COMMENT_STORE = %(comment_store)r
LOCK_DIR = COMMENT_STORE + ".lock"


def _lock_acquire(deadline):
    while time.time() < deadline:
        try:
            os.mkdir(LOCK_DIR)
            return
        except FileExistsError:
            time.sleep(0.01)
    raise TimeoutError("could not acquire fake-github comment-store lock")


def _lock_release():
    try:
        os.rmdir(LOCK_DIR)
    except OSError:
        pass


def _load_store():
    if not os.path.exists(COMMENT_STORE):
        return {"comments": [], "next_id": 1, "state": "open"}
    with open(COMMENT_STORE, encoding="utf-8") as fh:
        return json.load(fh)


def _save_store(store):
    tmp = COMMENT_STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(store, fh)
    os.replace(tmp, COMMENT_STORE)


class _FakeCompleted:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def fake_runner(args, **kwargs):
    assert args[0] == "gh"
    verb = args[1]
    _lock_acquire(time.time() + 10.0)
    try:
        store = _load_store()
        if verb == "issue" and args[2] == "close":
            store["state"] = "closed"
            _save_store(store)
            return _FakeCompleted(0, stdout="")
        if verb == "api":
            rest = args[2:]
            input_text = kwargs.get("input")
            if rest and rest[0] == "-X" and rest[1] == "PATCH":
                comment_id = int(rest[2].rsplit("/", 1)[-1])
                body = json.loads(input_text)["body"]
                for c in store["comments"]:
                    if c["id"] == comment_id:
                        c["body"] = body
                        _save_store(store)
                        return _FakeCompleted(0, stdout=json.dumps(c))
                return _FakeCompleted(1, stderr="404 comment not found")
            if rest and rest[0] == "-X" and rest[1] == "POST":
                body = json.loads(input_text)["body"]
                comment = {"id": store["next_id"], "body": body, "author": %(agent_id)r}
                store["next_id"] += 1
                store["comments"].append(comment)
                _save_store(store)
                return _FakeCompleted(0, stdout=json.dumps(comment))
            path = rest[0]
            if "/comments/" in path:
                comment_id = int(path.split("/comments/")[-1])
                for c in store["comments"]:
                    if c["id"] == comment_id:
                        return _FakeCompleted(0, stdout=json.dumps(c))
                return _FakeCompleted(1, stderr="404")
            if path.endswith("/comments") or "/comments?" in path:
                return _FakeCompleted(0, stdout=json.dumps(store["comments"]))
            if "/issues/" in path:
                return _FakeCompleted(0, stdout=json.dumps({
                    "number": 42, "title": "t", "body": "b", "state": store["state"],
                    "state_reason": None, "labels": [], "assignees": [], "milestone": None,
                    "user": {"login": "someone"}, "created_at": "2024-01-01T00:00:00Z",
                    "updated_at": "2024-01-01T00:00:00Z", "html_url": "https://example/42",
                }))
        return _FakeCompleted(1, stderr="unsupported: %%r" %% (args,))
    finally:
        _lock_release()


queue = SQLiteRemoteQueue(%(db_path)r)
coordinator = AttemptCoordinator(queue, run_id=%(run_id)r)

# Synchronize both workers to attempt the claim as close together as real OS scheduling allows.
deadline = time.time() + 15.0
while not os.path.exists(%(start_gate)r) and time.time() < deadline:
    time.sleep(0.005)

result = {"agent_id": %(agent_id)r}
try:
    attempt = coordinator.claim(work_item_id=%(work_item_id)r, identity=%(identity)s,
                                goal="fix the bug", ttl=%(ttl)r)
    result["claimed"] = True
    result["fencing_token"] = attempt.lease.fencing_token

    adapter = GitHubSourceAdapter("acme", "widgets", publish_comment_fn=publish_comment,
                                  runner=fake_runner, timeout=5,
                                  outbox_dir=%(outbox_dir)r)
    receipt = adapter.claim("42", run_id=%(run_id)r, attempt_id=attempt.attempt_id,
                            require_active=lambda: coordinator.assert_active(attempt))
    result["receipt"] = receipt
except QueueConflict as exc:
    result["claimed"] = False
    result["reason"] = "QueueConflict"
    result["detail"] = str(exc)

with open(%(result_path)r, "w", encoding="utf-8") as fh:
    json.dump(result, fh)
"""


def _run_worker(tmp_path, *, agent_id, identity, work_item_id, run_id, db_path, comment_store,
                outbox_dir, start_gate, ttl):
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    scripts_dir = os.path.join(repo_root, "scripts")
    result_path = str(tmp_path / ("result-%s.json" % agent_id.replace("@", "-").replace("/", "-")))
    script = _WORKER_SCRIPT % {
        "repo_root": repo_root, "scripts_dir": scripts_dir, "db_path": db_path,
        "run_id": run_id, "work_item_id": work_item_id, "identity": identity, "ttl": ttl,
        "start_gate": start_gate, "result_path": result_path, "comment_store": comment_store,
        "outbox_dir": outbox_dir, "agent_id": agent_id,
    }
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    return proc, result_path


def test_two_real_processes_race_for_the_same_issue_lease_only_one_wins_no_duplicate_comment(tmp_path):
    db_path = str(tmp_path / "queue.db")
    comment_store = str(tmp_path / "fake_github_comments.json")
    outbox_dir = str(tmp_path / "outbox")
    start_gate = str(tmp_path / "GO")
    work_item_id = "ISSUE-42"
    run_id = "run-concurrency-1"
    ttl = 30.0

    proc_a, result_a = _run_worker(tmp_path, agent_id=IDENTITY_A["agent_id"], identity=IDENTITY_A,
                                   work_item_id=work_item_id, run_id=run_id, db_path=db_path,
                                   comment_store=comment_store, outbox_dir=outbox_dir,
                                   start_gate=start_gate, ttl=ttl)
    proc_b, result_b = _run_worker(tmp_path, agent_id=IDENTITY_B["agent_id"], identity=IDENTITY_B,
                                   work_item_id=work_item_id, run_id=run_id, db_path=db_path,
                                   comment_store=comment_store, outbox_dir=outbox_dir,
                                   start_gate=start_gate, ttl=ttl)

    # Both processes are now polling for the start gate; enqueue the shared task, then drop the
    # gate file so they race as close to simultaneously as real OS process scheduling allows.
    queue = SQLiteRemoteQueue(db_path)
    queue.enqueue(work_item_id, {"goal": "fix the bug"})
    time.sleep(0.2)
    with open(start_gate, "w", encoding="utf-8") as fh:
        fh.write("go")

    out_a, err_a = proc_a.communicate(timeout=30)
    out_b, err_b = proc_b.communicate(timeout=30)
    assert proc_a.returncode == 0, "worker A crashed: stdout=%r stderr=%r" % (out_a, err_a)
    assert proc_b.returncode == 0, "worker B crashed: stdout=%r stderr=%r" % (out_b, err_b)

    with open(result_a, encoding="utf-8") as fh:
        result_a = json.load(fh)
    with open(result_b, encoding="utf-8") as fh:
        result_b = json.load(fh)

    results = [result_a, result_b]
    winners = [r for r in results if r["claimed"]]
    losers = [r for r in results if not r["claimed"]]

    # (1) exactly one process wins the lease
    assert len(winners) == 1, "expected exactly one winner, got %r" % (results,)
    assert len(losers) == 1

    # (2) the loser gets a clean, typed rejection and never touched the comment store
    assert losers[0]["reason"] == "QueueConflict"

    winner = winners[0]
    assert winner["receipt"]["verified"] is True

    # (3)/(4) the shared fake-GitHub store has exactly one comment, authored by the winner only
    with open(comment_store, encoding="utf-8") as fh:
        store = json.load(fh)
    assert len(store["comments"]) == 1, "expected exactly one canonical comment, got %r" % store["comments"]
    assert store["comments"][0]["author"] == winner["agent_id"]
    assert store["comments"][0]["id"] == winner["receipt"]["comment_id"]

    # cleanup: nothing external was touched (SQLite file + comment store are both under tmp_path,
    # cleaned up automatically by pytest's tmp_path fixture).
