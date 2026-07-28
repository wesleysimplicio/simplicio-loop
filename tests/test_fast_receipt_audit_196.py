import pytest
from simplicio_fast.generation_receipts import seal_receipt
from simplicio_loop.fast_receipt_audit import audit_fast_generation


def test_loop_audits_receipt_without_opening_sfast():
    hashes = {"a.py": "a" * 64}
    receipt = seal_receipt(
        kind="rollout", repo="org/repo", commit="b" * 40,
        snapshot_digest="c" * 64, generation="g1",
        source_hashes=hashes, backend="python",
        fallback_reason="RUST_UNAVAILABLE",
        downstream_changeset_hash="d" * 64,
    )
    result = audit_fast_generation(
        receipt, repo="org/repo", commit="b" * 40, generation="g1",
        source_hashes=hashes, changeset_hash="d" * 64)
    assert result["status"] == "VERIFIED"
    assert result["completion_authority"] == "LOOP"


def test_loop_blocks_unbound_changeset():
    receipt = seal_receipt(
        kind="rollout", repo="r", commit="b", snapshot_digest="c",
        generation="g", source_hashes={"a": "h"}, backend="python",
        downstream_changeset_hash="one",
    )
    with pytest.raises(ValueError, match="changeset_mismatch"):
        audit_fast_generation(
            receipt, repo="r", commit="b", generation="g",
            source_hashes={"a": "h"}, changeset_hash="two")
