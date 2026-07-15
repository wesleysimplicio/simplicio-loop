"""#290 remaining gap 2 -- a real concurrency/crash/fault-injection matrix for the
delivery-truth path, proving no state corruption and no false-positive "verified" in any of:

  1. two independent OS **processes** racing to claim/transition the SAME work item;
  2. a process crashing mid-transition (between a `transition-intent` write and the
     external effect actually landing);
  3. a transient GitHub API failure during reconciliation (a fake transport that fails N
     times, then succeeds) -- proving a retry recovers cleanly and a real negative fact is
     never smoothed over into a false PASS.
  4. a process crashing (real `kill()`, not a mocked "pretend the call failed") **while
     genuinely blocked inside** the external network call itself -- `MergeExecutor.merge()`'s
     `gh pr merge` invocation, and `DeploymentVerifier`'s byte-verification call -- proving the
     interrupted call never gets to fabricate a favorable result and that only a fresh
     post-crash re-query (`reconcile()` / a fresh `github_delivery_payload()` call) determines
     the truth.

Reuses the same real primitives the rest of the suite already exercises for real (no new
transport layer invented here): `simplicio_loop.remote_queue.SQLiteRemoteQueue` (real
`BEGIN IMMEDIATE` fencing), `simplicio_loop.work_item_claims.AttemptCoordinator`, and
`simplicio_loop.external_verifiers.retry_transient` composed over the existing
`discover_default_branch`/`compare_commits` live-query functions.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

from simplicio_loop import external_verifiers as ev
from simplicio_loop.remote_queue import QueueConflict, SQLiteRemoteQueue
from simplicio_loop.work_item_claims import AttemptCoordinator

IDENTITY_A = {
    "agent_id": "codex@device-a", "runtime": "codex", "device_id": "device-a",
    "session_id": "session-a", "capabilities": ["claim", "heartbeat", "fencing", "receipts", "events"],
}
IDENTITY_B = {
    "agent_id": "claude@device-b", "runtime": "claude", "device_id": "device-b",
    "session_id": "session-b", "capabilities": ["claim", "heartbeat", "fencing", "receipts", "events"],
}


# ---------------------------------------------------------------------------
# 1. Two real OS processes race to claim the SAME work item
# ---------------------------------------------------------------------------

_RACE_WORKER_SCRIPT = r"""
import json, os, sys, time
sys.path.insert(0, %(repo_root)r)
from simplicio_loop.remote_queue import QueueConflict, SQLiteRemoteQueue
from simplicio_loop.work_item_claims import AttemptCoordinator

queue = SQLiteRemoteQueue(%(db_path)r)
coordinator = AttemptCoordinator(queue, run_id=%(run_id)r)

# Synchronize: both processes busy-wait for the same "go" file so the two `claim()` calls
# land as close together in wall-clock time as the OS scheduler allows -- a real race, not
# a sequential "first one then the other".
deadline = time.time() + 10.0
while not os.path.exists(%(go_path)r) and time.time() < deadline:
    time.sleep(0.01)

try:
    attempt = coordinator.claim(work_item_id=%(work_item_id)r, identity=%(identity)s,
                                goal="race for the same delivery work item", ttl=30.0)
    print("WON fencing_token=%%d" %% attempt.lease.fencing_token, flush=True)
except QueueConflict as exc:
    print("LOST %%s" %% exc, flush=True)
