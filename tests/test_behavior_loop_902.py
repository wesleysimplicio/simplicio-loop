from __future__ import annotations

import pytest

from simplicio_loop.behavior_loop import BehaviorLoop, BehaviorLoopError


def test_feedback_proposal_requires_gate_and_promotes_with_receipt(tmp_path) -> None:
    loop = BehaviorLoop(tmp_path / "behavior.jsonl")
    proposal = loop.propose("repeated blocked delivery", "sha256:skill")
    with pytest.raises(BehaviorLoopError, match="action gate"):
        loop.promote(proposal["proposal_id"], authorization_digest="sha256:auth")
    promoted = loop.promote(proposal["proposal_id"], action_gate=True,
                             authorization_digest="sha256:auth")
    assert promoted["state"] == "promoted"
    assert loop.evaluate(proposal["proposal_id"])["state"] == "promoted"


def test_low_acceptance_archives_skill_and_is_idempotent(tmp_path) -> None:
    loop = BehaviorLoop(tmp_path / "behavior.jsonl", archive_threshold=0.5, minimum_samples=3)
    proposal = loop.propose("bad pattern", "sha256:skill")
    for index in range(3):
        loop.feedback(proposal["proposal_id"], accepted=index == 0,
                      evidence_digest=f"sha256:e{index}")
    result = loop.evaluate(proposal["proposal_id"])
    assert result["state"] == "archived"
    assert result["acceptance_rate"] == 1 / 3
    assert loop.evaluate(proposal["proposal_id"])["state"] == "archived"


def test_proposals_are_deduplicated_and_evidence_is_required(tmp_path) -> None:
    loop = BehaviorLoop(tmp_path / "behavior.jsonl")
    first = loop.propose("pattern", "sha256:skill")
    assert loop.propose("pattern", "sha256:skill")["proposal_id"] == first["proposal_id"]
    with pytest.raises(BehaviorLoopError, match="evidence"):
        loop.feedback(first["proposal_id"], accepted=True, evidence_digest="")
