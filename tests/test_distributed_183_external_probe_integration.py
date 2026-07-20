from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import distributed_183_external_probe as probe  # noqa: E402


def _valid_payload() -> str:
    return (
        '{"run_id":"issue-183-ac6","lanes":['
        '{"lane_id":"lane-codex","runtime":"codex","agent_id":"agent-codex-a"},'
        '{"lane_id":"lane-claude","runtime":"claude","agent_id":"agent-claude-b"}'
        "]}"
    )


def _valid_env() -> dict[str, str]:
    return {
        probe.ENV_URL: "https://queue.example.internal",
        probe.ENV_TOKEN: "super-secret-token",
        probe.ENV_TLS_HOSTNAME: "queue.example.internal",
        probe.ENV_TLS_FINGERPRINT: "ab" * 32,
        probe.ENV_PAYLOAD: _valid_payload(),
    }


@pytest.mark.parametrize("missing_key", list(probe.REQUIRED_ENV))
def test_missing_env_matrix_is_fail_closed(missing_key):
    env = _valid_env()
    env.pop(missing_key)

    receipt = probe.run(env=env)

    assert receipt["status"] == "UNVERIFIED"
    assert receipt["fail_closed"] is True
    assert receipt["checks"]["real_endpoint_probe"] is False
    assert missing_key in receipt["missing_env"]
    assert receipt["claim_boundary"].startswith("UNVERIFIED|")


def test_valid_payload_and_live_probe_require_https_hostname_fingerprint_token_and_two_lanes(monkeypatch):
    env = _valid_env()

    def fake_tls(queue_url: str, expected_hostname: str, timeout: float = 5.0):
        assert queue_url == env[probe.ENV_URL]
        assert expected_hostname == env[probe.ENV_TLS_HOSTNAME]
        assert timeout == 7.5
        return {
            "hostname": expected_hostname,
            "port": 443,
            "fingerprint_sha256": env[probe.ENV_TLS_FINGERPRINT],
            "tls_version": "TLSv1.3",
            "cipher": "TLS_AES_256_GCM_SHA384",
        }

    def fake_events(queue_url: str, token: str, timeout: float = 5.0):
        assert queue_url == env[probe.ENV_URL]
        assert token == env[probe.ENV_TOKEN]
        assert timeout == 7.5
        return {"http_status": 200, "event_count": 0}

    monkeypatch.setattr(probe, "inspect_tls_endpoint", fake_tls)
    monkeypatch.setattr(probe, "probe_queue_events", fake_events)

    receipt = probe.run(env=env, execute_real=True, timeout=7.5)

    assert receipt["status"] == "VERIFIED"
    assert all(receipt["checks"].values()), receipt["checks"]
    assert receipt["missing_env"] == []
    assert receipt["payload_summary"] == {
        "lane_count": 2,
        "runtimes": ["codex", "claude"],
        "lane_ids": ["lane-codex", "lane-claude"],
    }
    assert receipt["tls"]["fingerprint_sha256"] == env[probe.ENV_TLS_FINGERPRINT]
    assert receipt["probe"] == {"http_status": 200, "event_count": 0}
    assert receipt["epic_closure_ready"] is False
    assert receipt["config"]["token_sha256_prefix"]
