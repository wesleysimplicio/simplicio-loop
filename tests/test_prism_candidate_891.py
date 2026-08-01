import pytest

from simplicio_loop.prism_reducer import Candidate, PrismReducerError


HASH = "a" * 64


def test_candidate_is_versioned_and_normalizes_changed_paths():
    candidate = Candidate(
        task_id="task-1",
        attempt=1,
        slot_id="slot-1",
        base_tree_hash=HASH,
        head_tree_hash="b" * 64,
        tree_hash="c" * 64,
        changed_paths=("b.py", "a.py", "a.py"),
        plan_hash="d" * 64,
        generation="mapper-7",
        operator_receipt_hash="e" * 64,
        evidence_receipt_hash="f" * 64,
        verification_status="verified",
    )

    assert candidate.to_dict()["schema"] == "simplicio.loop.candidate/v1"
    assert candidate.changed_paths == ("a.py", "b.py")
    assert len(candidate.candidate_hash) == 64


def test_candidate_rejects_unverified_or_malformed_receipts():
    with pytest.raises(PrismReducerError, match="unsupported candidate verification"):
        Candidate(
            "task-1", 1, "slot-1", HASH, HASH, HASH, (), HASH, "generation",
            HASH, HASH, "unverified",
        )
    with pytest.raises(PrismReducerError, match="lowercase SHA-256"):
        Candidate(
            "task-1", 1, "slot-1", "not-a-hash", HASH, HASH, (), HASH, "generation",
            HASH, HASH, "verified",
        )