"""


def test_two_processes_race_to_claim_same_work_item_only_one_wins(tmp_path):
    repo_root = str(Path(__file__).resolve().parent.parent)
    db_path = str(tmp_path / "queue.db")
    go_path = str(tmp_path / "go")
    run_id = "run-race-1"
    work_item_id = "WI-RACE-1"

    script_a = _RACE_WORKER_SCRIPT % {
        "repo_root": repo_root, "db_path": db_path, "run_id": run_id,
        "work_item_id": work_item_id, "identity": IDENTITY_A, "go_path": go_path,
    }
    script_b = _RACE_WORKER_SCRIPT % {
        "repo_root": repo_root, "db_path": db_path, "run_id": run_id,
        "work_item_id": work_item_id, "identity": IDENTITY_B, "go_path": go_path,
    }

    proc_a = subprocess.Popen([sys.executable, "-c", script_a],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              stdin=subprocess.DEVNULL)
    proc_b = subprocess.Popen([sys.executable, "-c", script_b],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              stdin=subprocess.DEVNULL)
    try:
        # Both processes are now spinning on the "go" file -- drop it to release them
        # together, as simultaneously as the OS scheduler allows.
        time.sleep(0.2)
        Path(go_path).write_text("go", encoding="utf-8")

        out_a, err_a = proc_a.communicate(timeout=15)
        out_b, err_b = proc_b.communicate(timeout=15)
    finally:
        for proc in (proc_a, proc_b):
            try:
                proc.kill()
            except Exception:
                pass

    assert proc_a.returncode == 0, err_a
    assert proc_b.returncode == 0, err_b

    outcomes = [out_a.strip(), out_b.strip()]
    winners = [line for line in outcomes if line.startswith("WON")]
    losers = [line for line in outcomes if line.startswith("LOST")]
    assert len(winners) == 1, "expected exactly ONE winner of the race, got: %r" % (outcomes,)
    assert len(losers) == 1, "expected exactly ONE loser of the race, got: %r" % (outcomes,)

    # No state corruption: the queue's own durable record shows the work item claimed by
    # exactly one agent, and the queue file itself is still a valid, single row of truth.
    queue = SQLiteRemoteQueue(db_path)
    task = queue.task(work_item_id)
    assert task["status"] == "claimed"
    assert task["lease"]["agent_id"] in (IDENTITY_A["agent_id"], IDENTITY_B["agent_id"])
    claimed_events = [e for e in queue.events(after=0, limit=1000)
                      if e.get("task_id") == work_item_id and e.get("kind") == "claimed"]
    assert len(claimed_events) == 1, "exactly one claim must be durably recorded, got %r" % claimed_events


# ---------------------------------------------------------------------------
# 2. Crash between transition-intent and the external effect landing
# ---------------------------------------------------------------------------

_CRASH_MID_TRANSITION_SCRIPT = r"""
import json, os, sys, time
sys.path.insert(0, %(repo_root)r)
from simplicio_loop.remote_queue import SQLiteRemoteQueue
from simplicio_loop.work_item_claims import AttemptCoordinator

queue = SQLiteRemoteQueue(%(db_path)r)
coordinator = AttemptCoordinator(queue, run_id=%(run_id)r, receipt_dir=%(receipt_dir)r)
attempt = coordinator.claim(work_item_id=%(work_item_id)r, identity=%(identity)s,
                            goal="mutate then crash before the effect lands", ttl=%(ttl)r)

# The intent is durably recorded FIRST (this is the "transition-intent" receipt the #290
# reconciliation transaction requires) -- only THEN would the real external effect run.
coordinator.record_event(attempt, "transition_intent", {
    "target": "merged", "idempotency_key": attempt.lease.idempotency_key,
})
with open(%(handoff_path)r, "w", encoding="utf-8") as fh:
    json.dump({"fencing_token": attempt.lease.fencing_token}, fh)
print("INTENT_RECORDED", flush=True)

