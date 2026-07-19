"""Live negative / fault-injection proofs for the #289 connect-time hardening.

`tests/test_secure_transport.py` unit-tests the policy functions and proves
`check_endpoint()` is wired to a real TLS handshake. This file goes one step
further and reproduces the specific attack scenarios #289 calls out as
mandatory negative tests: a live redirect to an unauthorized origin, a
DNS-rebinding attempt (a second, different DNS answer appearing between
validation and connect), and a proxy-injection attempt via `HTTPS_PROXY`.
Each proves the hardening rejects the attack against a real socket/local
server, not just a mocked policy function.
"""
import datetime
import hashlib
import http.server
import json
import ssl
import threading
from unittest import mock

import pytest

# `cryptography` is not a declared project/dev dependency (pyproject.toml has no `cryptography`
# entry anywhere) -- it is only ever present because a host happens to have it installed
# system-wide. Without this guard, a plain `pip install -e ".[dev]"` + `pytest tests/` hits a
# hard import failure at COLLECTION time, which pytest treats as fatal and aborts the entire run
# ("Interrupted: N errors during collection") -- silently skipping every other test file in the
# suite, not just this one. A bare `pytest.importorskip("cryptography")` is not enough: on a host
# where the top-level package imports but its native `x509`/hazmat rust bindings are broken (e.g.
# a system `cryptography` install missing `_cffi_backend`), the failure surfaces as
# `pyo3_runtime.PanicException` -- a direct `BaseException` subclass, NOT `Exception` -- which
# neither `importorskip` nor a plain `except Exception:` catches. Skip on ANY import-time
# exception (including that one) instead, so a missing OR broken optional dependency degrades to
# a clean per-module skip rather than taking the whole local gate (`scripts/check.py`) down.
try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
except BaseException as _cryptography_import_error:  # noqa: E722 -- see comment above
    pytest.skip(
        "cryptography unavailable/broken in this environment: %r" % (_cryptography_import_error,),
        allow_module_level=True,
    )

from simplicio_loop.secure_transport import (
    SecureTransportError,
    TrustedEndpoint,
    request_json,
    resolve_pinned_address,
)
from scripts.distributed_trust_policy import check_endpoint


