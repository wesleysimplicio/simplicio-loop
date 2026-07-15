"""Connect-time transport hardening for the #289 distributed-queue trust policy.

Gap this closes: ``scripts/distributed_trust_policy.check_endpoint()`` existed as a
standalone, unit-tested function but was never called by the code path that
actually opens a socket (``simplicio_loop.remote_queue.HTTPRemoteQueue``), so a
mismatched hostname/pin would never actually stop a real request. This module is
the connect-time enforcement point: it resolves DNS itself, refuses to hand a
request to any disallowed address, performs the TLS handshake itself so it can
measure the certificate the peer *actually* presented, and only then calls
``check_endpoint()`` with the measured origin/hostname/fingerprint -- never a
value supplied by the caller -- before the request (and therefore any bearer
token in its headers) is written to the socket.

Specifically, for every request through :func:`request_json` when a
``TrustedEndpoint`` is supplied:

* the URL is parsed and rejected if it is not ``https``, carries userinfo, or
  a fragment;
* DNS is resolved exactly once via :func:`socket.getaddrinfo`; the connection
  is opened to that resolved address only, so a second (rebound) DNS answer
  can never be substituted between validation and connection (the TOCTOU
  window #289 calls out);
* the resolved address is rejected if it is loopback, link-local, private,
  multicast, reserved, unspecified, or a known cloud metadata address, unless
  the trust policy's environment explicitly allows private ranges;
* the TLS handshake is performed with the standard library's certificate
  validation (hostname + chain) still enabled -- pinning is defense in depth,
  not a replacement for chain validation;
* the leaf certificate actually presented is hashed (SHA-256 of the DER
  encoding) and checked, via ``check_endpoint()``, against the trust policy's
  pins for the resolved ``environment_id`` -- a mismatch closes the socket and
  raises before any data is sent;
* no HTTP redirect is ever followed (#289's fail-closed default is zero
  redirects) and ``HTTP_PROXY``/``HTTPS_PROXY``/``ALL_PROXY``/``.netrc`` are
  never consulted, because this module opens its own socket instead of
  routing through ``urllib``'s environment-aware proxy handling.

This does not (yet) implement rotation with a `current + next` pin set, nor an
allow-list of private IP ranges for self-hosted runners; the policy schema
does not currently declare either, and both are out of scope for the shared
``HTTPRemoteQueue`` client. Extending the policy schema to add them is
straightforward follow-up once there's a concrete self-hosted deployment to
validate against.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import socket
import ssl
from dataclasses import dataclass
from typing import Any, Dict, Mapping
from urllib.parse import urlsplit

METADATA_ADDRESSES = {
    "169.254.169.254",  # AWS/GCP/Azure instance metadata
    "fd00:ec2::254",    # AWS IMDSv2 IPv6
    "100.100.100.200",  # Alibaba Cloud metadata
}


class SecureTransportError(RuntimeError):
    """Raised for any DNS/TLS/pin/redirect violation. Callers must fail closed."""


@dataclass(frozen=True)
class TrustedEndpoint:
    """Binds a request to a specific trust-policy environment for enforcement."""

    environment_id: str
    policy: Mapping[str, Any]
    check_endpoint: Any  # Callable[[policy, environment_id, url, hostname, sha256], (bool, str)]


def _is_disallowed_ip(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    return bool(
        ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
    )


def resolve_pinned_address(hostname: str, port: int) -> str:
    """Resolve ``hostname`` exactly once and reject disallowed target addresses.

    Returns the numeric address to connect to. The hostname is still used for
    TLS SNI/hostname verification -- only the *connection* target is pinned to
    this single resolution, closing the DNS-rebinding TOCTOU window where a
    second lookup at connect time could answer differently than the one that
    was validated.
    """
    try:
        infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise SecureTransportError(f"DNS resolution failed for '{hostname}': {exc}") from exc
    if not infos:
        raise SecureTransportError(f"DNS resolution returned no addresses for '{hostname}'")
    _family, _socktype, _proto, _canonname, sockaddr = infos[0]
    address = str(sockaddr[0])
    if address in METADATA_ADDRESSES:
        raise SecureTransportError(f"refusing to connect to metadata service address '{address}'")
    try:
        ip = ipaddress.ip_address(address)
    except ValueError as exc:
        raise SecureTransportError(f"unparsable resolved address '{address}'") from exc
    if _is_disallowed_ip(ip):
        raise SecureTransportError(
            f"refusing to connect to disallowed address '{address}' for host '{hostname}'"
        )
    return address


def _leaf_cert_sha256(sock: ssl.SSLSocket) -> str:
    der = sock.getpeercert(binary_form=True)
    if not der:
        raise SecureTransportError("TLS handshake did not yield a peer certificate")
    return hashlib.sha256(der).hexdigest()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that connects to a pre-resolved address, not a fresh lookup."""

    def __init__(self, hostname: str, pinned_address: str, port: int, *, timeout: float) -> None:
        super().__init__(hostname, port, timeout=timeout)
        self._pinned_address = pinned_address

    def connect(self) -> None:  # noqa: D102 - stdlib override
        # Intentionally ignore `self._context` (which `HTTPSConnection.__init__`
        # already populated via `ssl._create_default_https_context()`) and build
        # the verifying context here, at handshake time, so hostname + chain
        # validation always runs against the *current* system trust store.
        sock = socket.create_connection((self._pinned_address, self.port), timeout=self.timeout)
        context = ssl.create_default_context()
        self.sock = context.wrap_socket(sock, server_hostname=self.host)


def request_json(
    method: str,
    url: str,
    *,
    body: bytes,
    headers: Mapping[str, str],
    timeout: float,
    endpoint: TrustedEndpoint,
) -> Dict[str, Any]:
    """Perform one HTTPS request with DNS/TLS/pin enforcement, never following redirects.

    Raises :class:`SecureTransportError` before anything is written to the
    socket if the resolved address, certificate, or pin does not match the
    trust policy for ``endpoint.environment_id``. Non-2xx responses are
    surfaced to the caller as a plain dict with a ``_status`` key rather than
    silently retried or redirected.
    """
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise SecureTransportError("secure transport requires an https URL")
    if not parsed.hostname:
        raise SecureTransportError("URL must include a hostname")
    if parsed.username or parsed.password:
        raise SecureTransportError("URL must not carry userinfo")
    if parsed.fragment:
        raise SecureTransportError("URL must not carry a fragment")
    hostname = parsed.hostname
    port = parsed.port or 443
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    pinned_address = resolve_pinned_address(hostname, port)
    conn = _PinnedHTTPSConnection(hostname, pinned_address, port, timeout=timeout)
    try:
        conn.connect()
        measured_sha256 = _leaf_cert_sha256(conn.sock)  # type: ignore[arg-type]
        ok, reason = endpoint.check_endpoint(
            endpoint.policy, endpoint.environment_id, url, hostname, measured_sha256,
        )
        if not ok:
            raise SecureTransportError(f"connect-time trust check failed: {reason}")

        conn.request(method, path, body=body, headers=dict(headers))
        response = conn.getresponse()
        raw = response.read()
        status = response.status
    finally:
        conn.close()

    if status in (301, 302, 303, 307, 308):
        raise SecureTransportError(
            f"refusing to follow redirect (status {status}); zero redirects is the fail-closed default"
        )

    try:
        decoded = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecureTransportError(f"non-JSON response body: {exc}") from exc
    if not isinstance(decoded, dict):
        raise SecureTransportError("response body must be a JSON object")
    decoded["_status"] = status
    return decoded
