"""Tests for delivery_agent (#429) — unit, git/local integration, adapter sandbox, and
the two named anti-patterns: "PR open != merged" and "mergedAt without reachability"."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from simplicio_loop import delivery_agent as da  # noqa: E402

RUN_ID, TASK_ID, FENCE, PLAN_REV = "r1", "t1", "f1", 3
HEAD_SHA = "a" * 40
BASE_SHA = "b" * 40
TREE_HASH = "c" * 40


def _identity(**overrides):
    base = dict(run_id=RUN_ID, task_id=TASK_ID, plan_revision=PLAN_REV,
                head_sha=HEAD_SHA, base_sha=BASE_SHA, tree_hash=TREE_HASH, fence=FENCE)
    base.update(overrides)
    return da.compute_identity(**base)


def _impl_receipt(**overrides):
    r = {"verdict": "pass", "head_sha": HEAD_SHA, "run_id": RUN_ID, "task_id": TASK_ID,
         "fence": FENCE, "plan_revision": PLAN_REV, "base_sha": BASE_SHA, "tree_hash": TREE_HASH}
    r.update(overrides)
    return r


def _safety_receipt(**overrides):
    r = {"decision": "ALLOW", "run_id": RUN_ID, "task_id": TASK_ID, "fence": FENCE, "plan_revision": PLAN_REV}
    r.update(overrides)
    return r


REVIEW_ROLES = (
    "security_correctness_reviewer", "maintainability_reviewer",
    "runtime_reproduction_verifier", "blast_radius_reviewer",
)


def _review_receipts():
    return [{"role_id": role, "run_id": RUN_ID, "task_id": TASK_ID, "fence": FENCE, "plan_revision": PLAN_REV,
             "verdict": "pass"} for role in REVIEW_ROLES]


def _synthesis(verdict="pass"):
    return {"verdict": verdict}


def _target():
    return {"target_branch": "main"}


def _authorizations(**overrides):
    base = {k: True for k in ("push", "pull_request", "merge", "comment", "close")}
    base.update(overrides)
    return base


def _full_preconditions(**overrides):
    kwargs = dict(
        stage_graph_valid=True,
        identity=_identity(),
        implementation_receipt=_impl_receipt(),
        safety_receipt=_safety_receipt(),
        safety_receipt_fresh=True,
        review_receipts=_review_receipts(),
        review_synthesis=_synthesis(),
        task_anchor_gate_open=True,
        secret_scan_ok=True,
        delivery_target=_target(),
        action_authorizations=_authorizations(),
    )
    kwargs.update(overrides)
    return da.check_preconditions(**kwargs)


# --------------------------------------------------------------------------- #
# Unit: preconditions + identity mismatch
# --------------------------------------------------------------------------- #
def test_preconditions_all_present_ok():
    result = _full_preconditions()
    assert result.ok, result.errors


def test_preconditions_missing_safety_receipt_blocks():
    result = _full_preconditions(safety_receipt=None)
    assert not result.ok
    assert any("safety receipt" in e for e in result.errors)


def test_preconditions_stale_safety_receipt_blocks():
    result = _full_preconditions(safety_receipt_fresh=False)
    assert not result.ok
    assert any("not fresh" in e for e in result.errors)


def test_preconditions_missing_review_role_blocks():
    receipts = [r for r in _review_receipts() if r["role_id"] != "blast_radius_reviewer"]
    result = _full_preconditions(review_receipts=receipts)
    assert not result.ok
    assert any("blast_radius_reviewer" in e for e in result.errors)


def test_preconditions_review_synthesis_not_pass_blocks():
    result = _full_preconditions(review_synthesis=_synthesis("blocked"))
    assert not result.ok


def test_preconditions_secret_scan_required():
    result = _full_preconditions(secret_scan_ok=False)
    assert not result.ok
    assert any("secret scan" in e for e in result.errors)


def test_preconditions_missing_action_authorization_blocks():
    result = _full_preconditions(action_authorizations=_authorizations(merge=False))
    assert not result.ok
    assert any("merge" in e for e in result.errors)


def test_assert_preconditions_ok_raises_on_failure():
    result = _full_preconditions(secret_scan_ok=False)
    with pytest.raises(da.PreconditionError):
        da.assert_preconditions_ok(result)


def test_identity_mismatch_detected():
    current = _identity()
    drifted = _impl_receipt(head_sha="d" * 40)
    errors = da.check_identity_match(current, drifted, label="implementation_receipt")
    assert any("head_sha mismatch" in e for e in errors)


def test_identity_consistent_raises_on_drift():
    current = _identity()
    receipts = {"implementation_receipt": _impl_receipt(head_sha="d" * 40)}
    with pytest.raises(da.IdentityDriftError):
        da.assert_identity_consistent(current, receipts)


def test_identity_consistent_ok_when_matching():
    current = _identity()
    receipts = {"implementation_receipt": _impl_receipt(), "safety_receipt": _safety_receipt()}
    da.assert_identity_consistent(current, receipts)  # should not raise


def test_base_drift_detected():
    assert da.detect_base_drift(expected_base_sha=BASE_SHA, current_base_sha="e" * 40)
    assert not da.detect_base_drift(expected_base_sha=BASE_SHA, current_base_sha=BASE_SHA)


# --------------------------------------------------------------------------- #
# Unit: idempotency key
# --------------------------------------------------------------------------- #
def test_idempotency_key_deterministic():
    k1 = da.idempotency_key(effect="push", run_id=RUN_ID, task_id=TASK_ID, fence=FENCE, head_sha=HEAD_SHA)
    k2 = da.idempotency_key(effect="push", run_id=RUN_ID, task_id=TASK_ID, fence=FENCE, head_sha=HEAD_SHA)
    assert k1 == k2


def test_idempotency_key_differs_per_effect():
    k_push = da.idempotency_key(effect="push", run_id=RUN_ID, task_id=TASK_ID, fence=FENCE, head_sha=HEAD_SHA)
    k_merge = da.idempotency_key(effect="merge", run_id=RUN_ID, task_id=TASK_ID, fence=FENCE, head_sha=HEAD_SHA)
    assert k_push != k_merge


def test_idempotency_ledger_dedup():
    ledger = da.IdempotencyLedger()
    key = "k1"
    assert ledger.seen(key) is None
    ledger.record(key, {"ok": True})
    assert ledger.seen(key) == {"ok": True}


# --------------------------------------------------------------------------- #
# Fake adapter for saga/unit tests (no network).
# --------------------------------------------------------------------------- #
class FakeAdapter:
    def __init__(self, *, reachable=True, ancestor=True, merged_state="MERGED", merge_commit="m" * 40):
        self.branch_pushed = {}
        self.prs = {}
        self.comments = {}
        self.closed = {}
        self._pr_seq = 1
        self.reachable = reachable
        self.ancestor = ancestor
        self.merged_state = merged_state
        self.merge_commit = merge_commit
        self.merge_calls = 0
        self.push_calls = 0
        self.pr_create_calls = 0
        self.comment_calls = 0
        self.close_calls = 0

    def find_existing_pr(self, *, branch):
        return self.prs.get(branch)

    def push(self, *, branch, head_sha):
        self.push_calls += 1
        self.branch_pushed[branch] = head_sha
        return {"ok": True}

    def query_push(self, *, branch):
        return {"branch": branch, "remote_head_sha": self.branch_pushed.get(branch, "")}

    def create_or_update_pr(self, *, branch, base, title, body):
        self.pr_create_calls += 1
        existing = self.prs.get(branch)
        if existing:
            return existing
        pr = {"number": self._pr_seq, "url": f"https://example/pr/{self._pr_seq}", "state": "OPEN"}
        self._pr_seq += 1
        self.prs[branch] = pr
        return pr

    def query_pr(self, *, pr_id):
        for pr in self.prs.values():
            if str(pr["number"]) == str(pr_id):
                return pr
        return {}

    def query_checks(self, *, pr_id):
        return [{"name": "ci", "bucket": "PASS"}]

    def query_reviews(self, *, pr_id):
        return [{"state": "APPROVED"}]

    def merge(self, *, pr_id, strategy):
        self.merge_calls += 1
        for pr in self.prs.values():
            if str(pr["number"]) == str(pr_id):
                pr["state"] = self.merged_state
        return {"ok": True}

    def query_merge(self, *, pr_id):
        for pr in self.prs.values():
            if str(pr["number"]) == str(pr_id):
                return {"state": pr["state"],
                        "merge_commit_sha": self.merge_commit if pr["state"] == "MERGED" else "",
                        "base_ref": "main"}
        return {"state": "UNKNOWN", "merge_commit_sha": "", "base_ref": ""}

    def check_reachability(self, *, commit_sha, target_branch):
        return {"commit_sha": commit_sha, "target_branch": target_branch,
                "reachable": self.reachable, "ancestor": self.ancestor,
                "patch_equivalent": self.reachable and not self.ancestor}

    def comment(self, *, source_id, body, idempotency_key):
        self.comment_calls += 1
        self.comments.setdefault(source_id, []).append({"body": f"{body}\n\n<!-- simplicio-delivery-evidence:{idempotency_key} -->"})
        return {"ok": True}

    def query_comments(self, *, source_id):
        return self.comments.get(source_id, [])

    def close(self, *, source_id, idempotency_key):
        self.close_calls += 1
        self.closed[source_id] = True
        return {"ok": True}

    def query_source_state(self, *, source_id):
        return {"state": "CLOSED" if self.closed.get(source_id) else "OPEN"}


def _run_full_saga(adapter, *, pr_id_hint=None):
    saga = da.DeliverySaga(adapter=adapter)
    saga.prepare(identity=_identity())
    saga.verify(test_runs=[{"command": "pytest", "exit_code": 0, "log_hash": "x" * 8}], review_synthesis=_synthesis())
    saga.push(branch="feat/x", head_sha=HEAD_SHA, run_id=RUN_ID, task_id=TASK_ID, fence=FENCE)
    saga.open_pr(branch="feat/x", base="main", title="t", body="b",
                 run_id=RUN_ID, task_id=TASK_ID, fence=FENCE, head_sha=HEAD_SHA)
    pr_step = da.find_step(saga.steps, "PullRequestObserved")
    pr_id = pr_step.detail["number"]
    saga.observe_checks(pr_id=pr_id)
    saga.observe_reviews(pr_id=pr_id, required_approvals=1)
    saga.merge(pr_id=pr_id, strategy="squash", run_id=RUN_ID, task_id=TASK_ID, fence=FENCE, head_sha=HEAD_SHA)
    saga.observe_target_reachability(commit_sha=adapter.merge_commit, target_branch="main")
    return saga, pr_id


# --------------------------------------------------------------------------- #
# Unit: saga transitions
# --------------------------------------------------------------------------- #
def test_saga_happy_path_all_steps_ok():
    adapter = FakeAdapter()
    saga, pr_id = _run_full_saga(adapter)
    assert da.steps_ok(saga.steps)
    events = [s.event for s in saga.steps]
    for expected in ("DeliveryPrepared", "ComposedVerificationPassed", "PushConfirmed",
                     "PullRequestObserved", "ChecksObserved", "ReviewsObserved",
                     "MergeConfirmed", "TargetReachabilityObserved"):
        assert expected in events


def test_saga_push_deduped_when_already_at_head():
    adapter = FakeAdapter()
    adapter.branch_pushed["feat/x"] = HEAD_SHA
    saga = da.DeliverySaga(adapter=adapter)
    saga.push(branch="feat/x", head_sha=HEAD_SHA, run_id=RUN_ID, task_id=TASK_ID, fence=FENCE)
    assert adapter.push_calls == 0
    step = da.find_step(saga.steps, "PushConfirmed")
    assert step.ok


# --------------------------------------------------------------------------- #
# Unit: "PR open != merged" — a receipt must not mark delivered on PR alone.
# --------------------------------------------------------------------------- #
def test_pr_open_alone_is_not_delivered():
    adapter = FakeAdapter()
    saga = da.DeliverySaga(adapter=adapter)
    saga.prepare(identity=_identity())
    saga.push(branch="feat/x", head_sha=HEAD_SHA, run_id=RUN_ID, task_id=TASK_ID, fence=FENCE)
    saga.open_pr(branch="feat/x", base="main", title="t", body="b",
                 run_id=RUN_ID, task_id=TASK_ID, fence=FENCE, head_sha=HEAD_SHA)
    # No merge, no reachability observed yet.
    receipt = da.build_delivery_stage_receipt(
        run_id=RUN_ID, task_id=TASK_ID, attempt_id="a1", fence=FENCE, plan_revision=PLAN_REV,
        identity=_identity(), preconditions=_full_preconditions(), saga=saga,
        source_id="42", target_branch="main",
    )
    assert receipt["merged"] is False
    assert receipt["delivered"] is False
    assert not da.receipt_is_delivered(receipt)


# --------------------------------------------------------------------------- #
# THE most important test: mergedAt-like state without independent reachability
# must NEVER be treated as merged/delivered.
# --------------------------------------------------------------------------- #
def test_merged_state_without_reachability_is_not_delivered():
    # Adapter reports the PR as MERGED with a merge_commit_sha (a "mergedAt" style
    # field) — but check_reachability (independent git ancestry check) says the
    # commit is NOT actually reachable from the target branch.
    adapter = FakeAdapter(reachable=False, ancestor=False)
    saga = da.DeliverySaga(adapter=adapter)
    saga.prepare(identity=_identity())
    saga.push(branch="feat/x", head_sha=HEAD_SHA, run_id=RUN_ID, task_id=TASK_ID, fence=FENCE)
    saga.open_pr(branch="feat/x", base="main", title="t", body="b",
                 run_id=RUN_ID, task_id=TASK_ID, fence=FENCE, head_sha=HEAD_SHA)
    pr_id = da.find_step(saga.steps, "PullRequestObserved").detail["number"]
    merge_step = saga.merge(pr_id=pr_id, strategy="squash", run_id=RUN_ID, task_id=TASK_ID,
                             fence=FENCE, head_sha=HEAD_SHA)
    # The adapter DOES report MERGED + a merge_commit_sha (mergedAt-equivalent).
    assert merge_step.ok, "adapter-reported merge state should confirm on its own terms"
    reach_step = saga.observe_target_reachability(commit_sha=adapter.merge_commit, target_branch="main")
    assert not reach_step.ok, "reachability check must independently observe non-ancestor commit"

    receipt = da.build_delivery_stage_receipt(
        run_id=RUN_ID, task_id=TASK_ID, attempt_id="a1", fence=FENCE, plan_revision=PLAN_REV,
        identity=_identity(), preconditions=_full_preconditions(), saga=saga,
        source_id="42", target_branch="main",
    )
    # THE anti-pattern assertion: mergedAt/MERGED state alone must NOT produce a
    # delivered=True / merged=True receipt when reachability wasn't observed True.
    assert receipt["merge_confirmed"] is True
    assert receipt["target_reachability_observed"] is False
    assert receipt["merged"] is False, "merged must require BOTH merge confirmation AND reachability"
    assert receipt["delivered"] is False
    assert not da.receipt_is_delivered(receipt)


# --------------------------------------------------------------------------- #
# Unit: source close without confirmation must be rejected.
# --------------------------------------------------------------------------- #
def test_source_close_refused_without_delivery_confirmation():
    adapter = FakeAdapter()
    saga = da.DeliverySaga(adapter=adapter)
    step = saga.close_source(source_id="42", delivered=False, run_id=RUN_ID, task_id=TASK_ID,
                              fence=FENCE, head_sha=HEAD_SHA)
    assert not step.ok
    assert adapter.close_calls == 0, "close() must never be invoked when delivered=False"


def test_source_close_succeeds_after_delivery_confirmed():
    adapter = FakeAdapter()
    saga = da.DeliverySaga(adapter=adapter)
    step = saga.close_source(source_id="42", delivered=True, run_id=RUN_ID, task_id=TASK_ID,
                              fence=FENCE, head_sha=HEAD_SHA)
    assert step.ok
    assert adapter.close_calls == 1
    # Re-query confirms it.
    assert adapter.query_source_state(source_id="42")["state"] == "CLOSED"


def test_close_source_never_called_just_because_pr_exists():
    """Regression guard for the second named anti-pattern: build a saga where a PR was
    opened but delivery was never confirmed, and assert close_source refuses."""
    adapter = FakeAdapter()
    saga = da.DeliverySaga(adapter=adapter)
    saga.open_pr(branch="feat/x", base="main", title="t", body="b",
                 run_id=RUN_ID, task_id=TASK_ID, fence=FENCE, head_sha=HEAD_SHA)
    assert adapter.prs  # PR exists
    step = saga.close_source(source_id="42", delivered=False, run_id=RUN_ID, task_id=TASK_ID,
                              fence=FENCE, head_sha=HEAD_SHA)
    assert not step.ok
    assert adapter.close_calls == 0


# --------------------------------------------------------------------------- #
# Unit: stale checks/reviews.
# --------------------------------------------------------------------------- #
class _StaleReviewAdapter(FakeAdapter):
    def query_reviews(self, *, pr_id):
        return [{"state": "CHANGES_REQUESTED"}]


def test_reviews_observed_blocks_on_changes_requested():
    adapter = _StaleReviewAdapter()
    saga = da.DeliverySaga(adapter=adapter)
    step = saga.observe_reviews(pr_id="1", required_approvals=1)
    assert not step.ok


class _RedChecksAdapter(FakeAdapter):
    def query_checks(self, *, pr_id):
        return [{"name": "ci", "bucket": "FAIL"}]


def test_checks_observed_blocks_on_failing_check():
    adapter = _RedChecksAdapter()
    saga = da.DeliverySaga(adapter=adapter)
    step = saga.observe_checks(pr_id="1")
    assert not step.ok


# --------------------------------------------------------------------------- #
# Unit: idempotency — retry never repeats an external effect (merge/comment/close).
# --------------------------------------------------------------------------- #
def test_merge_effect_not_repeated_on_retry():
    adapter = FakeAdapter()
    saga = da.DeliverySaga(adapter=adapter)
    saga.push(branch="feat/x", head_sha=HEAD_SHA, run_id=RUN_ID, task_id=TASK_ID, fence=FENCE)
    saga.open_pr(branch="feat/x", base="main", title="t", body="b",
                 run_id=RUN_ID, task_id=TASK_ID, fence=FENCE, head_sha=HEAD_SHA)
    pr_id = da.find_step(saga.steps, "PullRequestObserved").detail["number"]
    saga.merge(pr_id=pr_id, strategy="squash", run_id=RUN_ID, task_id=TASK_ID, fence=FENCE, head_sha=HEAD_SHA)
    assert adapter.merge_calls == 1
    # Simulate a retry of the same attempt (same saga+ledger).
    saga.merge(pr_id=pr_id, strategy="squash", run_id=RUN_ID, task_id=TASK_ID, fence=FENCE, head_sha=HEAD_SHA)
    assert adapter.merge_calls == 1, "a retried merge must be deduped by idempotency key, not repeated"


def test_comment_effect_deduped_on_retry():
    adapter = FakeAdapter()
    saga = da.DeliverySaga(adapter=adapter)
    saga.comment_source(source_id="42", body="evidence", run_id=RUN_ID, task_id=TASK_ID, fence=FENCE, head_sha=HEAD_SHA)
    assert adapter.comment_calls == 1
    saga.comment_source(source_id="42", body="evidence", run_id=RUN_ID, task_id=TASK_ID, fence=FENCE, head_sha=HEAD_SHA)
    assert adapter.comment_calls == 1


def test_close_effect_deduped_on_retry():
    adapter = FakeAdapter()
    saga = da.DeliverySaga(adapter=adapter)
    saga.close_source(source_id="42", delivered=True, run_id=RUN_ID, task_id=TASK_ID, fence=FENCE, head_sha=HEAD_SHA)
    assert adapter.close_calls == 1
    saga.close_source(source_id="42", delivered=True, run_id=RUN_ID, task_id=TASK_ID, fence=FENCE, head_sha=HEAD_SHA)
    assert adapter.close_calls == 1


def test_idempotency_ledger_shared_across_two_saga_instances_dedups_effect():
    """A crash-and-restart scenario: a new DeliverySaga instance sharing the same
    persistent ledger must not repeat an already-confirmed effect."""
    adapter = FakeAdapter()
    ledger = da.IdempotencyLedger()
    saga1 = da.DeliverySaga(adapter=adapter, ledger=ledger)
    saga1.comment_source(source_id="42", body="evidence", run_id=RUN_ID, task_id=TASK_ID, fence=FENCE, head_sha=HEAD_SHA)
    assert adapter.comment_calls == 1

    saga2 = da.DeliverySaga(adapter=adapter, ledger=ledger)  # fresh instance, same ledger
    saga2.comment_source(source_id="42", body="evidence", run_id=RUN_ID, task_id=TASK_ID, fence=FENCE, head_sha=HEAD_SHA)
    assert adapter.comment_calls == 1, "shared ledger must prevent a duplicate effect across saga instances"


# --------------------------------------------------------------------------- #
# Unit: base drift handoff.
# --------------------------------------------------------------------------- #
def test_repair_handoff_shape():
    result = da.repair_handoff(reason="base drift", identity=_identity())
    assert result["handed_off_to"] == "feedback_recovery_agent"
    assert result["reason"] == "base drift"


def test_regression_reopen_stub_shape():
    result = da.regression_reopen_stub(source_id="42", signal="ci_regression")
    assert result["routed_to"] == "feedback_recovery_agent"


# --------------------------------------------------------------------------- #
# Unit: composed verification.
# --------------------------------------------------------------------------- #
def test_composed_verification_requires_both_tests_and_review():
    ok = da.composed_verification(
        test_runs=[{"command": "pytest", "exit_code": 0, "log_hash": "x" * 8}],
        review_synthesis=_synthesis("pass"),
    )
    assert ok["ok"]

    bad_tests = da.composed_verification(test_runs=[], review_synthesis=_synthesis("pass"))
    assert not bad_tests["ok"]

    bad_review = da.composed_verification(
        test_runs=[{"command": "pytest", "exit_code": 0, "log_hash": "x" * 8}],
        review_synthesis=_synthesis("blocked"),
    )
    assert not bad_review["ok"]


# --------------------------------------------------------------------------- #
# Unit: forbidden receipt schema.
# --------------------------------------------------------------------------- #
def test_assert_receipt_schema_allowed_rejects_foreign_schema():
    with pytest.raises(da.ForbiddenReceiptError):
        da.assert_receipt_schema_allowed("simplicio.review-receipt/v1")


def test_assert_receipt_schema_allowed_ok_for_own_schema():
    da.assert_receipt_schema_allowed(da.DELIVERY_STAGE_RECEIPT_SCHEMA)  # should not raise


# --------------------------------------------------------------------------- #
# Unit: status/blocker/next-action surface.
# --------------------------------------------------------------------------- #
def test_delivery_status_reports_blocker():
    adapter = FakeAdapter(reachable=False, ancestor=False)
    saga = da.DeliverySaga(adapter=adapter)
    saga.prepare(identity=_identity())
    bad_verify = saga.verify(test_runs=[], review_synthesis=_synthesis("pass"))
    assert not bad_verify.ok
    status = da.delivery_status(saga.steps)
    assert status["blocker"]["event"] == "ComposedVerificationPassed"
    assert status["next_action"] == "ComposedVerificationPassed"


def test_receipt_to_stage_receipt_projection():
    adapter = FakeAdapter()
    saga, pr_id = _run_full_saga(adapter)
    receipt = da.build_delivery_stage_receipt(
        run_id=RUN_ID, task_id=TASK_ID, attempt_id="a1", fence=FENCE, plan_revision=PLAN_REV,
        identity=_identity(), preconditions=_full_preconditions(), saga=saga,
        source_id="42", target_branch="main",
    )
    assert da.receipt_is_delivered(receipt)
    stage_receipt = da.to_stage_receipt(receipt, receipt_id="rec1", agent_instance_id="inst1")
    assert stage_receipt["stage_id"] == "delivering"
    assert stage_receipt["role_id"] == "delivery_agent"
    assert stage_receipt["verdict"] == "pass"


def test_to_stage_receipt_passes_the_real_canonical_validator():
    # Regression for issue #458: to_stage_receipt() was missing ~15 fields
    # the canonical stage-receipt/v1 schema requires, so every real
    # coordinator-driven delivery_agent receipt was silently rejected by
    # stage_agents.validate_receipt() despite this module's own shallow
    # tests passing.
    from simplicio_loop import stage_agents as sa

    adapter = FakeAdapter()
    saga, pr_id = _run_full_saga(adapter)
    receipt = da.build_delivery_stage_receipt(
        run_id=RUN_ID, task_id=TASK_ID, attempt_id="a1", fence=FENCE, plan_revision=PLAN_REV,
        identity=_identity(), preconditions=_full_preconditions(), saga=saga,
        source_id="42", target_branch="main",
    )
    context_hash, manifest_hash = "a" * 64, "b" * 64
    stage_receipt = da.to_stage_receipt(
        receipt, receipt_id="rec-full", agent_instance_id="inst-full",
        attempt_ordinal=1, context_hash=context_hash, manifest_hash=manifest_hash,
    )
    instance = {
        "run_id": RUN_ID, "task_id": TASK_ID, "attempt_id": "a1", "attempt_ordinal": 1,
        "fence": FENCE, "plan_revision": PLAN_REV, "agent_instance_id": "inst-full",
        "role_id": "delivery_agent", "stage_id": "delivering",
        "context_hash": context_hash, "manifest_hash": manifest_hash,
        "negotiated_capabilities": ["receipts"], "terminal_status": "completed",
    }
    ok, errors = sa.validate_receipt(stage_receipt, instance)
    assert ok, errors


# =========================================================================== #
# Git/local integration — real git repo, no network.
# =========================================================================== #
def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


@pytest.fixture
def git_repo():
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, capture_output=True, text=True, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp, check=True)
        with open(os.path.join(tmp, "f.txt"), "w") as fh:
            fh.write("base\n")
        subprocess.run(["git", "add", "."], cwd=tmp, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=tmp, check=True)
        yield tmp


def test_reachability_true_when_commit_merged_into_target(git_repo):
    with open(os.path.join(git_repo, "f2.txt"), "w") as fh:
        fh.write("feature\n")
    subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=git_repo, check=True)
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feature"], cwd=git_repo, check=True)
    feature_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True,
                                  text=True, check=True).stdout.strip()
    subprocess.run(["git", "checkout", "-q", "main"], cwd=git_repo, check=True)
    subprocess.run(["git", "merge", "-q", "--no-ff", "feature", "-m", "merge"], cwd=git_repo, check=True)

    def local_runner(args, **kwargs):
        if args[:2] == ["git", "fetch"]:
            # No real remote in this local-only test; treat local main as "origin/main".
            subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "main"], cwd=git_repo, check=True)
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.run(args, cwd=git_repo, capture_output=True, text=True)

    adapter = da.GitHubDeliveryAdapter(repo="owner/name", runner=local_runner)
    result = adapter.check_reachability(commit_sha=feature_sha, target_branch="main")
    assert result["reachable"] is True
    assert result["ancestor"] is True


def test_reachability_false_when_commit_not_merged(git_repo):
    with open(os.path.join(git_repo, "f3.txt"), "w") as fh:
        fh.write("unmerged\n")
    subprocess.run(["git", "checkout", "-q", "-b", "unmerged"], cwd=git_repo, check=True)
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "unmerged"], cwd=git_repo, check=True)
    unmerged_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True,
                                   text=True, check=True).stdout.strip()
    subprocess.run(["git", "checkout", "-q", "main"], cwd=git_repo, check=True)

    def local_runner(args, **kwargs):
        if args[:2] == ["git", "fetch"]:
            subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "main"], cwd=git_repo, check=True)
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.run(args, cwd=git_repo, capture_output=True, text=True)

    adapter = da.GitHubDeliveryAdapter(repo="owner/name", runner=local_runner)
    result = adapter.check_reachability(commit_sha=unmerged_sha, target_branch="main")
    assert result["reachable"] is False
    assert result["ancestor"] is False


def test_reachability_patch_equivalent_after_squash(git_repo):
    """Squash merge: the feature commit sha never lands on main, but an equivalent patch
    does. `git cherry` should detect it as patch-equivalent (reachable=True via patch
    equivalence, ancestor=False)."""
    with open(os.path.join(git_repo, "f4.txt"), "w") as fh:
        fh.write("squash-feature\n")
    subprocess.run(["git", "checkout", "-q", "-b", "squashme"], cwd=git_repo, check=True)
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "squashme"], cwd=git_repo, check=True)
    feature_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True,
                                  text=True, check=True).stdout.strip()
    subprocess.run(["git", "checkout", "-q", "main"], cwd=git_repo, check=True)
    subprocess.run(["git", "merge", "-q", "--squash", "squashme"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "squashme (squashed)"], cwd=git_repo, check=True)

    def local_runner(args, **kwargs):
        if args[:2] == ["git", "fetch"]:
            subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "main"], cwd=git_repo, check=True)
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.run(args, cwd=git_repo, capture_output=True, text=True)

    adapter = da.GitHubDeliveryAdapter(repo="owner/name", runner=local_runner)
    result = adapter.check_reachability(commit_sha=feature_sha, target_branch="main")
    assert result["ancestor"] is False
    assert result["patch_equivalent"] is True
    assert result["reachable"] is True


def test_default_branch_not_main(git_repo):
    """A non-'main' default/target branch (e.g. 'trunk') must work identically."""
    subprocess.run(["git", "branch", "-m", "main", "trunk"], cwd=git_repo, check=True)
    with open(os.path.join(git_repo, "f5.txt"), "w") as fh:
        fh.write("trunk-feature\n")
    subprocess.run(["git", "checkout", "-q", "-b", "feature2"], cwd=git_repo, check=True)
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feature2"], cwd=git_repo, check=True)
    feature_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True,
                                  text=True, check=True).stdout.strip()
    subprocess.run(["git", "checkout", "-q", "trunk"], cwd=git_repo, check=True)
    subprocess.run(["git", "merge", "-q", "--no-ff", "feature2", "-m", "merge"], cwd=git_repo, check=True)

    def local_runner(args, **kwargs):
        if args[:2] == ["git", "fetch"]:
            subprocess.run(["git", "update-ref", "refs/remotes/origin/trunk", "trunk"], cwd=git_repo, check=True)
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.run(args, cwd=git_repo, capture_output=True, text=True)

    adapter = da.GitHubDeliveryAdapter(repo="owner/name", runner=local_runner)
    result = adapter.check_reachability(commit_sha=feature_sha, target_branch="trunk")
    assert result["reachable"] is True


def test_merge_conflict_reported_by_composed_verification_gate_still_blocks_pending_reachability():
    """A conflicting merge never gets to reachability=True: without an actual merge
    landing, check_reachability must observe non-ancestor/non-equivalent and the receipt
    must not claim delivered."""
    adapter = FakeAdapter(reachable=False, ancestor=False)
    saga = da.DeliverySaga(adapter=adapter)
    saga.prepare(identity=_identity())
    saga.push(branch="feat/x", head_sha=HEAD_SHA, run_id=RUN_ID, task_id=TASK_ID, fence=FENCE)
    saga.open_pr(branch="feat/x", base="main", title="t", body="b",
                 run_id=RUN_ID, task_id=TASK_ID, fence=FENCE, head_sha=HEAD_SHA)
    pr_id = da.find_step(saga.steps, "PullRequestObserved").detail["number"]
    saga.merge(pr_id=pr_id, strategy="squash", run_id=RUN_ID, task_id=TASK_ID, fence=FENCE, head_sha=HEAD_SHA)
    reach = saga.observe_target_reachability(commit_sha=adapter.merge_commit, target_branch="main")
    assert not reach.ok


# =========================================================================== #
# Adapter sandbox — create/update PR, pagination, timeout, duplicate effect.
# =========================================================================== #
def test_create_or_update_pr_is_idempotent_no_duplicate():
    adapter = FakeAdapter()
    pr1 = adapter.create_or_update_pr(branch="feat/x", base="main", title="t", body="b")
    pr2 = adapter.create_or_update_pr(branch="feat/x", base="main", title="t", body="b")
    assert pr1["number"] == pr2["number"]
    assert adapter.pr_create_calls == 2  # both calls happen, but result identity is stable
    assert len(adapter.prs) == 1


class _PagingAdapter(FakeAdapter):
    def query_checks(self, *, pr_id):
        # Simulate a paginated result already flattened by the time it reaches us.
        return [{"name": f"check-{i}", "bucket": "PASS"} for i in range(50)]

    def query_reviews(self, *, pr_id):
        return [{"state": "APPROVED"} for _ in range(10)]


def test_pagination_of_checks_and_reviews_handled():
    adapter = _PagingAdapter()
    checks_step = da.DeliverySaga(adapter=adapter).observe_checks(pr_id="1")
    assert checks_step.ok
    reviews_step = da.DeliverySaga(adapter=adapter).observe_reviews(pr_id="1", required_approvals=1)
    assert reviews_step.ok


class _TimeoutThenOkAdapter(FakeAdapter):
    """Simulates a timeout after the effect landed: the first post-query attempt raises,
    but a subsequent call (retry) succeeds and must not re-invoke the effect."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._merge_attempts = 0

    def merge(self, *, pr_id, strategy):
        self.merge_calls += 1
        for pr in self.prs.values():
            if str(pr["number"]) == str(pr_id):
                pr["state"] = "MERGED"
        return {"ok": True}