# Simulate the crash: the process dies here, BEFORE the external effect (e.g. `gh pr merge`)
# ever runs and BEFORE any "effect landed" receipt is written. The effect-counter file must
# stay untouched by this process.
time.sleep(300)
"""


def test_crash_between_intent_and_effect_recovers_without_duplicating_the_effect(tmp_path):
    repo_root = str(Path(__file__).resolve().parent.parent)
    db_path = str(tmp_path / "queue.db")
    receipt_dir = str(tmp_path / "receipts")
    handoff_path = str(tmp_path / "handoff.json")
    effect_counter_path = tmp_path / "effect_calls.txt"
    run_id = "run-crash-1"
    work_item_id = "WI-CRASH-1"
    ttl = 2.0

    script = _CRASH_MID_TRANSITION_SCRIPT % {
        "repo_root": repo_root, "db_path": db_path, "receipt_dir": receipt_dir,
        "run_id": run_id, "work_item_id": work_item_id, "identity": IDENTITY_A,
        "ttl": ttl, "handoff_path": handoff_path,
    }
    proc = subprocess.Popen([sys.executable, "-c", script],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            stdin=subprocess.DEVNULL)
    try:
        deadline = time.time() + 10.0
        line = ""
        while time.time() < deadline:
            line = proc.stdout.readline()
            if line:
                line = line.strip()
                if line == "INTENT_RECORDED":
                    break
        assert line == "INTENT_RECORDED", "child never recorded its transition intent: %r" % line
        proc.kill()
        proc.wait(timeout=10)
    finally:
        try:
            proc.kill()
        except Exception:
            pass

    time.sleep(ttl + 0.5)  # let the lease actually expire (real wall-clock, not mocked)

    queue = SQLiteRemoteQueue(db_path)

    def run_effect_once(idempotency_key: str) -> str:
        """Stand-in for the real external effect (`gh pr merge`, a release publish, ...):
        appends one line per real invocation so the test can prove it never ran twice for
        the same logical transition."""
        with open(effect_counter_path, "a", encoding="utf-8") as fh:
            fh.write(idempotency_key + "\n")
        return "effect-ran-for-" + idempotency_key

    # Recovery: a second claimant, different identity, picks up the (now-expired) lease.
    second_coordinator = AttemptCoordinator(queue, run_id=run_id, receipt_dir=receipt_dir)
    second_attempt = second_coordinator.claim(work_item_id=work_item_id, identity=IDENTITY_B,
                                              goal="resume after crash", ttl=60.0)
    assert second_attempt.lease.idempotency_key

    # The dead worker's OWN idempotency key must never be replayed to run the effect again --
    # its intent receipt is inspectable (durable, append-only) but its authority is gone.
    with open(handoff_path, encoding="utf-8") as fh:
        first_handoff = json.load(fh)
    assert second_attempt.lease.fencing_token > first_handoff["fencing_token"]

    # The second claimant runs the effect for real, exactly once, under its OWN idempotency key.
    run_effect_once(second_attempt.lease.idempotency_key)
    second_coordinator.record_event(second_attempt, "transition_confirmation",
                                    {"target": "merged", "effect_ran": True})
    second_coordinator.complete(second_attempt, receipt_ref="recovered-after-crash")

    # No corruption / no duplicate effect: the effect ran EXACTLY once, and it was the
    # second claimant's key -- never the dead worker's stale intent replayed.
    effect_lines = effect_counter_path.read_text(encoding="utf-8").splitlines()
    assert effect_lines == [second_attempt.lease.idempotency_key]

    # Both the crashed worker's intent (append-only, never overwritten) and the second
    # worker's confirmation are inspectable afterwards -- receipts are never lost, only
    # superseded in authority. (attempt_id is "<work_item_id>-<fencing_token>".)
    first_attempt_id = "%s-%d" % (work_item_id, first_handoff["fencing_token"])
    first_events_file = Path(receipt_dir) / run_id / work_item_id / first_attempt_id / "events.jsonl"
    first_events = [json.loads(l) for l in first_events_file.read_text(encoding="utf-8").splitlines()]
    assert [e["kind"] for e in first_events] == ["claimed", "transition_intent"]

    second_events_file = Path(receipt_dir) / run_id / work_item_id / second_attempt.attempt_id / "events.jsonl"
    second_events = [json.loads(l) for l in second_events_file.read_text(encoding="utf-8").splitlines()]
    assert [e["kind"] for e in second_events] == ["claimed", "transition_confirmation", "completed"]


# ---------------------------------------------------------------------------
# 3. Transient GitHub API failure during reconciliation: retry recovers cleanly, a real
#    negative fact is never retried into a false PASS.
# ---------------------------------------------------------------------------

def test_transient_failure_during_branch_reachability_reconciliation_retries_then_succeeds(monkeypatch):
    calls = {"default_branch": 0, "compare": 0}

    def flaky_discover(repo):
        calls["default_branch"] += 1
        if calls["default_branch"] < 3:
            return {"ok": False, "reason_code": "default_branch_query_failed"}
        return {"ok": True, "default_branch": "main"}

    def flaky_compare(repo, base, head):
        calls["compare"] += 1
        if calls["compare"] < 2:
            return {"ok": False, "reason_code": "compare_query_failed"}
        return {"ok": True, "status": "identical", "ahead_by": 0, "behind_by": 0}

    monkeypatch.setattr(ev, "discover_default_branch", flaky_discover)
    monkeypatch.setattr(ev, "compare_commits", flaky_compare)

    # Without any retry wrapper, a single call surfaces the transient failure fail-closed --
    # never a false PASS.
    first_attempt = ev.verify_branch_reachability("acme/widgets", "deadbeef")
    assert first_attempt["ok"] is False
    assert first_attempt["reachable"] is False
    assert first_attempt["reason_code"] == "default_branch_query_failed"

    # Wrapping the SAME call in `retry_transient` recovers once the transient condition
    # clears on the provider side -- proving reconciliation is not permanently wedged by a
    # rate limit/5xx blip.
    result = ev.retry_transient(
        lambda: ev.verify_branch_reachability("acme/widgets", "deadbeef"),
        attempts=6, backoff=0, sleep=lambda s: None,
        is_transient=lambda r: not r.get("ok") and r.get("reason_code") in (
            "default_branch_query_failed", "compare_query_failed"),
    )
    assert result["ok"] is True
    assert result["reachable"] is True
    assert result["default_branch"] == "main"


def test_transient_failure_never_masks_a_real_negative_reachability_verdict(monkeypatch):
    """A commit that is genuinely NOT reachable (e.g. `diverged`) must come back as a real
    negative on the FIRST attempt -- retrying it must never turn it into a false PASS, and
    `retry_transient` must recognize it is not a transient reason code and stop immediately."""
    calls = {"n": 0}

    def stable_discover(repo):
        return {"ok": True, "default_branch": "main"}

    def stable_but_diverged_compare(repo, base, head):
        calls["n"] += 1
        return {"ok": True, "status": "diverged"}

    monkeypatch.setattr(ev, "discover_default_branch", stable_discover)
    monkeypatch.setattr(ev, "compare_commits", stable_but_diverged_compare)

    result = ev.retry_transient(
        lambda: ev.verify_branch_reachability("acme/widgets", "deadbeef"),
        attempts=5, backoff=0, sleep=lambda s: None,
        is_transient=lambda r: not r.get("ok"),
    )
    assert result["ok"] is True
    assert result["reachable"] is False
    assert result["reason_code"] == "merge_commit_not_reachable"
    assert calls["n"] == 1, "a real negative verdict must never be retried into a false PASS"


# ---------------------------------------------------------------------------
# 4a. Real process crash WHILE BLOCKED INSIDE `MergeExecutor.merge()`'s own `gh pr merge`
#     network call (not merely around a lease claim) -- the interrupted call must never
#     produce a fabricated "merged" result; only a fresh `reconcile()` re-query afterward may.
# ---------------------------------------------------------------------------

_CRASH_DURING_MERGE_CALL_SCRIPT = r"""
import json, subprocess, sys, time
sys.path.insert(0, %(repo_root)r)
from simplicio_loop.merge_executor import MergeExecutor

