from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from simplicio_loop.hookwall_gate import validate_envelope
from simplicio_loop.mapper_hookwall import MapperHookwallEffectLedger
from simplicio_loop.mapper_operations import MapperOperationsAdapter


@dataclass
class FakeOperations:
    events: list[dict] = field(default_factory=list)
    effects: dict[str, str] = field(default_factory=dict)

    def replay(self, run_id: str) -> dict:
        events = [event for event in self.events if event["run_id"] == run_id]
        previous = "GENESIS"
        for event in events:
            assert event["prev_hash"] == previous
            previous = event["event_hash"]
        return {"valid": True, "events": events, "compaction": None}

    def append_event(self, run_id, event_type, payload, *, expected_seq):
        current = [event for event in self.events if event["run_id"] == run_id]
        assert expected_seq == len(current)
        seq = len(current) + 1
        event = {
            "run_id": run_id,
            "seq": seq,
            "event_id": f"event-{seq}",
            "event_type": event_type,
            "payload": dict(payload),
            "event_hash": f"hash-{seq}",
            "prev_hash": current[-1]["event_hash"] if current else "GENESIS",
            "created_at": f"2026-08-02T00:00:0{seq}Z",
        }
        self.events.append(event)
        return {"event_id": event["event_id"]}

    def prepare_effect_for_attempt(self, attempt_id, fence_token, *, effect_id, idempotency_key, payload):
        self.effects[idempotency_key] = "prepared"
        return {"status": "prepared", "effect_id": effect_id}

    def commit_effect_for_attempt(self, effect_id, attempt_id, fence_token, receipt):
        self.effects[effect_id.removeprefix("hookwall:")] = "committed"
        return {"status": "committed"}

    def mark_effect_unknown_for_attempt(self, effect_id, attempt_id, fence_token):
        self.effects[effect_id.removeprefix("hookwall:")] = "unknown"
        return {"status": "unknown"}

    def reconcile_effect(
        self, *, effect_id, attempt_id, outcome, receipt, fence_token=None
    ):
        assert attempt_id == "attempt-1"
        assert fence_token == "fence-1"
        assert outcome == "failed"
        assert receipt["outcome"] == "failed"
        self.effects[effect_id.removeprefix("hookwall:")] = "failed"
        return {"status": "failed", "state": "failed"}


def _request(
    fake,
    *,
    attempt_id: str = "attempt-1",
    fence: str = "fence-1",
    workspace: str = "/tmp",
):
    envelope = validate_envelope(
        {
            "schema": "simplicio.dispatch-envelope/v1",
            "envelope_id": "run:effect",
            "run_id": "run",
            "plan_id": "plan",
            "source_hash": "source",
            "policy_hash": "policy",
            "idempotency_key": "effect-key",
            "workspace": workspace,
            "fence": fence,
            "attempt_id": attempt_id,
            "effect_set": ["process", "write"],
            "write_set": ["repo:src"],
            "command": ["simplicio-dev-cli", "task"],
        }
    )
    pre = {
        "schema": "simplicio.hookwall-decision/v1",
        "phase": "pre",
        "verdict": "proceed",
        "reason_code": "policy_authorized",
        "envelope_id": envelope["envelope_id"],
        "envelope_hash": envelope["envelope_hash"],
        "source_hash": envelope["source_hash"],
        "policy_hash": envelope["policy_hash"],
        "fence": envelope["fence"],
    }
    return envelope, pre, MapperHookwallEffectLedger("/tmp/operations.sqlite", operations=fake)


def test_mapper_hookwall_commits_only_after_post_receipt():
    fake = FakeOperations()
    envelope, pre, ledger = _request(fake)
    assert ledger.reserve(envelope, pre)["action"] == "EXECUTE"
    assert fake.effects["effect-key"] == "prepared"
    ledger.effect_confirmed("effect-key", {"returncode": 0})
    receipt = {
        "schema": "simplicio.mutation-receipt/v1",
        "envelope_id": envelope["envelope_id"],
        "source_hash": envelope["source_hash"],
        "policy_hash": envelope["policy_hash"],
        "idempotency_key": envelope["idempotency_key"],
        "fence": envelope["fence"],
        "status": "committed",
        "result_hash": "result",
    }
    receipt["receipt_hash"] = hashlib.sha256(
        json.dumps(
            {key: receipt[key] for key in sorted(receipt) if key != "receipt_hash"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    post = {
        "schema": "simplicio.hookwall-decision/v1",
        "phase": "post",
        "verdict": "proceed",
        "reason_code": "effect_verified",
        "envelope_id": envelope["envelope_id"],
        "source_hash": envelope["source_hash"],
        "policy_hash": envelope["policy_hash"],
        "idempotency_key": envelope["idempotency_key"],
        "fence": envelope["fence"],
        "receipt_hash": receipt["receipt_hash"],
    }
    evidence = ledger.verify_and_commit(envelope, pre, receipt, post)
    assert evidence["verdict"] == "verified"
    assert fake.effects["effect-key"] == "committed"
    assert ledger.reserve(envelope, pre)["action"] == "REPLAY_VERIFIED"
    assert ledger.verify_audit_chain()["status"] == "VERIFIED"


def test_mapper_hookwall_reconciles_only_an_already_unknown_effect_as_failed():
    fake = FakeOperations()
    envelope, pre, ledger = _request(fake)

    assert ledger.reserve(envelope, pre)["action"] == "EXECUTE"
    assert ledger.mark_unresolved("effect-key", "effect_not_committed")["state"] == "UNCERTAIN"

    proof = {
        "schema": "simplicio.loop.effect-reconciliation-proof/v1",
        "outcome": "failed",
        "before_tree_hash": "tree-before",
        "after_tree_hash": "tree-before",
    }
    result = ledger.reconcile_failed("effect-key", proof)

    assert result["state"] == "FAILED"
    assert fake.effects["effect-key"] == "failed"
    assert ledger.status("effect-key")["state"] == "FAILED"
    # The journal-backed method is idempotent after the terminal failed state.
    assert ledger.reconcile_failed("effect-key", proof)["state"] == "FAILED"


def test_mapper_hookwall_failed_reconciliation_unblocks_real_mapper_completion(tmp_path):
    database = tmp_path / "operations.sqlite"
    operations = MapperOperationsAdapter(database, auto_create=True)
    operations.initialize()
    operations.register_slot("default", 1)
    operations.enqueue("task-1", {"kind": "test"}, idempotency_key="task-1")
    lease = operations.claim_next("worker-1")
    assert lease is not None

    envelope, pre, ledger = _request(
        operations,
        attempt_id=lease.attempt_id,
        fence=lease.fence_token,
        workspace=str(tmp_path),
    )
    assert ledger.reserve(envelope, pre)["action"] == "EXECUTE"
    ledger.mark_unresolved("effect-key", "effect_not_committed")
    ledger.reconcile_failed(
        "effect-key",
        {
            "schema": "simplicio.loop.effect-reconciliation-proof/v1",
            "outcome": "failed",
            "before_tree_hash": "tree-before",
            "after_tree_hash": "tree-before",
        },
    )

    completion = operations.complete(
        lease,
        status="failed",
        receipt={"status": "failed", "reason": "deterministic_no_mutation"},
    )
    assert completion["status"] == "failed"