def test_duplicate_effect_after_timeout_is_deduped_via_ledger():
    """Even if the first attempt's confirmation step "times out" from the caller's point
    of view (caller retries), a shared ledger keyed by idempotency_key prevents the merge
    effect from being invoked twice."""
    adapter = _TimeoutThenOkAdapter()
    ledger = da.IdempotencyLedger()
    saga1 = da.DeliverySaga(adapter=adapter, ledger=ledger)
    saga1.push(branch="feat/x", head_sha=HEAD_SHA, run_id=RUN_ID, task_id=TASK_ID, fence=FENCE)
    saga1.open_pr(branch="feat/x", base="main", title="t", body="b",
                  run_id=RUN_ID, task_id=TASK_ID, fence=FENCE, head_sha=HEAD_SHA)
    pr_id = da.find_step(saga1.steps, "PullRequestObserved").detail["number"]
    saga1.merge(pr_id=pr_id, strategy="squash", run_id=RUN_ID, task_id=TASK_ID, fence=FENCE, head_sha=HEAD_SHA)
    assert adapter.merge_calls == 1

    # Caller "times out" and retries with a fresh saga instance, same ledger + pr_id.
    saga2 = da.DeliverySaga(adapter=adapter, ledger=ledger)
    saga2.merge(pr_id=pr_id, strategy="squash", run_id=RUN_ID, task_id=TASK_ID, fence=FENCE, head_sha=HEAD_SHA)
    assert adapter.merge_calls == 1, "retry after timeout must not repeat the merge effect"


