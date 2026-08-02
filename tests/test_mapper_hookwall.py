from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from simplicio_loop.hookwall_gate import validate_envelope
from simplicio_loop.mapper_hookwall import MapperHookwallEffectLedger


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


def _request(fake: FakeOperations):
    envelope = validate_envelope(
        {
            "schema": "simplicio.dispatch-envelope/v1",
            "envelope_id": "run:effect",
            "run_id": "run",
            "plan_id": "plan",
            "source_hash": "source",
            "policy_hash": "policy",
            "idempotency_key": "effect-key",
            "workspace": "/tmp",
            "fence": "fence-1",
            "attempt_id": "attempt-1",
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
