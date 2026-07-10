"""Hash-chained, cross-process-safe operational event ledger."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

try:
    import _locked_append as _locks
except ImportError:  # pragma: no cover
    _locks = None


SCHEMA = "simplicio.ops-event/v1"
CONTEXT_SCHEMA = "simplicio.ops-context/v1"
HANDSHAKE_SCHEMA = "simplicio.executor-handshake/v1"
LEGACY_COMPATIBILITY = "legacy-v1"
REQUIRED_CONTEXT_FIELDS = (
    "run_id",
    "wave_id",
    "lane",
    "owner",
    "session",
    "reason_code",
)
REQUIRED_HANDSHAKE_FIELDS = (
    "executor_id",
    "executor_version",
    "protocol",
    "concurrency_budget",
    "context",
)


class LedgerError(ValueError):
    """Raised when the ledger is corrupt or cannot be safely updated."""


def validate_handshake(handshake: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate and copy the executor handshake used by read-only surfaces.

    The handshake is deliberately independent from an event payload: a viewer
    may replay a ledger without mutating it, while still proving which executor
    contract and concurrency budget it is interpreting.  Keeping the complete
    context object in the handshake also makes the CLI output self-contained.
    """
    if not isinstance(handshake, Mapping):
        raise LedgerError("executor handshake must be an object")
    normalized = dict(handshake)
    if normalized.get("schema") != HANDSHAKE_SCHEMA:
        raise LedgerError("executor handshake schema must be %s" % HANDSHAKE_SCHEMA)
    missing = [name for name in REQUIRED_HANDSHAKE_FIELDS if name not in normalized]
    if missing:
        raise LedgerError("executor handshake missing required fields: %s" %
                          ", ".join(missing))
    for name in ("executor_id", "executor_version", "protocol"):
        if not isinstance(normalized[name], str) or not normalized[name].strip():
            raise LedgerError("executor handshake %s must be a non-empty string" % name)
    budget = normalized["concurrency_budget"]
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise LedgerError("executor handshake concurrency_budget must be a positive integer")
    normalized["context"] = _validate_context(
        normalized["context"], kind="executor_handshake", payload={}
    )
    return normalized


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _validate_context(context: Mapping[str, Any], *, kind: str,
                      payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate and copy the minimum operational context contract.

    Context is deliberately a separate, hash-bound object instead of being
    inferred from the event payload.  This keeps queue identity and evidence
    provenance available to read-only consumers even when payload schemas
    evolve.  Additional context keys remain allowed for forward-compatible
    extensions; the six required keys below are never optional for a strict
    event.
    """
    if not isinstance(context, Mapping):
        raise LedgerError("event context must be an object")
    if not isinstance(payload, Mapping):
        raise LedgerError("event payload must be an object")
    normalized = dict(context)
    missing = [name for name in REQUIRED_CONTEXT_FIELDS
               if not isinstance(normalized.get(name), str)
               or not normalized[name].strip()]
    if missing:
        raise LedgerError("event context missing required fields: %s" %
                          ", ".join(missing))

    receipts = normalized.get("receipts")
    receipt_signal = str(kind).lower()
    receipt_signal = ("receipt" in receipt_signal or
                      any(name in payload for name in
                          ("receipt", "receipts", "receipt_id")))
    if receipts is not None:
        if not isinstance(receipts, list):
            raise LedgerError("event context receipts must be a list")
        for index, receipt in enumerate(receipts):
            if isinstance(receipt, str):
                if not receipt.strip():
                    raise LedgerError("event context receipt %d is empty" % index)
                continue
            if not isinstance(receipt, Mapping):
                raise LedgerError("event context receipt %d must be a string or object" % index)
            if not any(isinstance(receipt.get(name), str) and receipt[name].strip()
                       for name in ("id", "receipt_id", "path")):
                raise LedgerError(
                    "event context receipt %d needs id, receipt_id, or path" % index
                )
    if receipt_signal and (not isinstance(receipts, list) or not receipts):
        raise LedgerError("receipt events require a non-empty context.receipts list")
    return normalized


class EventLedger:
    """Append events with a monotonic sequence and verifiable hash chain."""

    def __init__(self, path: str | os.PathLike[str], *, compatibility: bool = False):
        self.path = Path(path)
        self.lock_path = Path(str(self.path) + ".lock")
        # Existing v1 ledgers did not carry context.  Reading or appending
        # those records is still available, but only through this explicit
        # opt-in so new callers cannot silently lose provenance.
        self.compatibility = bool(compatibility)

    @contextmanager
    def _lock(self) -> Iterator[None]:
        if _locks is None:
            raise LedgerError("cross-process locking helper is unavailable")
        _locks._ensure_lock_file(str(self.lock_path))
        if _locks.fcntl is not None:
            with self.lock_path.open("a+b") as handle:
                if not _locks._acquire_posix(handle, _locks.DEFAULT_TIMEOUT_MS):
                    raise LedgerError("ledger lock acquisition timed out")
                try:
                    yield
                finally:
                    _locks._release_posix(handle)
        elif _locks.msvcrt is not None:  # pragma: no cover - Windows CI covers this path
            with self.lock_path.open("r+b") as handle:
                if not _locks._acquire_windows(handle, _locks.DEFAULT_TIMEOUT_MS):
                    raise LedgerError("ledger lock acquisition timed out")
                try:
                    yield
                finally:
                    _locks._release_windows(handle)
        else:  # pragma: no cover
            raise LedgerError("no cross-process locking primitive is available")

    def _read_unlocked(self, recover_trailing: bool = False) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        events: List[Dict[str, Any]] = []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        for line_number, raw in enumerate(lines, 1):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
            except ValueError as exc:
                if recover_trailing and line_number == len(lines):
                    self.path.write_text("\n".join(lines[:-1]) + ("\n" if lines[:-1] else ""), encoding="utf-8")
                    break
                raise LedgerError("invalid JSON at ledger line %d" % line_number) from exc
            if not isinstance(value, dict):
                raise LedgerError("ledger line %d is not an object" % line_number)
            events.append(value)
        return events

    def _verify(self, events: List[Dict[str, Any]]) -> None:
        previous = ""
        for expected_sequence, event in enumerate(events, 1):
            if event.get("schema") != SCHEMA or event.get("sequence") != expected_sequence:
                raise LedgerError("ledger sequence/schema mismatch at %s" % event.get("event_id", "?"))
            if event.get("prev_hash", "") != previous:
                raise LedgerError("ledger hash chain mismatch at %s" % event.get("event_id", "?"))
            body = dict(event)
            recorded = body.pop("hash", None)
            if recorded != _digest(body):
                raise LedgerError("ledger event hash mismatch at %s" % event.get("event_id", "?"))
            context_schema = event.get("context_schema")
            if context_schema == CONTEXT_SCHEMA:
                _validate_context(event.get("context"),
                                  kind=str(event.get("kind", "")),
                                  payload=event.get("payload", {}))
            elif event.get("compatibility") == LEGACY_COMPATIBILITY:
                if not self.compatibility:
                    raise LedgerError(
                        "legacy ledger event requires compatibility=True"
                    )
            elif self.compatibility and not context_schema and "context" not in event:
                # A pre-context v1 row may not have a compatibility marker at
                # all.  This branch is intentionally limited to explicit
                # compatibility mode and is never used by strict readers.
                continue
            else:
                raise LedgerError(
                    "ledger event context schema is missing or unsupported; "
                    "legacy rows require compatibility=True"
                )
            previous = recorded

    def replay(self, recover_trailing: bool = False) -> List[Dict[str, Any]]:
        with self._lock():
            events = self._read_unlocked(recover_trailing=recover_trailing)
            self._verify(events)
            return events

    def append(self, kind: str, payload: Mapping[str, Any], event_id: Optional[str] = None,
               *, context: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        if not kind.strip():
            raise ValueError("event kind is required")
        if not isinstance(payload, Mapping):
            raise ValueError("event payload must be an object")
        if context is None and not self.compatibility:
            raise LedgerError(
                "event context is required; pass compatibility=True only for legacy v1"
            )
        normalized_context = (_validate_context(context, kind=kind, payload=payload)
                              if context is not None else None)
        with self._lock():
            events = self._read_unlocked()
            self._verify(events)
            event_id = event_id or "%d-%s" % (time.time_ns(), os.getpid())
            for existing in events:
                if existing.get("event_id") == event_id:
                    if (existing.get("kind") == kind
                            and existing.get("payload") == dict(payload)
                            and existing.get("context") == normalized_context):
                        return existing
                    raise LedgerError("event_id already exists with a different payload: %s" % event_id)
            body: Dict[str, Any] = {
                "schema": SCHEMA,
                "sequence": len(events) + 1,
                "event_id": event_id,
                "kind": kind,
                "payload": dict(payload),
                "prev_hash": events[-1].get("hash", "") if events else "",
            }
            if normalized_context is not None:
                body["context_schema"] = CONTEXT_SCHEMA
                body["context"] = normalized_context
            else:
                body["compatibility"] = LEGACY_COMPATIBILITY
            body["hash"] = _digest(body)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(body, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return body


__all__ = [
    "CONTEXT_SCHEMA",
    "EventLedger",
    "HANDSHAKE_SCHEMA",
    "LEGACY_COMPATIBILITY",
    "LedgerError",
    "REQUIRED_CONTEXT_FIELDS",
    "REQUIRED_HANDSHAKE_FIELDS",
    "SCHEMA",
    "validate_handshake",
]
