"""``runtime-execution-receipt`` (issue #287): what actually happened when a
routed runtime attempted (or failed) to execute -- distinct from the
``routing-decision-receipt`` in ``simplicio_loop/model_router.py``, which only
records the *decision* (candidates, score, selection), never the outcome.

Builds on ``simplicio_loop/receipt_verifier.py`` (#288) for validation:
:func:`build_runtime_execution_receipt` produces the receipt;
``RUNTIME_EXECUTION_RECEIPT_SCHEMA`` + ``receipt_verifier.verify_receipt()``
validate schema/hash/freshness/provenance on read, exactly like the
operator/evidence receipts this codebase already produces (same discipline,
one more receipt kind).

Fields that cannot be genuinely measured (e.g. a CLI tool that never exposes
which model actually ran, or usage/cost a driver never reports) are recorded as
the literal string ``"UNAVAILABLE"`` -- never fabricated, mirroring
``model_registry.py``'s ``probe()`` and ``scripts/runtime_matrix.py``'s
``external_launch_verified`` discipline.

This module only builds/shapes the receipt; it does not itself execute a
runtime. Real ``CodexRuntimeDriver``/``ClaudeRuntimeDriver`` execution (which
would call this builder with genuinely measured values) remains out of scope
here -- it requires live credentialed runtimes.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, Mapping, Optional, Sequence

from .receipt_verifier import ReceiptSchema

SCHEMA = "simplicio.runtime-execution-receipt/v1"
UNAVAILABLE = "UNAVAILABLE"

STOP_REASONS = frozenset((
    "completed", "timeout", "cancelled", "error", "circuit_open", "budget_exceeded",
))


class RuntimeExecutionReceiptError(ValueError):
    """Raised for malformed receipt-building input."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _text(value: Any) -> str:
    return str(value or "").strip()


