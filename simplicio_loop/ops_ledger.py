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


class LedgerError(ValueError):
    """Raised when the ledger is corrupt or cannot be safely updated."""


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


class EventLedger:
    """Append events with a monotonic sequence and verifiable hash chain."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.lock_path = Path(str(self.path) + ".lock")

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

    def _read_unlocked(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        events: List[Dict[str, Any]] = []
        for line_number, raw in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
            except ValueError as exc:
                raise LedgerError("invalid JSON at ledger line %d" % line_number) from exc
            if not isinstance(value, dict):
                raise LedgerError("ledger line %d is not an object" % line_number)
            events.append(value)
        return events

    @staticmethod
    def _verify(events: List[Dict[str, Any]]) -> None:
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
            previous = recorded

    def replay(self) -> List[Dict[str, Any]]:
        with self._lock():
            events = self._read_unlocked()
            self._verify(events)
            return events

    def append(self, kind: str, payload: Mapping[str, Any], event_id: Optional[str] = None) -> Dict[str, Any]:
        if not kind.strip():
            raise ValueError("event kind is required")
        with self._lock():
            events = self._read_unlocked()
            self._verify(events)
            event_id = event_id or "%d-%s" % (time.time_ns(), os.getpid())
            for existing in events:
                if existing.get("event_id") == event_id:
                    return existing
            body: Dict[str, Any] = {
                "schema": SCHEMA,
                "sequence": len(events) + 1,
                "event_id": event_id,
                "kind": kind,
                "payload": dict(payload),
                "prev_hash": events[-1].get("hash", "") if events else "",
            }
            body["hash"] = _digest(body)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(body, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return body


__all__ = ["EventLedger", "LedgerError", "SCHEMA"]
