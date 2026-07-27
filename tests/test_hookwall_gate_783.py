import hashlib
import json

import pytest

from simplicio_loop.hookwall_gate import (
    DECISION_SCHEMA,
    ENVELOPE_SCHEMA,
    RECEIPT_SCHEMA,
    HookwallBlocked,
    gate_completion,
    validate_envelope,
    validate_pre_decision,
    verify_post_receipt,
)


def stable_hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def envelope(**updates):
    value = {
        "schema": ENVELOPE_SCHEMA,
        "envelope_id": "env-1",
        "run_id": "run-1",
        "plan_id": "plan-1",
        "source_hash": "source-a",
        "policy_hash": "policy-a",
        "idempotency_key": "idem-1",
        "workspace": "/workspace/repo",
        "fence": "7",
        "effect_set": ["write"],
    }
    value.update(updates)
    return validate_envelope(value)


def decision(env, phase="pre", verdict="proceed", **updates):
    value = {
        "schema": DECISION_SCHEMA,
        "phase": phase,
        "verdict": verdict,
        "envelope_id": env["envelope_id"],
        "envelope_hash": env["envelope_hash"],
        "source_hash": env["source_hash"],
        "policy_hash": env["policy_hash"],
        "idempotency_key": env["idempotency_key"],
        "fence": env["fence"],
    }
    value.update(updates)
    return value


def receipt(env, **updates):
    value = {
        "schema": RECEIPT_SCHEMA,
        "envelope_id": env["envelope_id"],
        "source_hash": env["source_hash"],
        "policy_hash": env["policy_hash"],
        "idempotency_key": env["idempotency_key"],
        "fence": env["fence"],
        "status": "committed",
        "before_hash": "before",
        "after_hash": "after",
    }
    value.update(updates)
    value["receipt_hash"] = stable_hash(value)
    return value


def test_clean_pre_post_chain_yields_completion_evidence():
    env = envelope()
    pre = decision(env)
    rec = receipt(env)
    post = decision(env, phase="post", receipt_hash=rec["receipt_hash"])
    evidence = verify_post_receipt(env, pre, rec, post)
    assert evidence["verdict"] == "verified"
    assert gate_completion(evidence) == (True, "ok")


@pytest.mark.parametrize("effect", [[], ["effect_unknown"], ["network-magic"]])
def test_unresolved_or_unsupported_effect_fails_closed(effect):
    with pytest.raises(HookwallBlocked) as error:
        envelope(effect_set=effect)
    assert error.value.reason_code in {"invalid_envelope", "effect_unknown"}


def test_missing_pre_and_block_verdict_never_authorize():
    env = envelope()
    with pytest.raises(HookwallBlocked, match="hookwall_pre_missing"):
        validate_pre_decision(env, None)
    with pytest.raises(HookwallBlocked) as error:
        validate_pre_decision(env, decision(env, verdict="block", reason_code="policy_denied"))
    assert error.value.reason_code == "policy_denied"


def test_source_drift_and_receipt_tamper_fail_closed():
    env = envelope()
    with pytest.raises(HookwallBlocked, match="hookwall_lineage_mismatch"):
        validate_pre_decision(env, decision(env, source_hash="source-b"))
    pre = decision(env)
    rec = receipt(env)
    rec["after_hash"] = "tampered"
    post = decision(env, phase="post", receipt_hash=rec["receipt_hash"])
    with pytest.raises(HookwallBlocked, match="mutation_receipt_hash_mismatch"):
        verify_post_receipt(env, pre, rec, post)


def test_missing_post_and_duplicate_retry_do_not_complete_twice():
    env = envelope()
    pre = decision(env)
    rec = receipt(env)
    with pytest.raises(HookwallBlocked, match="hookwall_post_missing"):
        verify_post_receipt(env, pre, rec, None)
    post = decision(env, phase="post", receipt_hash=rec["receipt_hash"])
    committed = set()
    verify_post_receipt(env, pre, rec, post, seen_idempotency_keys=committed)
    with pytest.raises(HookwallBlocked, match="duplicate_effect"):
        verify_post_receipt(env, pre, rec, post, seen_idempotency_keys=committed)


def test_tampered_completion_evidence_is_rejected():
    env = envelope()
    rec = receipt(env)
    evidence = verify_post_receipt(
        env, decision(env), rec, decision(env, phase="post", receipt_hash=rec["receipt_hash"])
    )
    evidence["receipt_hash"] = "forged"
    assert gate_completion(evidence) == (False, "hookwall_evidence_hash_mismatch")
