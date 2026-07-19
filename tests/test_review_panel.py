"""Tests for simplicio_loop.review_panel (issue #427, epic #422).

Four independent per-item reviewer roles materialized over the #423/#424
stage-agent contract: security_correctness_reviewer, maintainability_reviewer,
runtime_reproduction_verifier, blast_radius_reviewer.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simplicio_loop import review_panel as rp  # noqa: E402
from simplicio_loop import stage_agents as sa  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _receipt(role_id: str, *, agent_instance_id: str, verdict: str = rp.VERDICT_PASS,
             accepted: bool = True, findings=(), context_hash: str = "c" * 64,
             panel_sig: str = "p" * 64, plan_revision: int = 0) -> dict:
    return rp.make_review_receipt(
        receipt_id=f"receipt-{role_id}", agent_instance_id=agent_instance_id, role_id=role_id,
        run_id="run-1", task_id="task-1", attempt_id="attempt-1", fence="fence-0",
        plan_revision=plan_revision, context_hash=context_hash, panel_sig=panel_sig,
        verdict=verdict, findings=findings, evidence_refs=["ev-1"], accepted=accepted,
        created_at="2026-01-01T00:00:00Z",
    )


def _full_panel(**overrides) -> list[dict]:
    receipts = []
    for i, role_id in enumerate(rp.REVIEWER_ROLE_IDS):
        kwargs = dict(role_id=role_id, agent_instance_id=f"reviewer-{i}")
        kwargs.update(overrides.get(role_id, {}))
        receipts.append(_receipt(**kwargs))
    return receipts


# --------------------------------------------------------------------------
# 1. Roles registered with a separate-actor requirement.
# --------------------------------------------------------------------------


def test_four_roles_materialized_in_manifest():
    graph = sa.load_graph()
    role_ids = {r["role_id"] for r in graph["roles"]}
    for role_id in rp.REVIEWER_ROLE_IDS:
        assert role_id in role_ids


def test_role_definitions_require_separate_actor():
    roles = rp.build_role_definitions()
    assert len(roles) == 4
    for role in roles:
        assert "implementation_agent" in role["independent_of_roles"]
        others = [r for r in rp.REVIEWER_ROLE_IDS if r != role["role_id"]]
        for other in others:
            assert other in role["independent_of_roles"]


# --------------------------------------------------------------------------
# 2. Unit: same-actor-identity rejected.
# --------------------------------------------------------------------------


def test_same_actor_as_implementer_rejected():
    with pytest.raises(rp.ReviewPanelError) as exc:
        rp.reject_same_actor(
            implementer_instance_id="impl-1",
            reviewer_instance_ids={"security_correctness_reviewer": "impl-1", "maintainability_reviewer": "r-2"},
        )
    assert exc.value.reason_code == rp.REASON_SAME_ACTOR


def test_two_reviewers_sharing_instance_rejected():
    with pytest.raises(rp.ReviewPanelError) as exc:
        rp.reject_same_actor(
            implementer_instance_id="impl-1",
            reviewer_instance_ids={"security_correctness_reviewer": "r-1", "maintainability_reviewer": "r-1"},
        )
    assert exc.value.reason_code == rp.REASON_SAME_ACTOR


def test_distinct_instances_accepted():
    ok, errors = rp.enforce_panel_independence(
        implementer_instance_id="impl-1",
        reviewer_instance_ids={rid: f"r-{i}" for i, rid in enumerate(rp.REVIEWER_ROLE_IDS)},
    )
    assert ok
    assert errors == []


# --------------------------------------------------------------------------
# 3. Unit: wrong rubric hash.
# --------------------------------------------------------------------------


def test_wrong_rubric_hash_rejected():
    receipt = _receipt("security_correctness_reviewer", agent_instance_id="r-1")
    receipt["rubric_hash"] = "0" * 64
    ok, errors = rp.validate_review_receipt(
        receipt, expected_context_hash="c" * 64, expected_panel_signature="p" * 64,
    )
    assert not ok
    assert any("rubric_hash" in e for e in errors)


def test_correct_rubric_hash_accepted():
    receipt = _receipt("security_correctness_reviewer", agent_instance_id="r-1")
    ok, errors = rp.validate_review_receipt(
        receipt, expected_context_hash="c" * 64, expected_panel_signature="p" * 64,
    )
    assert ok, errors


def test_rubric_hash_distinct_per_role_and_stable():
    hashes = {role_id: rp.rubric_hash(role_id) for role_id in rp.REVIEWER_ROLE_IDS}
    assert len(set(hashes.values())) == 4
    assert rp.rubric_hash("security_correctness_reviewer") == hashes["security_correctness_reviewer"]


# --------------------------------------------------------------------------
# 4. Unit: finding schema.
# --------------------------------------------------------------------------


def test_finding_schema_valid():
    finding = rp.make_finding(
        role_id="security_correctness_reviewer", file="src/auth.py", line=42,
        claim="Missing authz check on delete endpoint", finding_class="security",
        confidence="high", evidence_refs=["src/auth.py:42"],
    )
    assert finding["schema"] == "simplicio.review-finding/v1"
    assert finding["normalized_claim"] == "missing authz check on delete endpoint"


def test_finding_schema_rejects_bad_class():
    with pytest.raises(rp.ReviewPanelError):
        rp.make_finding(role_id="security_correctness_reviewer", file="a.py", line=1,
                        claim="x", finding_class="not_a_class")


def test_finding_schema_rejects_bad_line():
    with pytest.raises(rp.ReviewPanelError):
        rp.make_finding(role_id="security_correctness_reviewer", file="a.py", line=-1,
                        claim="x", finding_class="security")


# --------------------------------------------------------------------------
# 5. Unit: dedup/vote.
# --------------------------------------------------------------------------


def test_dedup_findings_by_file_line_and_normalized_claim():
    f1 = rp.make_finding(role_id="security_correctness_reviewer", file="a.py", line=10,
                         claim="Missing authz check!", finding_class="security", confidence="medium")
    f2 = rp.make_finding(role_id="blast_radius_reviewer", file="a.py", line=10,
                         claim="missing authz check", finding_class="security", confidence="high")
    f3 = rp.make_finding(role_id="maintainability_reviewer", file="b.py", line=5,
                         claim="dead code", finding_class="maintainability", confidence="low")
    deduped = rp.dedup_findings([f1, f2, f3])
    assert len(deduped) == 2
    dup = next(d for d in deduped if d["file"] == "a.py")
    assert dup["votes"] == 2
    assert dup["max_confidence"] == "high"
    assert set(dup["roles"]) == {"security_correctness_reviewer", "blast_radius_reviewer"}


def test_dedup_never_drops_lone_high_confidence_security_finding():
    f1 = rp.make_finding(role_id="security_correctness_reviewer", file="a.py", line=1,
                         claim="sql injection", finding_class="security", confidence="high")
    deduped = rp.dedup_findings([f1])
    assert len(deduped) == 1
    assert deduped[0]["votes"] == 1
    assert deduped[0]["max_confidence"] == "high"


# --------------------------------------------------------------------------
# 6. Unit: security single-vote block.
# --------------------------------------------------------------------------


def test_single_high_confidence_security_finding_blocks_synthesis():
    finding = rp.make_finding(role_id="security_correctness_reviewer", file="a.py", line=1,
                              claim="hardcoded secret", finding_class="security", confidence="high")
    receipts = _full_panel(security_correctness_reviewer={"findings": [finding], "verdict": "fail", "accepted": False})
    result = rp.synthesize(receipts)
    assert result["verdict"] == rp.VERDICT_FIX_REQUIRED
    assert result["reason_code"] == "high_confidence_security_finding"


# --------------------------------------------------------------------------
# 7. Unit: majority-refute.
# --------------------------------------------------------------------------


def test_majority_refute_on_ac_sends_back_to_implementation():
    finding_a = rp.make_finding(role_id="maintainability_reviewer", file="a.py", line=1,
                                claim="AC not met", finding_class="maintainability", confidence="medium")
    finding_b = rp.make_finding(role_id="runtime_reproduction_verifier", file="a.py", line=1,
                                claim="AC not met", finding_class="maintainability", confidence="medium")
    finding_c = rp.make_finding(role_id="blast_radius_reviewer", file="a.py", line=1,
                                claim="AC not met", finding_class="maintainability", confidence="medium")
    receipts = _full_panel(
        maintainability_reviewer={"findings": [finding_a]},
        runtime_reproduction_verifier={"findings": [finding_b]},
        blast_radius_reviewer={"findings": [finding_c]},
    )
    result = rp.synthesize(receipts)
    assert result["verdict"] == rp.VERDICT_FIX_REQUIRED
    assert result["reason_code"] == "majority_refute"


def test_single_dissent_does_not_block_pass():
    finding_a = rp.make_finding(role_id="maintainability_reviewer", file="a.py", line=1,
                                claim="nitpick", finding_class="maintainability", confidence="low")
    receipts = _full_panel(maintainability_reviewer={"findings": [finding_a]})
    result = rp.synthesize(receipts)
    assert result["verdict"] == rp.VERDICT_PASS


# --------------------------------------------------------------------------
# 8. Unit: reviewer missing/timeout -> BLOCKED, never PASS.
# --------------------------------------------------------------------------


def test_missing_reviewer_blocks_not_passes():
    receipts = _full_panel()
    receipts = [r for r in receipts if r["role_id"] != "blast_radius_reviewer"]
    result = rp.synthesize(receipts)
    assert result["verdict"] == rp.VERDICT_BLOCKED
    assert result["reason_code"] == rp.REASON_PANEL_INCOMPLETE
    assert "blast_radius_reviewer" in result["missing_roles"]


def test_all_reviewers_missing_is_independent_reviewer_unavailable():
    result = rp.synthesize([])
    assert result["verdict"] == rp.VERDICT_BLOCKED
    assert result["reason_code"] == rp.REASON_INDEPENDENT_REVIEWER_UNAVAILABLE


def test_reviewer_blocked_keeps_gate_non_terminal():
    receipts = _full_panel(runtime_reproduction_verifier={"verdict": rp.VERDICT_BLOCKED, "accepted": False})
    result = rp.synthesize(receipts)
    assert result["verdict"] == rp.VERDICT_BLOCKED
    assert result["reason_code"] == rp.REASON_INDEPENDENT_REVIEWER_UNAVAILABLE


def test_runtime_without_independent_actor_blocks_waves():
    with pytest.raises(rp.ReviewPanelError) as exc:
        rp.plan_reviewer_waves(0)
    assert exc.value.reason_code == rp.REASON_INDEPENDENT_REVIEWER_UNAVAILABLE


def test_waves_split_four_roles_by_capacity():
    waves = rp.plan_reviewer_waves(2)
    assert waves == [list(rp.REVIEWER_ROLE_IDS[:2]), list(rp.REVIEWER_ROLE_IDS[2:])]
    assert rp.plan_reviewer_waves(4) == [list(rp.REVIEWER_ROLE_IDS)]


# --------------------------------------------------------------------------
# 9. Unit: stale after new commit (invalidation on diff/head/plan_revision change).
# --------------------------------------------------------------------------


def test_stale_after_new_head_invalidates_receipt():
    sig_old = rp.panel_signature(base_hash="a" * 64, head_sha="head-1", plan_revision=0)
    sig_new = rp.panel_signature(base_hash="a" * 64, head_sha="head-2", plan_revision=0)
    receipt = _receipt("security_correctness_reviewer", agent_instance_id="r-1", panel_sig=sig_old)
    assert not rp.is_stale(receipt, current_signature=sig_old)
    assert rp.is_stale(receipt, current_signature=sig_new)

    ok, errors = rp.validate_review_receipt(
        receipt, expected_context_hash="c" * 64, expected_panel_signature=sig_new,
    )
    assert not ok
    assert any("stale" in e for e in errors)


def test_a_fix_generating_new_head_invalidates_all_four_receipts():
    sig_before = rp.panel_signature(base_hash="a" * 64, head_sha="head-1", plan_revision=0)
    sig_after = rp.panel_signature(base_hash="a" * 64, head_sha="head-2", plan_revision=0)
    receipts = _full_panel()
    for r in receipts:
        r["panel_signature"] = sig_before
    for r in receipts:
        assert rp.is_stale(r, current_signature=sig_after)


# --------------------------------------------------------------------------
# Adversarial: implementer tries to forge a reviewer receipt.
# --------------------------------------------------------------------------


def test_forged_receipt_same_instance_as_implementer_rejected():
    receipt = _receipt("security_correctness_reviewer", agent_instance_id="impl-1")
    ok, errors = rp.validate_review_receipt(
        receipt, expected_context_hash="c" * 64, expected_panel_signature="p" * 64,
        implementer_instance_id="impl-1",
    )
    assert not ok
    assert any("same-actor" in e for e in errors)


def test_forged_receipt_wrong_context_hash_rejected():
    receipt = _receipt("security_correctness_reviewer", agent_instance_id="r-1", context_hash="d" * 64)
    ok, errors = rp.validate_review_receipt(
        receipt, expected_context_hash="c" * 64, expected_panel_signature="p" * 64,
    )
    assert not ok
    assert any("context_hash" in e for e in errors)


def test_receipt_with_findings_authored_by_another_role_rejected():
    foreign_finding = rp.make_finding(role_id="maintainability_reviewer", file="a.py", line=1,
                                      claim="x", finding_class="maintainability")
    receipt = _receipt("security_correctness_reviewer", agent_instance_id="r-1", findings=[foreign_finding])
    ok, errors = rp.validate_review_receipt(
        receipt, expected_context_hash="c" * 64, expected_panel_signature="p" * 64,
    )
    assert not ok
    assert any("not authored by this reviewer" in e for e in errors)


# --------------------------------------------------------------------------
# Context bundle: sanitized + content-addressed; no implementer private reasoning.
# --------------------------------------------------------------------------


def test_context_bundle_strips_private_reasoning():
    bundle = rp.build_context_bundle(
        diff="--- a/x\n+++ b/x\n", acceptance_criteria=["AC1"], evidence_refs=["ev"],
        base_hash="a" * 64,
        raw_extra={"transcript": "secret CoT", "private_reasoning": "secret", "public_note": "ok"},
    )
    assert "transcript" not in bundle["extra"]
    assert "private_reasoning" not in bundle["extra"]
    assert bundle["extra"]["public_note"] == "ok"
    assert set(bundle["sanitized_keys_removed"]) == {"transcript", "private_reasoning"}


def test_context_bundle_is_content_addressed_and_stable():
    kwargs = dict(diff="d", acceptance_criteria=["AC1"], evidence_refs=["ev"], base_hash="a" * 64)
    b1 = rp.build_context_bundle(**kwargs)
    b2 = rp.build_context_bundle(**kwargs)
    assert b1["context_hash"] == b2["context_hash"]

    b3 = rp.build_context_bundle(diff="different", acceptance_criteria=["AC1"], evidence_refs=["ev"], base_hash="a" * 64)
    assert b3["context_hash"] != b1["context_hash"]


def test_context_bundle_same_for_all_four_reviewers():
    kwargs = dict(diff="d", acceptance_criteria=["AC1"], evidence_refs=["ev"], base_hash="a" * 64)
    bundles = [rp.build_context_bundle(**kwargs) for _ in rp.REVIEWER_ROLE_IDS]
    assert len({b["context_hash"] for b in bundles}) == 1


# --------------------------------------------------------------------------
# System: fix -> re-review -> pass.
# --------------------------------------------------------------------------


def test_fix_then_re_review_then_pass():
    finding = rp.make_finding(role_id="security_correctness_reviewer", file="a.py", line=1,
                              claim="hardcoded secret", finding_class="security", confidence="high")
    first_pass = rp.synthesize(_full_panel(
        security_correctness_reviewer={"findings": [finding], "verdict": "fail", "accepted": False},
    ))
    assert first_pass["verdict"] == rp.VERDICT_FIX_REQUIRED

    # after the fix, a fresh panel with no findings passes.
    second_pass = rp.synthesize(_full_panel())
    assert second_pass["verdict"] == rp.VERDICT_PASS


# --------------------------------------------------------------------------
# Adversarial: synthesis must actually validate receipts, not trust them.
# --------------------------------------------------------------------------


def test_synthesize_rejects_forged_context_hash_when_validation_wired():
    receipts = _full_panel()
    receipts[0]["context_hash"] = "d" * 64  # forged/cross-head artifact
    result = rp.synthesize(
        receipts, expected_context_hash="c" * 64, expected_panel_signature="p" * 64,
    )
    assert result["verdict"] == rp.VERDICT_BLOCKED
    assert result["reason_code"] == "invalid_receipt"


def test_synthesize_rejects_stale_receipt_when_validation_wired():
    receipts = _full_panel()
    receipts[0]["panel_signature"] = "stale" * 12 + "x" * 4
    result = rp.synthesize(
        receipts, expected_context_hash="c" * 64, expected_panel_signature="p" * 64,
    )
    assert result["verdict"] == rp.VERDICT_BLOCKED
    assert result["reason_code"] == "invalid_receipt"


def test_synthesize_rejects_same_actor_receipt_when_validation_wired():
    receipts = _full_panel()
    result = rp.synthesize(
        receipts, expected_context_hash="c" * 64, expected_panel_signature="p" * 64,
        implementer_instance_id=receipts[0]["agent_instance_id"],
    )
    assert result["verdict"] == rp.VERDICT_BLOCKED
    assert result["reason_code"] == "invalid_receipt"


def test_synthesize_still_passes_a_genuinely_valid_panel_when_validated():
    receipts = _full_panel()
    result = rp.synthesize(
        receipts, expected_context_hash="c" * 64, expected_panel_signature="p" * 64,
        implementer_instance_id="implementer-not-in-panel",
    )
    assert result["verdict"] == rp.VERDICT_PASS


def test_synthesize_without_expected_hash_args_skips_validation_backward_compatibly():
    receipts = _full_panel()
    receipts[0]["context_hash"] = "d" * 64  # would be forged, but no validation args passed
    result = rp.synthesize(receipts)
    assert result["verdict"] == rp.VERDICT_PASS


# --------------------------------------------------------------------------
# System: panel with enough slots / across two waves.
# --------------------------------------------------------------------------


def test_panel_with_four_slots_runs_in_one_wave():
    assert rp.plan_reviewer_waves(4) == [list(rp.REVIEWER_ROLE_IDS)]


def test_panel_across_two_waves_covers_all_roles():
    waves = rp.plan_reviewer_waves(3)
    covered = [role for wave in waves for role in wave]
    assert sorted(covered) == sorted(rp.REVIEWER_ROLE_IDS)
    assert len(waves) == 2