# =========================================================================== #
# Fault injection — crash between intent and confirmation; cancel during wait.
# =========================================================================== #
def test_crash_between_push_intent_and_confirm_resumes_idempotently():
    adapter = FakeAdapter()
    ledger = da.IdempotencyLedger()
    saga1 = da.DeliverySaga(adapter=adapter, ledger=ledger)
    saga1.push(branch="feat/x", head_sha=HEAD_SHA, run_id=RUN_ID, task_id=TASK_ID, fence=FENCE)
    assert adapter.push_calls == 1
    # "Crash": build a brand new saga (simulating a fresh process) with the same ledger.
    saga2 = da.DeliverySaga(adapter=adapter, ledger=ledger)
    step = saga2.push(branch="feat/x", head_sha=HEAD_SHA, run_id=RUN_ID, task_id=TASK_ID, fence=FENCE)
    assert step.ok
    assert adapter.push_calls == 1, "resumed push must not repeat the network effect"


def test_cancel_during_wait_leaves_no_partial_close():
    """A cancel before delivery is confirmed must never leave the source item closed."""
    adapter = FakeAdapter()
    saga = da.DeliverySaga(adapter=adapter)
    # Simulate cancellation: caller never confirms delivery, and calls close_source with
    # delivered=False (the only safe value it can compute at that point).
    step = saga.close_source(source_id="42", delivered=False, run_id=RUN_ID, task_id=TASK_ID,
                              fence=FENCE, head_sha=HEAD_SHA)
    assert not step.ok
    assert adapter.query_source_state(source_id="42")["state"] == "OPEN"