def _stable_hash(data: Any) -> str:
    blob = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _model_ref(ref: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if ref is None:
        return {"runtime": UNAVAILABLE, "provider": UNAVAILABLE, "model_id": UNAVAILABLE, "verified": False}
    return {
        "runtime": _text(ref.get("runtime")) or UNAVAILABLE,
        "provider": _text(ref.get("provider")) or UNAVAILABLE,
        "model_id": _text(ref.get("model_id")) or UNAVAILABLE,
        "verified": bool(ref.get("verified", True)),
    }


def build_runtime_execution_receipt(
    *,
    route_id: str,
    requested: Mapping[str, Any],
    resolved: Optional[Mapping[str, Any]],
    driver: Mapping[str, Any],
    session: Mapping[str, Any],
    argv_redacted: Sequence[str],
    env_allowlist: Sequence[str],
    tree: Mapping[str, Any],
    exit_status: Optional[int],
    duration_seconds: Optional[float],
    stop_reason: str,
    stream_hashes: Optional[Mapping[str, str]] = None,
    usage: Optional[Mapping[str, Any]] = None,
    evidence_refs: Optional[Sequence[str]] = None,
    previous_route_id: str = "",
    fallback_reason_code: str = "",
) -> Dict[str, Any]:
    """Build one runtime-execution-receipt (never fabricates unmeasured fields).

    ``route_id``: the id of the routing-decision-receipt (``model_router.route``)
        that selected this execution -- links decision to outcome.
    ``requested`` / ``resolved``: ``{"runtime", "provider", "model_id"}`` -- what
        the router decided to use, and what the driver actually *observed*
        running. These may differ, or ``resolved`` may be ``None`` entirely when
        the tool never exposes the model it used -- recorded as ``UNAVAILABLE``
        with ``verified=False``, never silently assumed to match ``requested``.
    ``driver``: ``{"name", "binary", "version", "identity_verified"}``.
    ``session``: ``{"worker_id", "device_id", "attempt_id", "lease_id",
        "fence_token"}`` -- ties this receipt to the attempt/worktree/fence that
        authorized the mutation.
    ``argv_redacted`` / ``env_allowlist``: the structured argv actually invoked
        (secrets already redacted by the caller) and the environment variable
        allow-list used -- never a shell string built from task data.
    ``tree``: ``{"base_sha", "head_sha", "changed_paths"}``.
    ``usage``: optional ``{"tokens", "cost_usd", "latency_seconds"}`` -- each
        value must be a real measured number or the literal string
        ``"UNAVAILABLE"``; missing keys default to ``"UNAVAILABLE"`` rather than
        ``None`` or ``0`` (which would look like a measured zero-cost run).
    ``previous_route_id`` / ``fallback_reason_code``: set when this execution
        followed a fallback hop (see ``model_router.route_with_fallback``),
        linking the execution receipt back to the routing-decision-receipt
        chain that produced it.
    """
    if not _text(route_id):
        raise RuntimeExecutionReceiptError("route_id is required")
    if stop_reason not in STOP_REASONS:
        raise RuntimeExecutionReceiptError(
            f"stop_reason must be one of {sorted(STOP_REASONS)}, got {stop_reason!r}"
        )
    if duration_seconds is not None and duration_seconds < 0:
        raise RuntimeExecutionReceiptError("duration_seconds must be >= 0 when known")

    usage = dict(usage or {})
    usage_out = {
        "tokens": usage.get("tokens", UNAVAILABLE),
        "cost_usd": usage.get("cost_usd", UNAVAILABLE),
        "latency_seconds": usage.get("latency_seconds", UNAVAILABLE),
    }

    receipt: Dict[str, Any] = {
        "schema": SCHEMA,
        "route_id": _text(route_id),
        "previous_route_id": _text(previous_route_id),
        "fallback_reason_code": _text(fallback_reason_code),
        "requested": _model_ref(requested),
        "resolved": _model_ref(resolved),
        "driver": {
            "name": _text(driver.get("name")) or UNAVAILABLE,
            "binary": _text(driver.get("binary")) or UNAVAILABLE,
            "version": _text(driver.get("version")) or UNAVAILABLE,
            "identity_verified": bool(driver.get("identity_verified", False)),
        },
        "session": {
            "worker_id": _text(session.get("worker_id")),
            "device_id": _text(session.get("device_id")),
            "attempt_id": _text(session.get("attempt_id")),
            "lease_id": _text(session.get("lease_id")),
            "fence_token": _text(session.get("fence_token")),
        },
        "argv_redacted": [str(a) for a in argv_redacted],
        "env_allowlist": sorted({str(e) for e in env_allowlist}),
        "tree": {
            "base_sha": _text(tree.get("base_sha")),
            "head_sha": _text(tree.get("head_sha")),
            "changed_paths": sorted({str(p) for p in (tree.get("changed_paths") or [])}),
        },
        "exit_status": exit_status,
        "duration_seconds": duration_seconds,
        "stop_reason": stop_reason,
        "stream_hashes": dict(stream_hashes or {}),
        "usage": usage_out,
        "evidence_refs": sorted({str(e) for e in (evidence_refs or [])}),
        "measured_at": _now(),
    }
    content_fields = [k for k in receipt if k != "receipt_sha"]
    receipt["receipt_sha"] = _stable_hash({k: receipt[k] for k in content_fields})
    return receipt


RUNTIME_EXECUTION_RECEIPT_SCHEMA = ReceiptSchema(
    name="runtime-execution-receipt",
    required_fields=(
        "schema", "route_id", "requested", "resolved", "driver", "session",
        "exit_status", "stop_reason", "measured_at",
    ),
    required_values={"schema": SCHEMA},
    provenance_fields=("route_id", "session.attempt_id"),
    freshness_field="measured_at",
    hash_field="receipt_sha",
)


__all__ = [
    "SCHEMA",
    "STOP_REASONS",
    "UNAVAILABLE",
    "RUNTIME_EXECUTION_RECEIPT_SCHEMA",
    "RuntimeExecutionReceiptError",
    "build_runtime_execution_receipt",
]
