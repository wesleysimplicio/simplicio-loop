"""Typed, append-only attempt observations for Loop/Runtime reconciliation.

``loop_journal.py`` is intentionally human-friendly and accepts legacy rows.  This
module is the machine-facing companion: every row has stable identity, causal
lineage, a hash-chain link and an explicit claim class.  It is transport agnostic
and can therefore be exported to Runtime or replayed after a provider handoff.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .ops_ledger import _locks

SCHEMA = "simplicio.loop-observation/v1"
EVENT_KINDS = frozenset((
    "hypothesis", "action", "tool_execution", "validation", "failure",
    "observation", "decision",
))
CLAIM_TYPES = frozenset(("MEASURED", "UNVERIFIED", "ESTIMATED"))


class AttemptJournalError(ValueError):
    """Raised when journal data cannot be safely appended or replayed."""


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AttemptJournalError("%s must be a non-empty string" % name)
    return value.strip()


def build_observation(*, run_id: str, work_item_id: str, attempt_id: str,
                      actor: str, kind: str, payload: Mapping[str, Any],
                      sequence: int, event_id: str, causation_id: Optional[str] = None,
                      claim_type: str = "UNVERIFIED", ac_ids: Optional[Iterable[str]] = None,
                      observed_at: Optional[str] = None) -> Dict[str, Any]:
    """Build one validated typed observation envelope.

    ``event_id`` is caller-owned so retries can reuse it.  Reusing an ID with a
    changed envelope is rejected by :class:`AttemptJournal` rather than silently
    creating a second attempt.
    """
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise AttemptJournalError("sequence must be a positive integer")
    kind = _text(kind, "kind")
    if kind not in EVENT_KINDS:
        raise AttemptJournalError("unsupported observation kind: %s" % kind)
    if not isinstance(payload, Mapping):
        raise AttemptJournalError("payload must be an object")
    claim_type = _text(claim_type, "claim_type").upper()
    if claim_type not in CLAIM_TYPES:
        raise AttemptJournalError("unsupported claim_type: %s" % claim_type)
    criteria = list(ac_ids or ())
    if any(not isinstance(item, str) or not item.strip() for item in criteria):
        raise AttemptJournalError("ac_ids must contain non-empty strings")
    event: Dict[str, Any] = {
        "schema": SCHEMA,
        "sequence": sequence,
        "event_id": _text(event_id, "event_id"),
        "run_id": _text(run_id, "run_id"),
        "work_item_id": _text(work_item_id, "work_item_id"),
        "attempt_id": _text(attempt_id, "attempt_id"),
        "actor": _text(actor, "actor"),
        "kind": kind,
        "causation_id": _text(causation_id or event_id, "causation_id"),
        "claim_type": claim_type,
        "ac_ids": criteria,
        "payload": dict(payload),
    }
    if kind == "failure":
        # Providers may supply the loop_journal fingerprint; otherwise derive a
        # provider-independent digest from the failure payload.  This is the
        # identity used to decide whether a resumed retry is genuinely new.
        supplied = payload.get("failure_fingerprint", payload.get("fingerprint"))
        event["failure_fingerprint"] = _text(supplied, "failure_fingerprint") if supplied else _hash(payload)[:12]
    if observed_at is not None:
        event["observed_at"] = _text(observed_at, "observed_at")
    return event


def validate_observation(event: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(event, Mapping) or event.get("schema") != SCHEMA:
        raise AttemptJournalError("unsupported observation schema")
    required = ("event_id", "run_id", "work_item_id", "attempt_id", "actor",
                "kind", "causation_id", "claim_type", "ac_ids", "payload")
    for field in required:
        if field not in event:
            raise AttemptJournalError("observation missing %s" % field)
    checked = build_observation(
        run_id=event["run_id"], work_item_id=event["work_item_id"],
        attempt_id=event["attempt_id"], actor=event["actor"], kind=event["kind"],
        payload=event["payload"], sequence=event["sequence"], event_id=event["event_id"],
        causation_id=event.get("causation_id"), claim_type=event["claim_type"],
        ac_ids=event.get("ac_ids"), observed_at=event.get("observed_at"),
    )
    if event.get("kind") == "failure" and event.get("failure_fingerprint") != checked.get("failure_fingerprint"):
        raise AttemptJournalError("failure fingerprint mismatch")
    if set(event) - set(checked):
        # Forward-compatible fields are allowed, but are included in the hash and
        # therefore cannot be changed during replay.
        checked.update({key: event[key] for key in set(event) - set(checked)})
    return checked


class AttemptJournal:
    """Hash-chained JSONL journal with idempotent append and deterministic import."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.lock_path = Path(str(self.path) + ".lock")

    def _lock(self):
        if _locks is None:
            raise AttemptJournalError("cross-process locking helper is unavailable")
        _locks._ensure_lock_file(str(self.lock_path))
        if _locks.fcntl is not None:
            handle = self.lock_path.open("a+b")
            if not _locks._acquire_posix(handle, _locks.DEFAULT_TIMEOUT_MS):
                handle.close()
                raise AttemptJournalError("journal lock acquisition timed out")
            return handle, _locks._release_posix
        if _locks.msvcrt is not None:  # pragma: no cover - exercised on Windows CI
            handle = self.lock_path.open("r+b")
            if not _locks._acquire_windows(handle, _locks.DEFAULT_TIMEOUT_MS):
                handle.close()
                raise AttemptJournalError("journal lock acquisition timed out")
            return handle, _locks._release_windows
        raise AttemptJournalError("no cross-process locking primitive is available")

    def _read(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: List[Dict[str, Any]] = []
        for n, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise AttemptJournalError("invalid JSON at journal line %d" % n) from exc
            if not isinstance(row, dict):
                raise AttemptJournalError("journal line %d is not an object" % n)
            rows.append(row)
        return rows

    @staticmethod
    def _verify(rows: List[Dict[str, Any]]) -> None:
        previous = ""
        for expected, row in enumerate(rows, 1):
            validate_observation(row)
            if row.get("sequence") != expected or row.get("prev_hash", "") != previous:
                raise AttemptJournalError("journal sequence/hash gap at %s" % row.get("event_id", "?"))
            body = dict(row)
            recorded = body.pop("hash", None)
            if recorded != _hash(body):
                raise AttemptJournalError("journal hash mismatch at %s" % row.get("event_id", "?"))
            previous = recorded

    def replay(self) -> List[Dict[str, Any]]:
        handle, release = self._lock()
        try:
            rows = self._read()
            self._verify(rows)
            return rows
        finally:
            release(handle)
            handle.close()

    def append(self, event: Mapping[str, Any]) -> Dict[str, Any]:
        normalized = validate_observation(event)
        handle, release = self._lock()
        try:
            rows = self._read()
            self._verify(rows)
            for prior in rows:
                if prior.get("event_id") == normalized["event_id"]:
                    prior_core = {key: value for key, value in prior.items()
                                  if key not in ("sequence", "prev_hash", "hash")}
                    normalized_core = {key: value for key, value in normalized.items()
                                       if key not in ("sequence", "prev_hash", "hash")}
                    if prior_core == normalized_core:
                        return prior
                    raise AttemptJournalError("event_id already exists with a different envelope")
            body = dict(normalized)
            # Imported envelopes carry their source chain metadata.  Recompute
            # local sequence/link/hash instead of hashing the old hash field.
            body.pop("hash", None)
            body.pop("prev_hash", None)
            body["sequence"] = len(rows) + 1
            body["prev_hash"] = rows[-1].get("hash", "") if rows else ""
            body["hash"] = _hash(body)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(body, ensure_ascii=False, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            return body
        finally:
            release(handle)
            handle.close()

    def import_legacy(self, path: str | os.PathLike[str], *, run_id: str,
                      work_item_id: str, attempt_id: str, actor: str) -> List[Dict[str, Any]]:
        """Migrate human journal rows deterministically; source is never modified."""
        source = Path(path)
        if not source.exists():
            raise AttemptJournalError("legacy journal does not exist")
        imported: List[Dict[str, Any]] = []
        for index, raw in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            try:
                old = json.loads(raw)
            except ValueError as exc:
                raise AttemptJournalError("invalid legacy JSON at line %d" % index) from exc
            gate = str(old.get("gate", "blocked")).lower()
            kind = "validation" if gate == "pass" else ("failure" if gate == "fail" else "observation")
            event = build_observation(
                run_id=run_id, work_item_id=work_item_id, attempt_id=attempt_id,
                actor=actor, kind=kind, sequence=index, event_id="legacy-%d" % index,
                causation_id="legacy-%d" % index,
                claim_type="MEASURED" if gate == "pass" else "UNVERIFIED",
                payload={"legacy": old},
            )
            imported.append(self.append(event))
        return imported

    def export(self) -> List[Dict[str, Any]]:
        """Return the verified canonical envelopes for Runtime ingestion."""
        return self.replay()

    def import_events(self, events: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        """Validate and idempotently ingest envelopes from Runtime or another host."""
        return [self.append(event) for event in events]


__all__ = ["AttemptJournal", "AttemptJournalError", "CLAIM_TYPES", "EVENT_KINDS",
           "SCHEMA", "build_observation", "validate_observation"]
