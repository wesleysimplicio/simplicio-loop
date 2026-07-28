"""Offline Loop admission for Fast generation receipts."""
from __future__ import annotations

from typing import Any, Mapping

from simplicio_fast.generation_receipts import verify_receipt


def audit_fast_generation(receipt: Mapping[str, Any], *,
                          repo: str, commit: str, generation: str,
                          source_hashes: Mapping[str, str],
                          changeset_hash: str | None = None) -> dict[str, Any]:
    verified = verify_receipt(
        receipt, expected_repo=repo, expected_commit=commit,
        expected_generation=generation,
        expected_source_hashes=source_hashes,
    )
    if changeset_hash is not None and (
        verified.get("downstream_changeset_hash") != changeset_hash
    ):
        raise ValueError("fast_receipt_changeset_mismatch")
    return {
        "schema": "simplicio.loop-fast-generation-audit/v1",
        "receipt_hash": verified["receipt_hash"],
        "generation": generation, "backend": verified["backend"],
        "fallback_reason": verified["fallback_reason"],
        "status": "VERIFIED", "completion_authority": "LOOP",
    }
