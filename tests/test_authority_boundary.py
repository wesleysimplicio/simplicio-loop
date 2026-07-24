from __future__ import annotations

import json

import pytest

from simplicio_loop.authority_boundary import (
    AUTHORIZATION_FILENAME,
    AuthorityBoundaryError,
    canonical_hash,
    prepare_authorization_handoff,
    validate_effect_authorization,
)


def _authorization(**overrides):
    payload = {
        "schema": "simplicio.effect-authorization/v1",
        "proposal_digest": "a" * 64,
        "effect_digest": "b" * 64,
        "effect_id": "effect-1",
        "plan_node_id": "node-1",
        "authority": "coordinator-1",
        "capability": "write",
        "policy_revision": "policy-1",
        "attempt_id": "attempt-1",
        "lease_id": "lease-1",
        "fencing_token": "fence-1",
        "context_handle": "ctx-1",
        "issuer": "simplicio-loop",
        "issued_at": 100.0,
        "expires_at": 200.0,
        "human_gate_receipt": "",
    }
    payload.update(overrides)
    payload["authorization_digest"] = canonical_hash(payload)
    return payload


def test_coordinator_authorization_round_trips_with_dev_cli_hash_shape():
    payload = _authorization()
    summary = validate_effect_authorization(payload, now=150.0, expected={"context_handle": "ctx-1"})
    assert summary["authorization_digest"] == payload["authorization_digest"]
    assert summary["issuer"] == "simplicio-loop"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"issuer": "llm"}, "LLM_CANNOT_AUTHORIZE"),
        ({"expires_at": 99.0}, "AUTHORIZATION_WINDOW_INVALID"),
        ({"authorization_digest": "0" * 64}, "AUTHORIZATION_DIGEST_INVALID"),
        ({"unexpected": "model says allow"}, "AUTHORIZATION_FIELDS_INVALID"),
    ],
)
def test_boundary_rejects_adversarial_or_tampered_authorization(change, code):
    payload = _authorization(**change)
    if "authorization_digest" not in change:
        payload["authorization_digest"] = canonical_hash(payload)
    with pytest.raises(AuthorityBoundaryError) as error:
        validate_effect_authorization(payload, now=150.0)
    assert error.value.code == code


def test_mapper_payload_cannot_supply_authorization(tmp_path):
    args, summary = prepare_authorization_handoff(tmp_path)
    assert list(args) == []
    assert summary["status"] == "not_provided"
    mapper_payload = {"handoff": {"effect_authorization": _authorization()}}
    (tmp_path / "mapper-context.json").write_text(json.dumps(mapper_payload), encoding="utf-8")
    args, summary = prepare_authorization_handoff(tmp_path)
    assert list(args) == []
    assert summary["status"] == "not_provided"


def test_only_coordinator_artifact_is_forwarded_and_is_redacted(tmp_path):
    path = tmp_path / AUTHORIZATION_FILENAME
    path.write_text(json.dumps(_authorization()), encoding="utf-8")
    args, summary = prepare_authorization_handoff(tmp_path, required=True, now=150.0)
    assert args == ["--effect-authorization", str(path.resolve())]
    assert summary["status"] == "propagated"
    assert "proposal_digest" not in summary
    assert "human_gate_receipt" not in summary


def test_missing_required_artifact_fails_closed(tmp_path):
    with pytest.raises(AuthorityBoundaryError) as error:
        prepare_authorization_handoff(tmp_path, required=True)
    assert error.value.code == "EFFECT_AUTHORIZATION_REQUIRED"


def test_symlinked_authorization_cannot_escape_run_root(tmp_path):
    outside = tmp_path.parent / "authority-outside.json"
    outside.write_text(json.dumps(_authorization()), encoding="utf-8")
    (tmp_path / AUTHORIZATION_FILENAME).symlink_to(outside)
    with pytest.raises(AuthorityBoundaryError) as error:
        prepare_authorization_handoff(tmp_path, required=True)
    assert error.value.code == "AUTHORIZATION_PATH_INVALID"