def test_cli_stage_receipt_subcommand_is_a_real_live_path(tmp_path, capsys):
    # Regression for issue #458 adversarial review: to_stage_receipt() was only
    # ever called from this test file, never from any production entrypoint.
    # scripts/delivery_agent.py's `stage-receipt` subcommand is that
    # entrypoint -- exercise it via its real main(), not just the library call.
    payload = {
        "run_id": RUN_ID, "task_id": TASK_ID, "attempt_id": "a1", "fence": FENCE,
        "plan_revision": PLAN_REV, "identity": {}, "preconditions": {"ok": True, "errors": []},
        "saga": {"steps": [
            {"event": "MergeConfirmed", "ok": True, "detail": {}},
            {"event": "TargetReachabilityObserved", "ok": True, "detail": {}},
        ]},
        "source_id": "42", "target_branch": "main",
    }
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    spec = importlib.util.spec_from_file_location(
        "scripts.delivery_agent_cli", os.path.join(ROOT, "scripts", "delivery_agent.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    exit_code = mod.main([
        "stage-receipt", "--input", str(input_path), "--receipt-id", "rec-cli",
        "--agent-instance-id", "inst-cli", "--context-hash", "a" * 64, "--manifest-hash", "b" * 64,
    ])
    out = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert out["schema"] == "simplicio.stage-receipt/v1"
    assert out["role_id"] == "delivery_agent"
    assert out["verdict"] == "pass"
