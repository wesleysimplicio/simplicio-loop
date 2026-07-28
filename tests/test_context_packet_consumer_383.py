import pytest
from simplicio_loop.context_packet_consumer import (
    ContextPacketAdmissionError, _digest, admit_context_packet,
)


def pair():
    packet = {
        "schema": "simplicio.context-packet/v1", "repo_id": "repo",
        "generation": "g1", "graph_digest": "g" * 64,
        "items": [{"handle": f"fast://context/{'g' * 64}/f"}],
        "coverage": .5, "truncated": True, "omitted_items": 1,
        "budget": {}, "ancestor_packet_hash": None, "lineage_reason": "INITIAL",
    }
    packet["packet_hash"] = _digest(packet)
    packet["encoded_bytes"] = 1
    receipt = {
        "schema": "simplicio.fast-context-consumption/v1",
        "packet_hash": packet["packet_hash"], "generation": "g1",
        "handles": [packet["items"][0]["handle"]],
        "completion_authority": "LOOP_ONLY",
    }
    return packet, receipt


def test_loop_admits_bound_packet_and_retains_completion_authority():
    packet, receipt = pair()
    result = admit_context_packet(packet, receipt, slot_generation="g1")
    assert result["status"] == "ADMITTED"
    assert result["completion_authority"] == "LOOP"
    assert result["tokens"] is None


@pytest.mark.parametrize("mutation,reason", [
    (lambda p, r: p.update(generation="old"), "packet_corrupt"),
    (lambda p, r: r.update(packet_hash="bad"), "fast_receipt_binding_mismatch"),
    (lambda p, r: r.update(handles=[]), "fast_handle_set_mismatch"),
])
def test_rejects_stale_or_unbound_handoff(mutation, reason):
    packet, receipt = pair()
    mutation(packet, receipt)
    with pytest.raises(ContextPacketAdmissionError) as error:
        admit_context_packet(packet, receipt, slot_generation="g1")
    assert error.value.reason_code == reason
