"""Loop-side admission of Fast's verified Mapper context handoff."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


class ContextPacketAdmissionError(ValueError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=True).encode()
    return hashlib.sha256(raw).hexdigest()


def admit_context_packet(packet: Mapping[str, Any],
                         fast_receipt: Mapping[str, Any], *,
                         slot_generation: str) -> dict[str, Any]:
    """Bind Mapper facts and Fast handles to a Loop slot without copying bodies."""
    if packet.get("schema") != "simplicio.context-packet/v1":
        raise ContextPacketAdmissionError("packet_schema_invalid")
    unsigned = dict(packet)
    unsigned.pop("encoded_bytes", None)
    packet_hash = unsigned.pop("packet_hash", "")
    if packet_hash != _digest(unsigned):
        raise ContextPacketAdmissionError("packet_corrupt")
    if packet.get("generation") != slot_generation:
        raise ContextPacketAdmissionError("packet_generation_stale")
    if fast_receipt.get("schema") != "simplicio.fast-context-consumption/v1":
        raise ContextPacketAdmissionError("fast_receipt_schema_invalid")
    if fast_receipt.get("packet_hash") != packet_hash:
        raise ContextPacketAdmissionError("fast_receipt_binding_mismatch")
    if fast_receipt.get("completion_authority") != "LOOP_ONLY":
        raise ContextPacketAdmissionError("completion_authority_invalid")
    expected_handles = [item["handle"] for item in packet.get("items", [])]
    if list(fast_receipt.get("handles", [])) != expected_handles:
        raise ContextPacketAdmissionError("fast_handle_set_mismatch")
    return {
        "schema": "simplicio.loop-context-admission/v1",
        "packet_hash": packet_hash, "generation": slot_generation,
        "handles": expected_handles, "coverage": packet.get("coverage"),
        "truncated": bool(packet.get("truncated")),
        "status": "ADMITTED", "completion_authority": "LOOP",
        "tokens": None, "tokens_null_reason": "NO_LLM_USED",
    }