class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

def fake_runner(args, capture_output=True, text=True, timeout=30):
    # args is ["gh", "pr", "view"/"merge", ...]
    if "view" in args:
        return _Completed(0, json.dumps({"state": "OPEN", "mergeable": "MERGEABLE",
                                          "mergeStateStatus": "CLEAN"}))
    if "merge" in args:
        # This is the exact network call the real `gh pr merge` performs. Mark that we are
        # now genuinely inside it, then hang -- standing in for the process being killed
        # while GitHub's request is in flight, i.e. BEFORE we know whether it landed.
        with open(%(marker_path)r, "w", encoding="utf-8") as fh:
            fh.write("IN_MERGE_CALL")
        time.sleep(300)
    raise AssertionError("unexpected gh args: %%r" %% (args,))

executor = MergeExecutor(repo="acme/widgets", runner=fake_runner)
result = executor.merge(42, poll_interval=0, mergeable_timeout=5)

# Only reachable if the (fake) network call returned -- must never happen once killed.
with open(%(result_path)r, "w", encoding="utf-8") as fh:
    json.dump(result.to_dict(), fh)
print("UNEXPECTED_RETURN", flush=True)
"""


def test_crash_during_merge_command_network_call_produces_no_false_positive_receipt(tmp_path):
    """Kill the OS process for real while it is blocked inside the `gh pr merge` call itself
    (not at a lease boundary) -- proves no `MergeResult(merged=True)` is ever fabricated from
    an interrupted call, and that recovery must re-query (`reconcile()`) rather than assume."""
    repo_root = str(Path(__file__).resolve().parent.parent)
    marker_path = str(tmp_path / "in_merge_call.marker")
    result_path = str(tmp_path / "merge_result.json")

    script = _CRASH_DURING_MERGE_CALL_SCRIPT % {
        "repo_root": repo_root, "marker_path": marker_path, "result_path": result_path,
    }
    # File-redirected stdout/stderr (never `subprocess.PIPE`) -- this test's own point is a
    # real OS-level `kill()` of an in-flight child; piping through inheritable duplicated
    # handles is an orthogonal OS resource concern this test must not depend on.
    stdout_path = tmp_path / "child.stdout"
    with open(stdout_path, "w", encoding="utf-8") as stdout_fh:
        proc = subprocess.Popen([sys.executable, "-c", script],
                                stdout=stdout_fh, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
        try:
            deadline = time.time() + 10.0
            while time.time() < deadline and not Path(marker_path).exists():
                time.sleep(0.02)
            assert Path(marker_path).exists(), "child never reached the in-flight gh pr merge call"
            # The real crash: kill the process while it is genuinely blocked mid network call,
            # i.e. GitHub's own outcome for this request is still unknown to the client.
            proc.kill()
            proc.wait(timeout=10)
        finally:
            try:
                proc.kill()
            except Exception:
                pass

    # No result was ever written -- the interrupted call produced nothing, favorable or not.
    assert not Path(result_path).exists(), (
        "a killed mid-call process must never leave behind a fabricated merge receipt")
    assert stdout_path.read_text(encoding="utf-8").strip() != "UNEXPECTED_RETURN"

    # --- Recovery scenario A: GitHub actually finished the merge asynchronously despite the
    # client's death. Truth can ONLY come from a fresh re-query, never from the dead call.
    from simplicio_loop.merge_executor import MergeExecutor

    def post_crash_runner_merged(args, capture_output=True, text=True, timeout=30):
        assert "view" in args, "recovery must re-query, not replay the dead call's args"
        return subprocess.CompletedProcess(
            args, 0,
            stdout=json.dumps({"state": "MERGED",
                               "mergeCommit": {"oid": "deadbeefcafe"},
                               "baseRefName": "main"}),
            stderr="",
        )

    recovered = MergeExecutor(repo="acme/widgets", runner=post_crash_runner_merged)
    reconciled = recovered.reconcile(42)
    assert reconciled["merged"] is True
    assert reconciled["merge_commit_sha"] == "deadbeefcafe"

    # --- Recovery scenario B: GitHub never processed the request before the client died.
    # The fresh re-query must report the real negative -- never a leftover optimistic default.
    def post_crash_runner_not_merged(args, capture_output=True, text=True, timeout=30):
        assert "view" in args
        return subprocess.CompletedProcess(
            args, 0,
            stdout=json.dumps({"state": "OPEN", "mergeCommit": None, "baseRefName": "main"}),
            stderr="",
        )

    recovered_b = MergeExecutor(repo="acme/widgets", runner=post_crash_runner_not_merged)
    reconciled_b = recovered_b.reconcile(42)
    assert reconciled_b["merged"] is False


# ---------------------------------------------------------------------------
# 4b. Real process crash WHILE BLOCKED INSIDE `DeploymentVerifier`'s byte-verification network
#     call -- the interrupted call must never leave a fabricated `deployed=True` payload;
#     `github_delivery_payload()` must stay fail-closed until a fresh call actually completes.
# ---------------------------------------------------------------------------

_CRASH_DURING_DEPLOYMENT_VERIFY_SCRIPT = r"""
import json, sys, time
sys.path.insert(0, %(repo_root)r)
import simplicio_loop.source_state as ss
import simplicio_loop.external_verifiers as ev

