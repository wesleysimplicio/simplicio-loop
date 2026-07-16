"""Portable stage-agent display identity (EPIC #422, issue #434).

Separates the *human, auditable display name* an agent is reported under
(lifecycle events, GitHub comments, review tables, receipts, logs, status CLI)
from the *technical identity* used for uniqueness and authorization
(``agent_instance_id``, ``run_id``, ``task_id``, ``attempt_id``, ``fence``,
host fingerprint, observed ``provider``/``model``).

Required display format::

    <Name> <Role> - #<HOST4> - <LLM>

e.g. ``Alex Review - #PC1 - Claude``.

``HOST4`` is derived deterministically from the system user (never the raw
hostname alone, though hostname is an acceptable input when no username is
available), normalized to ASCII alphanumeric uppercase, capped at 4 chars. If
no usable identity source exists, an explicit fallback token is used and the
reason is reported via ``host_identity_fallback`` in the receipt-ready dict —
never a silently invented identifier.

The display name CAN collide across machines/LLMs by design — uniqueness is
guaranteed separately by the technical identity fields, never by the display
name. See ``resolve_agent_identity`` for the receipt-ready dict that keeps
both together without letting the display name substitute for identity.
"""
from __future__ import annotations

import os
import re
import unicodedata
from typing import Any, Mapping

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

HOST4_LEN = 4
# Explicit, observable fallback token — never a silently invented identifier.
HOST4_FALLBACK = "HOST"
HOST4_FALLBACK_REASON = "host_identity_unavailable"

_ASCII_ALNUM_RE = re.compile(r"[^A-Za-z0-9]+")
# Characters sanitized out of any field before it is embedded in a display
# name, Markdown, HTML or a log line (requirement 6: sanitize before render).
_MARKDOWN_HTML_RE = re.compile(r"[`*_\[\]()<>#|\\\r\n]")


# --------------------------------------------------------------------------- #
# 1. Pure HOST4 normalization
# --------------------------------------------------------------------------- #
def derive_host4(raw_user: str | None = None) -> str:
    """Pure function: normalize a system user (or hostname) into HOST4.

    Deterministic, case-insensitive (folded to uppercase), ASCII-alphanumeric
    only, exactly ``HOST4_LEN`` characters (right-padded is never done —
    inputs shorter than 4 chars are returned as-is, at their natural length,
    per requirement: "limitado a 4 caracteres" i.e. capped, not padded).

    Never raises and never invents an identity: if ``raw_user`` is ``None``,
    empty, or normalizes to nothing (e.g. only symbols/emoji), the explicit
    ``HOST4_FALLBACK`` token is returned. Callers that need the observable
    reason code should use :func:`resolve_host4` instead.
    """
    host4, _reason = resolve_host4(raw_user)
    return host4


def resolve_host4(raw_user: str | None = None) -> tuple[str, str | None]:
    """Like :func:`derive_host4` but also returns a reason code.

    Returns ``(host4, reason_code)``. ``reason_code`` is ``None`` when a real
    identity was derived, or a stable string (e.g.
    ``"host_identity_unavailable"``) when the fallback was used — this is the
    value surfaced as ``host_identity_fallback`` in receipts.
    """
    if raw_user is None:
        return HOST4_FALLBACK, HOST4_FALLBACK_REASON

    if not isinstance(raw_user, str):
        raw_user = str(raw_user)

    # NFKD-fold unicode to ASCII where possible (e.g. "José" -> "Jose",
    # full-width digits, etc.) before stripping non-alphanumerics.
    folded = unicodedata.normalize("NFKD", raw_user)
    ascii_only = folded.encode("ascii", "ignore").decode("ascii")
    cleaned = _ASCII_ALNUM_RE.sub("", ascii_only).upper()

    if not cleaned:
        return HOST4_FALLBACK, HOST4_FALLBACK_REASON

    return cleaned[:HOST4_LEN], None


def system_user_candidates() -> list[str]:
    """Cross-platform ordered list of candidate raw identity sources.

    Tries the conventional env vars first (``USER`` on POSIX, ``USERNAME`` on
    Windows), then falls back to the hostname, then ``os.getlogin()``. Pure
    with respect to its *output shape*, but reads the environment — kept
    separate from :func:`derive_host4` so that function stays a pure,
    unit-testable transform and this one owns the (impure) discovery.
    """
    candidates: list[str] = []
    for var in ("USER", "USERNAME", "LOGNAME"):
        val = os.environ.get(var)
        if val:
            candidates.append(val)
    try:
        host = os.environ.get("HOSTNAME") or os.environ.get("COMPUTERNAME")
        if not host:
            import socket
            host = socket.gethostname()
        if host:
            candidates.append(host)
    except Exception:
        pass
    try:
        login = os.getlogin()
        if login:
            candidates.append(login)
    except Exception:
        pass
    return candidates


