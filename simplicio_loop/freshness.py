"""TTL/freshness policy for observed delivery evidence (#290 invariant: "Freshness").

Issue #290 requires that "receipts terminais têm TTL por classe" and that "qualquer
observação em cache mais velha que um TTL configurável é tratada como obsoleta e deve
ser reconsultada antes de ser confiada". Prior to this module, `source_checked_at` was
recorded on every delivery receipt (`simplicio_loop/delivery.py`) but nothing ever
compared its age against a policy — a receipt built hours or days ago could still gate a
terminal `merged`/`released`/`deployed` transition as if it had just been observed.

This module is deliberately small and dependency-free (stdlib only) so it can be
imported both by `delivery.py` (receipt-level gate) and `github_lifecycle.py`
(`verify_issue_state`'s cache-vs-live decision) without a cycle.

"Unknown is not pass": a missing, empty, or unparsable timestamp is always treated as
stale — it is never assumed fresh just because the caller forgot to stamp it.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

# Conservative per-state defaults (seconds). Cheaper/soft-fresher states (an open PR
# whose review state changes constantly) get a short TTL; supply-chain-heavy or
# already-terminal facts that change rarely (a merged commit, a published release) get
# a longer one. These are defaults, not policy carved in stone -- callers may override
# per class via `overrides`.
DEFAULT_TTL_SECONDS: Dict[str, int] = {
    "pr-open": 300,
    "merge-ready": 120,
    "merged": 3600,
    "released": 86400,
    "deployed": 3600,
    "issue-closed": 60,
    "issue-open": 60,
}

# A conservative fallback for any class not explicitly listed above.
DEFAULT_FALLBACK_TTL_SECONDS = 300


def _parse_iso(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    # Accept both "...Z" and "...+00:00" forms; `datetime.fromisoformat` (py>=3.11
    # handles "Z" natively, but this repo supports older runtimes too) so normalize.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def ttl_for_state(state: str, overrides: Optional[Mapping[str, int]] = None) -> int:
    """Resolve the TTL (seconds) for one delivery-state/observation class."""
    key = str(state or "").strip().lower()
    if overrides and key in overrides:
        return int(overrides[key])
    return int(DEFAULT_TTL_SECONDS.get(key, DEFAULT_FALLBACK_TTL_SECONDS))


def is_stale(observed_at: Any, ttl_seconds: int, *, now: Optional[datetime] = None) -> bool:
    """True when `observed_at` is missing/unparsable, in the future beyond a small
    clock-skew tolerance, or older than `ttl_seconds`. `ttl_seconds <= 0` means "always
    re-query" (used by callers that want to force a live read, e.g. a terminal
    transition precheck) and always reports stale.
    """
    if ttl_seconds is None or ttl_seconds <= 0:
        return True
    parsed = _parse_iso(observed_at)
    if parsed is None:
        return True
    current = now or datetime.now(timezone.utc)
    age_seconds = (current - parsed).total_seconds()
    # A small tolerance for clock skew: a timestamp up to 60s in the "future" from the
    # caller's clock is not treated as an error, but is also not treated as fresher than
    # "just observed" -- it simply does not count as stale on its own.
    if age_seconds < -60:
        return True  # clock badly out of sync / fabricated future timestamp: fail closed
    return age_seconds > ttl_seconds


def freshness_gate(observed_at: Any, state: str, *, overrides: Optional[Mapping[str, int]] = None,
                    now: Optional[datetime] = None) -> Dict[str, Any]:
    """Build a `pass`/`fail` gate dict (same shape as `delivery.py::_gate`) for one
    observation's freshness against its class TTL."""
    ttl_seconds = ttl_for_state(state, overrides)
    stale = is_stale(observed_at, ttl_seconds, now=now)
    return {
        "name": "delivery_freshness",
        "status": "fail" if stale else "pass",
        "reason_code": "observation_stale" if stale else "observation_fresh",
        "detail": (
            f"observation for state {state!r} is stale (ttl={ttl_seconds}s, observed_at={observed_at!r})"
            if stale else
            f"observation for state {state!r} is within its {ttl_seconds}s TTL"
        ),
        "ttl_seconds": ttl_seconds,
    }


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
