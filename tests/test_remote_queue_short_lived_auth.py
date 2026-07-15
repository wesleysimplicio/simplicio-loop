"""Server-side short-lived-credential auth mode for the distributed queue (#289).

Proves the queue server's HTTP facade can require a signed, expiring, revocable
token (:mod:`scripts.short_lived_credentials`) instead of an indefinitely-lived
static bearer secret, over a real HTTP request/response round trip.
"""
import threading
import time

import pytest

from scripts.short_lived_credentials import issue_token, revoke_jti
from simplicio_loop.remote_queue import SQLiteRemoteQueue, create_http_queue_server
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import json


@pytest.fixture
def short_lived_queue_server(tmp_path):
    backend = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    server = create_http_queue_server(
        backend, token_secret="signing-secret", token_scope="staging",
        revocation_store=tmp_path / "revoked.json",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_port, backend, tmp_path / "revoked.json"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _post(url, path, token, body):
    req = Request(url + "/v1/queue" + path, data=json.dumps(body).encode("utf-8"),
                  headers={"Content-Type": "application/json",
                           **({"Authorization": "Bearer " + token} if token else {})},
                  method="POST")
    with urlopen(req, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_valid_short_lived_token_is_accepted(short_lived_queue_server):
    url, backend, _store = short_lived_queue_server
    token = issue_token("signing-secret", subject="agent-1", scope="staging", ttl_seconds=60)
    status, result = _post(url, "/enqueue", token, {"task_id": "T1", "payload": {}})
    assert status == 200
    assert backend.task("T1")["status"] == "ready"


def test_wrong_scope_token_is_rejected(short_lived_queue_server):
    url, _backend, _store = short_lived_queue_server
    token = issue_token("signing-secret", subject="agent-1", scope="production", ttl_seconds=60)
    with pytest.raises(HTTPError) as exc_info:
        _post(url, "/enqueue", token, {"task_id": "T-should-not-exist", "payload": {}})
    assert exc_info.value.code == 401


def test_expired_token_is_rejected(short_lived_queue_server):
    # Construct a token whose `exp` is already well past verify_token's default
    # clock-skew allowance, using the module's own signing primitives -- the
    # same shape `issue_token` produces, just dated in the past.
    from scripts.short_lived_credentials import TOKEN_SCHEMA, _b64u_encode, _sign
    import json as _json
    url, _backend, _store = short_lived_queue_server
    now = time.time()
    claims = {"schema": TOKEN_SCHEMA, "sub": "agent-1", "scope": "staging", "aud": None,
              "jti": "expired-jti", "iat": now - 3600, "nbf": now - 3600, "exp": now - 60}
    payload_b64 = _b64u_encode(_json.dumps(claims, sort_keys=True).encode("utf-8"))
    token = payload_b64 + "." + _b64u_encode(_sign("signing-secret", payload_b64.encode("ascii")))
    with pytest.raises(HTTPError) as exc_info:
        _post(url, "/enqueue", token, {"task_id": "T-expired", "payload": {}})
    assert exc_info.value.code == 401


def test_static_token_is_rejected_in_short_lived_mode(short_lived_queue_server):
    url, _backend, _store = short_lived_queue_server
    with pytest.raises(HTTPError) as exc_info:
        _post(url, "/enqueue", "some-static-secret", {"task_id": "T-static", "payload": {}})
    assert exc_info.value.code == 401


def test_revoked_token_is_rejected_immediately(short_lived_queue_server):
    url, backend, store = short_lived_queue_server
    token = issue_token("signing-secret", subject="agent-1", scope="staging", ttl_seconds=300)
    status, _ = _post(url, "/enqueue", token, {"task_id": "T-ok", "payload": {}})
    assert status == 200

    from scripts.short_lived_credentials import verify_token
    claims = verify_token("signing-secret", token, expected_scope="staging")
    revoke_jti(store, claims["jti"])

    with pytest.raises(HTTPError) as exc_info:
        _post(url, "/enqueue", token, {"task_id": "T-after-revoke", "payload": {}})
    assert exc_info.value.code == 401
    with pytest.raises(KeyError):
        backend.task("T-after-revoke")


def test_token_and_token_secret_are_mutually_exclusive(tmp_path):
    backend = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    with pytest.raises(ValueError, match="mutually exclusive"):
        create_http_queue_server(backend, token="static", token_secret="secret")
