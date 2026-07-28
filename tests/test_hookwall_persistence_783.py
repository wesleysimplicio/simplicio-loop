from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from simplicio_loop.hookwall_gate import (
    DECISION_SCHEMA, ENVELOPE_SCHEMA, RECEIPT_SCHEMA, HookwallBlocked,
    validate_envelope,
)
from simplicio_loop.hookwall_persistence import HookwallEffectLedger


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def envelope(key="idem-1", fence=7):
    return validate_envelope({
        "schema": ENVELOPE_SCHEMA, "envelope_id": "env-" + key,
        "run_id": "run", "plan_id": "plan", "source_hash": "source",
        "policy_hash": "policy", "idempotency_key": key,
        "workspace": "/workspace/repo", "fence": fence,
        "effect_set": ["write"],
        "write_set": ["app.py"],
        "command": ["simplicio-dev-cli", "task", "apply"],
    })


def decision(env, phase="pre", receipt_hash=None):
    value = {
        "schema": DECISION_SCHEMA, "phase": phase, "verdict": "proceed",
        "reason_code": "authorized", "envelope_id": env["envelope_id"],
        "envelope_hash": env["envelope_hash"],
        "source_hash": env["source_hash"], "policy_hash": env["policy_hash"],
        "idempotency_key": env["idempotency_key"], "fence": env["fence"],
    }
    if receipt_hash:
        value["receipt_hash"] = receipt_hash
    return value


def receipt(env):
    value = {
        "schema": RECEIPT_SCHEMA, "envelope_id": env["envelope_id"],
        "source_hash": env["source_hash"], "policy_hash": env["policy_hash"],
        "idempotency_key": env["idempotency_key"], "fence": env["fence"],
        "status": "committed", "before_hash": "a", "after_hash": "b",
    }
    value["receipt_hash"] = digest(value)
    return value


def complete(ledger, env):
    pre = decision(env)
    ledger.reserve(env, pre)
    ledger.effect_confirmed(env["idempotency_key"], {"changed": True})
    rec = receipt(env)
    return ledger.verify_and_commit(
        env, pre, rec, decision(env, "post", rec["receipt_hash"])
    )


def test_atomic_happy_path_and_verified_retry_never_repeat_effect(tmp_path):
    ledger = HookwallEffectLedger(tmp_path / "hookwall.db")
    env = envelope()
    evidence = complete(ledger, env)
    assert evidence["ledger_event_hash"]
    replay = ledger.reserve(env, decision(env))
    assert replay["action"] == "REPLAY_VERIFIED"
    assert replay["evidence"]["evidence_hash"] == evidence["evidence_hash"]
    assert ledger.status("idem-1")["state"] == "VERIFIED"
    assert ledger.verify_audit_chain()["status"] == "VERIFIED"


@pytest.mark.parametrize("workers", (1, 20, 100))
def test_concurrent_duplicate_dispatch_admits_one_effect(tmp_path, workers):
    ledger = HookwallEffectLedger(tmp_path / f"hookwall-{workers}.db")
    env = envelope()

    def reserve():
        try:
            return ledger.reserve(env, decision(env))["action"]
        except HookwallBlocked as exc:
            return exc.reason_code

    with ThreadPoolExecutor(max_workers=min(workers, 20)) as pool:
        results = list(pool.map(lambda _: reserve(), range(workers)))
    assert results.count("EXECUTE") == 1
    assert all(
        item in {"EXECUTE", "effect_reconciliation_required"} for item in results
    )


def test_crash_before_effect_blocks_retry_without_mutation(tmp_path):
    ledger = HookwallEffectLedger(tmp_path / "before.db")
    env = envelope()
    ledger.reserve(env, decision(env))
    with pytest.raises(HookwallBlocked) as error:
        ledger.reserve(env, decision(env))
    assert error.value.reason_code == "effect_reconciliation_required"
    assert ledger.status("idem-1")["state"] == "RESERVED"


def test_crash_after_effect_never_replays_and_requires_reconcile(tmp_path):
    ledger = HookwallEffectLedger(tmp_path / "after.db")
    env = envelope()
    ledger.reserve(env, decision(env))
    ledger.effect_confirmed("idem-1", {"changed": True})
    ledger.mark_unresolved("idem-1", "crash_before_post")
    with pytest.raises(HookwallBlocked) as error:
        ledger.reserve(env, decision(env))
    assert error.value.reason_code == "effect_reconciliation_required"
    assert ledger.status("idem-1")["state"] == "UNCERTAIN"


def test_effect_unknown_bypass_cross_fence_and_tamper_fail_closed(tmp_path):
    ledger = HookwallEffectLedger(tmp_path / "guards.db")
    bad = dict(envelope())
    bad["effect_set"] = ["effect_unknown"]
    bad.pop("envelope_hash")
    with pytest.raises(HookwallBlocked) as unknown:
        ledger.reserve(bad, decision(envelope()))
    assert unknown.value.reason_code == "effect_unknown"

    env = envelope()
    complete(ledger, env)
    replay = envelope(fence=8)
    replay["idempotency_key"] = "idem-1"
    replay["envelope_hash"] = digest({
        key: replay[key] for key in sorted(replay) if key != "envelope_hash"
    })
    with pytest.raises(HookwallBlocked) as cross:
        ledger.reserve(replay, decision(replay))
    assert cross.value.reason_code == "idempotency_lineage_mismatch"

    with ledger._connect() as db:
        db.execute(
            "UPDATE hookwall_events SET payload_json='{}' WHERE sequence=1"
        )
    assert ledger.verify_audit_chain()["reason_code"] == "event_tampered"


def test_effect_confirmation_without_pre_gate_emits_bypass_block(tmp_path):
    ledger = HookwallEffectLedger(tmp_path / "bypass.db")
    with pytest.raises(HookwallBlocked) as error:
        ledger.effect_confirmed("missing", {"changed": True})
    assert error.value.reason_code == "hookwall_bypass"
    blocked = ledger.mark_unresolved("missing", "hookwall_bypass")
    assert blocked["state"] == "BLOCKED"
    assert ledger.verify_audit_chain()["status"] == "VERIFIED"


def test_path_symlink_and_command_escape_fail_closed(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)

    base = {
        "schema": ENVELOPE_SCHEMA, "envelope_id": "env-path",
        "run_id": "run", "plan_id": "plan", "source_hash": "source",
        "policy_hash": "policy", "idempotency_key": "path",
        "workspace": str(workspace), "fence": 1, "effect_set": ["write"],
        "write_set": ["../outside/file"], "command": ["simplicio-dev-cli"],
    }
    with pytest.raises(HookwallBlocked) as traversal:
        validate_envelope(base)
    assert traversal.value.reason_code == "path_escape"

    with pytest.raises(HookwallBlocked) as symlink:
        validate_envelope({**base, "write_set": ["escape/file"]})
    assert symlink.value.reason_code == "symlink_escape"

    with pytest.raises(HookwallBlocked) as command:
        validate_envelope({
            **base, "write_set": ["app.py"], "command": ["bash", "-c", "write"]
        })
    assert command.value.reason_code == "command_not_allowlisted"