def _self_signed_cert(tmp_path, common_name="127.0.0.1"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(__import__("ipaddress").ip_address(common_name))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    der = cert.public_bytes(serialization.Encoding.DER)
    fingerprint = hashlib.sha256(der).hexdigest()
    return str(cert_path), str(key_path), fingerprint


def _trust_test_ca(monkeypatch, cert_path):
    trusted_context = ssl.create_default_context(cafile=cert_path)
    import simplicio_loop.secure_transport as secure_transport_mod
    monkeypatch.setattr(secure_transport_mod.ssl, "create_default_context", lambda: trusted_context)


def _policy_for(port, pins):
    return {
        "environments": {
            "test-env": {
                "origin": {"scheme": "https", "hostname": "127.0.0.1", "port": port, "base_path": "/"},
                "tls_sha256_pins": pins,
            }
        }
    }


# ---------------------------------------------------------------------------
# 1. Live redirect to an unauthorized origin
# ---------------------------------------------------------------------------

def _redirect_handler_factory(redirect_target):
    class _RedirectHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self.send_response(302)
            self.send_header("Location", redirect_target)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return _RedirectHandler


@pytest.fixture
def redirecting_https_server(tmp_path):
    cert_path, key_path, fingerprint = _self_signed_cert(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=cert_path, keyfile=key_path)
    server = http.server.HTTPServer(
        ("127.0.0.1", 0), _redirect_handler_factory("https://attacker.example/steal-token"),
    )
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, fingerprint, cert_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_redirect_to_unauthorized_origin_is_rejected_before_reuse(monkeypatch, redirecting_https_server):
    """A malicious/compromised endpoint answering with a 3xx pointing at an
    attacker-controlled origin must never be followed -- #289's fail-closed
    default is zero redirects, proven here against a real HTTP response, not
    a mocked one."""
    port, fingerprint, cert_path = redirecting_https_server
    monkeypatch.setattr(
        "simplicio_loop.secure_transport.resolve_pinned_address",
        lambda hostname, port_: "127.0.0.1",
    )
    _trust_test_ca(monkeypatch, cert_path)
    endpoint = TrustedEndpoint(
        environment_id="test-env", policy=_policy_for(port, [fingerprint]), check_endpoint=check_endpoint,
    )
    with pytest.raises(SecureTransportError, match="redirect"):
        request_json(
            "POST", "https://127.0.0.1:%d/v1/queue/enqueue" % port,
            body=json.dumps({"task_id": "T1"}).encode("utf-8"),
            headers={"Authorization": "Bearer super-secret-token"},
            timeout=5, endpoint=endpoint,
        )


# ---------------------------------------------------------------------------
# 2. DNS rebinding: a second, different DNS answer must never substitute for
#    the address that was actually validated.
# ---------------------------------------------------------------------------

def test_dns_rebinding_cannot_swap_the_connect_target_after_validation(monkeypatch):
    """Simulates an attacker-controlled DNS server that answers with a safe,
    publicly-routable address on the first (validated) lookup and then
    "rebinds" to an internal/attacker address on a hypothetical second
    lookup. `resolve_pinned_address` performs exactly one lookup and the
    connection is opened to *that* resolved address only -- there is no
    second `getaddrinfo` call for an attacker to win a race against.
    """
    call_count = {"n": 0}
    addresses = ["8.8.8.8", "169.254.169.254"]  # 2nd answer is the cloud metadata service

    def _rebinding_getaddrinfo(host, port, proto=None):
        idx = min(call_count["n"], len(addresses) - 1)
        call_count["n"] += 1
        return [(None, None, None, "", (addresses[idx], port))]

    monkeypatch.setattr("socket.getaddrinfo", _rebinding_getaddrinfo)

    resolved = resolve_pinned_address("attacker-controlled.example", 443)

    assert resolved == "8.8.8.8"
    assert call_count["n"] == 1  # exactly one DNS lookup was performed

    with mock.patch("socket.create_connection") as fake_connect:
        fake_connect.side_effect = OSError("no real network in this test")
        import simplicio_loop.secure_transport as secure_transport_mod
        conn = secure_transport_mod._PinnedHTTPSConnection(
            "attacker-controlled.example", resolved, 443, timeout=1,
        )
        with pytest.raises(OSError):
            conn.connect()
        # The connection target is the single, already-validated address --
        # never a fresh (potentially rebound) lookup performed at connect time.
        called_address = fake_connect.call_args[0][0][0]
        assert called_address == "8.8.8.8"
        assert called_address != "169.254.169.254"


# ---------------------------------------------------------------------------
# 3. Proxy injection via HTTPS_PROXY/HTTP_PROXY/ALL_PROXY
# ---------------------------------------------------------------------------

@pytest.fixture
def https_queue_server_for_proxy_test(tmp_path):
    from simplicio_loop.remote_queue import SQLiteRemoteQueue, create_http_queue_server

    cert_path, key_path, fingerprint = _self_signed_cert(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=cert_path, keyfile=key_path)
    backend = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    server = create_http_queue_server(backend, token="secure-secret", ssl_context=context)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, fingerprint, backend, cert_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_proxy_env_vars_are_ignored_even_when_pointed_at_an_unreachable_proxy(
    monkeypatch, https_queue_server_for_proxy_test,
):
    """A compromised/malicious environment sets HTTPS_PROXY (and friends) to
    redirect traffic through an attacker-controlled man-in-the-middle proxy.
    `secure_transport` opens its own socket instead of using urllib's
    environment-aware proxy handling, so the request must succeed by
    connecting directly to the trust-policy origin even though the proxy
    variables point at an address that would refuse the connection if it
    were actually used.
    """
    port, fingerprint, backend, cert_path = https_queue_server_for_proxy_test
    monkeypatch.setattr(
        "simplicio_loop.secure_transport.resolve_pinned_address",
        lambda hostname, port_: "127.0.0.1",
    )
    _trust_test_ca(monkeypatch, cert_path)
    # Port 9 ("discard") is reserved/unassigned and not listening -- if this
    # module ever honored proxy env vars, the request below would fail to
    # connect (or hang) rather than reach the real queue server directly.
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9/")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9/")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9/")

    endpoint = TrustedEndpoint(
        environment_id="test-env", policy=_policy_for(port, [fingerprint]), check_endpoint=check_endpoint,
    )
    result = request_json(
        "POST", "https://127.0.0.1:%d/v1/queue/enqueue" % port,
        body=json.dumps({"task_id": "T-proxy-bypass"}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer secure-secret"},
        timeout=5, endpoint=endpoint,
    )
    assert result["_status"] == 200
    assert backend.task("T-proxy-bypass")["status"] == "ready"
