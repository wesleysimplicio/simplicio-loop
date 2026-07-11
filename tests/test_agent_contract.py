import pytest

from simplicio_loop.agent_contract import AgentContractError, bind_receipt, build_context_pack, validate_identity
from simplicio_loop.remote_queue import QueueConflict, SQLiteRemoteQueue


IDENTITY = {"agent_id": "codex-a", "runtime": "codex", "device_id": "laptop-a", "session_id": "s1",
            "capabilities": ["claim", "heartbeat", "fencing", "receipts"]}


def test_context_pack_is_allow_listed_and_receipt_is_agent_bound():
    pack = build_context_pack(task_id="T1", goal="fix docs", identity=IDENTITY,
                              acs=["tests pass"], source_refs=["README.md", "secret.txt"],
                              allowed_paths=["README.md"])
    assert pack["source_refs"] == ["README.md"]
    assert "prompt" not in pack and "transcript" not in pack
    receipt = bind_receipt({"status": "VERIFIED"}, IDENTITY, context_pack=pack)
    assert receipt["agent"] == validate_identity(IDENTITY)
    with pytest.raises(AgentContractError, match="duplicate capabilities"):
        validate_identity({**IDENTITY, "capabilities": ["claim", "claim"]})


def test_queue_persists_identity_and_rejects_replayed_identity(tmp_path):
    q = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    q.enqueue("T1")
    lease = q.claim("T1", "codex-a", idempotency_key="run:T1", identity=IDENTITY)
    assert lease.identity == validate_identity(IDENTITY)
    result = q.complete(lease, receipt_ref="receipts/T1.json")
    assert result["agent"]["device_id"] == "laptop-a"

    q.enqueue("T2")
    with pytest.raises(QueueConflict, match="agent_id does not match"):
        q.claim("T2", "claude-b", idempotency_key="run:T2", identity=IDENTITY)