def derive_host4_for_this_host() -> tuple[str, str | None]:
    """Discover HOST4 for the current process's host, with reason code.

    Cross-platform fallback (requirement 2): tries the candidate sources in
    order; the first one that normalizes to a non-empty HOST4 wins. If none
    of ``USER``/``USERNAME``/``LOGNAME``/hostname/``os.getlogin()`` are
    available or all normalize to nothing, returns the explicit fallback
    token and reason code — never a silently invented identifier.
    """
    for candidate in system_user_candidates():
        host4, reason = resolve_host4(candidate)
        if reason is None:
            return host4, None
    return HOST4_FALLBACK, HOST4_FALLBACK_REASON


# --------------------------------------------------------------------------- #
# Sanitization (requirement 6): defend Markdown/HTML/log injection
# --------------------------------------------------------------------------- #
def sanitize_field(value: Any, max_len: int = 40) -> str:
    """Sanitize a free-form field (name, role, host user, provider, model)
    before it is embedded in a display name, Markdown/HTML surface or log
    line. Strips Markdown/HTML control characters and collapses whitespace;
    truncates to ``max_len`` to keep display names short and auditable.
    """
    text = "" if value is None else str(value)
    text = _MARKDOWN_HTML_RE.sub("", text)
    text = " ".join(text.split())
    return text[:max_len]


# --------------------------------------------------------------------------- #
# 2. Display name builder
# --------------------------------------------------------------------------- #
def format_display_name(name: str, role: str, host4: str, llm: str) -> str:
    """Build the required display-name format: ``<Name> <Role> - #<HOST4> - <LLM>``.

    All parts are sanitized independently so neither the human-picked
    ``name``/``role`` nor the observed ``llm`` string can inject
    Markdown/HTML into lifecycle events, GitHub comments, review tables or
    logs. This function never mutates or validates technical identity — it
    is purely a rendering step for the human-readable label.
    """
    safe_name = sanitize_field(name) or "Agent"
    safe_role = sanitize_field(role) or "Role"
    safe_host4 = sanitize_field(host4, max_len=HOST4_LEN) or HOST4_FALLBACK
    safe_llm = sanitize_field(llm) or "unknown"
    return f"{safe_name} {safe_role} - #{safe_host4} - {safe_llm}"


# --------------------------------------------------------------------------- #
# 3. Full identity resolution (display name + receipt-ready technical fields)
# --------------------------------------------------------------------------- #
def resolve_agent_identity(
    *,
    name: str,
    role: str,
    llm: str,
    agent_instance_id: str,
    raw_user: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    runtime: str | None = None,
    host_id: str | None = None,
    run_id: str | None = None,
    task_id: str | None = None,
    attempt_id: str | None = None,
    fence: str | None = None,
) -> dict[str, Any]:
    """Resolve the human display name AND a receipt-ready technical dict.

    Returns a mapping with:

    * ``display_name`` — the rendered ``<Name> <Role> - #<HOST4> - <LLM>``
      string, safe to embed in lifecycle events / GitHub comments / review
      tables / logs / status CLI output (requirement 4).
    * ``host4`` — the derived (or fallback) HOST4 token.
    * ``host_identity_fallback`` — ``None`` when a real host identity was
      derived, otherwise the observable reason code (requirement 2). Only
      present with a non-null value when the fallback path was actually
      taken — never fabricated.
    * The full technical identity block (``agent_instance_id``, ``provider``,
      ``model``, ``runtime``, ``host_user``, ``host_id``, ``run_id``,
      ``task_id``, ``attempt_id``, ``fence``) kept fully separate from the
      display name (requirement 3) so uniqueness/authorization never depends
      on the display string (requirement 7). Any field left unset by the
      caller is reported as ``"legacy-unbound"`` for compatibility with
      older receipts/comments (requirement 8) rather than silently omitted.
    """
    if raw_user is not None:
        host4, fallback_reason = resolve_host4(raw_user)
    else:
        host4, fallback_reason = derive_host4_for_this_host()

    display_name = format_display_name(name, role, host4, llm)

    def _legacy(value: str | None) -> str:
        v = "" if value is None else str(value).strip()
        return v if v else "legacy-unbound"

    return {
        "display_name": display_name,
        "host4": host4,
        "host_identity_fallback": fallback_reason,
        # Technical identity — never used as a uniqueness/authorization key.
        "agent_instance_id": _legacy(agent_instance_id),
        "provider": _legacy(provider),
        "model": _legacy(model),
        "runtime": _legacy(runtime),
        "host_user": _legacy(raw_user),
        "host_id": _legacy(host_id),
        "run_id": _legacy(run_id),
        "task_id": _legacy(task_id),
        "attempt_id": _legacy(attempt_id),
        "fence": _legacy(fence),
    }


__all__ = [
    "HOST4_FALLBACK",
    "HOST4_FALLBACK_REASON",
    "HOST4_LEN",
    "derive_host4",
    "derive_host4_for_this_host",
    "format_display_name",
    "resolve_agent_identity",
    "resolve_host4",
    "sanitize_field",
    "system_user_candidates",
]
