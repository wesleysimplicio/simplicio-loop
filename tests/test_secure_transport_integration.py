"""Connect-time enforcement tests for the #289 trust-policy transport hardening.

`scripts/distributed_trust_policy.check_endpoint()` was unit-tested (#320) but was
never invoked by the code path that actually opens a socket -- these tests prove
`simplicio_loop.secure_transport` closes that gap: DNS/IP hardening is exercised
without a real network via a mocked resolver, and the TLS-pin enforcement is
exercised against a real local HTTPS server and a real socket connection so the
measured certificate fingerprint is genuine, not injected.
"""
import datetime
import hashlib
import ssl
import threading

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

from simplicio_loop.remote_queue import (
    HTTPRemoteQueue,
    QueueUnavailable,
    SQLiteRemoteQueue,
    create_http_queue_server,
)
from simplicio_loop.secure_transport import (
    SecureTransportError,
    TrustedEndpoint,
    resolve_pinned_address,
)
from scripts.distributed_trust_policy import check_endpoint


# ---------------------------------------------------------------------------
# DNS/IP hardening (mocked resolver -- no real network required)
# ---------------------------------------------------------------------------

def _fake_getaddrinfo(address):
    def _impl(host, port, proto=None):
        return [(None, None, None, "", (address, port))]
    return _impl


@pytest.mark.parametrize("address", [
    "127.0.0.1",       # loopback
    "169.254.1.1",      # link-local
    "169.254.169.254",  # cloud metadata
    "10.0.0.5",          # RFC1918 private
    "192.168.1.1",       # RFC1918 private
    "224.0.0.1",          # multicast
    "0.0.0.0",             # unspecified
])
def test_resolve_pinned_address_blocks_disallowed_targets(monkeypatch, address):
    monkeypatch.setattr("socket.getaddrinfo", _fake_getaddrinfo(address))
    with pytest.raises(SecureTransportError):
        resolve_pinned_address("attacker-controlled.example", 443)


def test_resolve_pinned_address_allows_a_public_looking_address(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", _fake_getaddrinfo("8.8.8.8"))
    assert resolve_pinned_address("queue.example.internal", 443) == "8.8.8.8"


def test_resolve_pinned_address_surfaces_dns_failure(monkeypatch):
    def _boom(host, port, proto=None):
        raise OSError("name resolution failed")
    monkeypatch.setattr("socket.getaddrinfo", _boom)
    with pytest.raises(SecureTransportError, match="DNS resolution failed"):
        resolve_pinned_address("nowhere.example", 443)


# ---------------------------------------------------------------------------
# Real TLS handshake + connect-time check_endpoint() enforcement
# ---------------------------------------------------------------------------

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


@pytest.fixture
def https_queue_server(tmp_path):
    cert_path, key_path, fingerprint = _self_signed_cert(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=cert_path, keyfile=key_path)
    backend = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    backend.enqueue("T1")
    server = create_http_queue_server(backend, token="secure-secret", ssl_context=context)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, fingerprint, backend, cert_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _trust_test_ca(monkeypatch, cert_path):
    """Make the client-side handshake trust the test's self-signed leaf as its
    own CA. Real deployments verify against a publicly-trusted CA (#289's chain
    validation requirement); this only substitutes the trust anchor for a
    hermetic test, it does not weaken or skip hostname/chain verification."""
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


def test_check_endpoint_is_invoked_and_passes_for_the_correct_pin(monkeypatch, https_queue_server):
    port, fingerprint, backend, cert_path = https_queue_server
    # The trust-policy hostname is loopback (test infra); the production DNS/IP
    # hardening for that is proven above with a mocked resolver. Here we pin the
    # *resolved address* to loopback so the test exercises the real TLS handshake
    # and the real check_endpoint() call against a genuine measured certificate.
    monkeypatch.setattr(
        "simplicio_loop.secure_transport.resolve_pinned_address",
        lambda hostname, port_: "127.0.0.1",
    )
    _trust_test_ca(monkeypatch, cert_path)
    queue = HTTPRemoteQueue(
        "https://127.0.0.1:%d" % port, token="secure-secret", timeout=5,
        environment_id="test-env", policy=_policy_for(port, [fingerprint]),
    )
    queue.enqueue("T2", {"ok": True})
    task = backend.task("T2")
    assert task["status"] == "ready"


def test_check_endpoint_blocks_before_sending_credentials_on_pin_mismatch(monkeypatch, https_queue_server):
    port, _fingerprint, backend, cert_path = https_queue_server
    monkeypatch.setattr("simplicio_loop.secure_transport.resolve_pinned_address",
                        lambda hostname, port_: "127.0.0.1")
    _trust_test_ca(monkeypatch, cert_path)
    wrong_pin = "00" * 32
    queue = HTTPRemoteQueue(
        "https://127.0.0.1:%d" % port, token="secure-secret", timeout=5,
        environment_id="test-env", policy=_policy_for(port, [wrong_pin]),
    )
    with pytest.raises(QueueUnavailable, match="connect-time trust check failed"):
        queue.enqueue("T-should-not-exist", {})
    with pytest.raises(KeyError):
        backend.task("T-should-not-exist")


def test_trusted_endpoint_check_endpoint_receives_measured_values_not_caller_supplied(monkeypatch, https_queue_server):
    """check_endpoint() must see the *measured* hostname/fingerprint, proving this
    is real connect-time enforcement rather than a pass-through of caller input."""
    port, fingerprint, _backend, cert_path = https_queue_server
    monkeypatch.setattr("simplicio_loop.secure_transport.resolve_pinned_address",
                        lambda hostname, port_: "127.0.0.1")
    _trust_test_ca(monkeypatch, cert_path)

    seen = {}
    def spy_check_endpoint(policy, environment_id, url, hostname, tls_sha256):
        seen["hostname"] = hostname
        seen["tls_sha256"] = tls_sha256
        return check_endpoint(policy, environment_id, url, hostname, tls_sha256)

    endpoint = TrustedEndpoint(
        environment_id="test-env", policy=_policy_for(port, [fingerprint]), check_endpoint=spy_check_endpoint,
    )
    queue = HTTPRemoteQueue("https://127.0.0.1:%d" % port, token="secure-secret", timeout=5)
    queue._trusted_endpoint = endpoint
    queue.enqueue("T3", {})

    assert seen["hostname"] == "127.0.0.1"
    assert seen["tls_sha256"] == fingerprint