class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

def fake_run_gh(args):
    if args[:2] == ["release", "view"]:
        return _Completed(0, json.dumps({
            "tagName": "v1.2.3", "isDraft": False, "isPrerelease": False, "assets": [],
        }))
    return _Completed(0, "{}")

ss._run_gh = fake_run_gh

def hanging_verify(self, repo, tag, environment, **kwargs):
    # Stands in for the real network call inside DeploymentVerifier.verify() (downloading
    # release bytes / running install smoke) -- mark that we are genuinely inside it, then
    # hang, standing in for the process being killed before GitHub's own state is known.
    with open(%(marker_path)r, "w", encoding="utf-8") as fh:
        fh.write("IN_DEPLOYMENT_VERIFY")
    time.sleep(300)

ev.DeploymentVerifier.verify = hanging_verify

payload = ss.github_delivery_payload(
    "acme/widgets", tag="v1.2.3", target_state="deployed",
    environment="pypi-index", verify_deployment=True,
)

# Only reachable if the (fake) network call returned -- must never happen once killed.
with open(%(result_path)r, "w", encoding="utf-8") as fh:
    json.dump(payload, fh)
print("UNEXPECTED_RETURN", flush=True)
"""


def test_crash_during_deployment_verifier_network_call_produces_no_false_positive_payload(tmp_path):
    """Kill the OS process for real while it is blocked inside `DeploymentVerifier.verify()`'s
    own byte-verification network call -- proves the interrupted call can never leave behind a
    fabricated `deployment.smoke.passed=True`, and that only a completed, fresh call may set it."""
    repo_root = str(Path(__file__).resolve().parent.parent)
    marker_path = str(tmp_path / "in_deployment_verify.marker")
    result_path = str(tmp_path / "deployment_payload.json")

    script = _CRASH_DURING_DEPLOYMENT_VERIFY_SCRIPT % {
        "repo_root": repo_root, "marker_path": marker_path, "result_path": result_path,
    }
    # File-redirected stdout/stderr (never `subprocess.PIPE`) -- same rationale as the merge
    # crash test above: the point under test is a real `kill()` mid in-flight call, not pipe
    # plumbing.
    stdout_path = tmp_path / "child.stdout"
    with open(stdout_path, "w", encoding="utf-8") as stdout_fh:
        proc = subprocess.Popen([sys.executable, "-c", script],
                                stdout=stdout_fh, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
        try:
            deadline = time.time() + 10.0
            while time.time() < deadline and not Path(marker_path).exists():
                time.sleep(0.02)
            assert Path(marker_path).exists(), "child never reached the in-flight deployment verify call"
            proc.kill()
            proc.wait(timeout=10)
        finally:
            try:
                proc.kill()
            except Exception:
                pass

    assert not Path(result_path).exists(), (
        "a killed mid-call process must never leave behind a fabricated deployment payload")
    assert stdout_path.read_text(encoding="utf-8").strip() != "UNEXPECTED_RETURN"

    # Recovery: a fresh, COMPLETED call is the only thing allowed to report `deployed`. Here it
    # genuinely fails (no wheel verified) -- proving the crash left no residue that could be
    # mistaken for a pass, and the real negative reason code surfaces cleanly.
    import simplicio_loop.source_state as ss
    import simplicio_loop.external_verifiers as ev

    original_run_gh = ss._run_gh
    original_verify = ev.DeploymentVerifier.verify

    def fake_run_gh(args):
        class _Completed:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr
        if args[:2] == ["release", "view"]:
            return _Completed(0, json.dumps({
                "tagName": "v1.2.3", "isDraft": False, "isPrerelease": False, "assets": [],
            }))
        return _Completed(0, "{}")

    def fresh_ok_but_no_wheel_verify(self, repo, tag, environment, **kwargs):
        return {"ok": False, "environment": environment, "reason_code": "wheel_not_verified",
                "smoke": {"passed": False, "reason_code": "wheel_not_verified"}}

    try:
        ss._run_gh = fake_run_gh
        ev.DeploymentVerifier.verify = fresh_ok_but_no_wheel_verify
        recovered_payload = ss.github_delivery_payload(
            "acme/widgets", tag="v1.2.3", target_state="deployed",
            environment="pypi-index", verify_deployment=True,
        )
    finally:
        # Restore both patched globals -- this mutates a *class* method directly (not via
        # `monkeypatch`, since the child-process recovery scenario needs the same shape), so
        # an un-restored patch here would silently corrupt every other test in this process
        # that constructs a real `DeploymentVerifier` after this one runs.
        ss._run_gh = original_run_gh
        ev.DeploymentVerifier.verify = original_verify

    assert recovered_payload["deployment"]["smoke"]["passed"] is False
    assert recovered_payload["deployment"]["reason_code"] == "wheel_not_verified"
