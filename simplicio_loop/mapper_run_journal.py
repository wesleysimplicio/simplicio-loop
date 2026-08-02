"""RunJournal compatibility adapter backed by MapperStore ``ops_events``.

The adapter keeps the Loop journal contract at the boundary while leaving
durability, sequencing, hashing, replay, and compaction to MapperStore.  It is
deliberately free of sqlite imports and never creates a local journal database.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .mapper_operations import MapperOperationsAdapter, MapperOperationsError
from .remote_queue import QueueConflict

EVENT_SCHEMA = "simplicio.run-event/v1"
TERMINAL_SCHEMA = "simplicio.run-terminal-receipt/v1"
GENESIS_HASH = "sha256:" + ("0" * 64)
_IDEMPOTENCY_FIELD = "_loop_idempotency_key"
_CAUSAL_PARENT_FIELD = "_loop_causal_parent"


class MapperJournalError(RuntimeError):
    """A Mapper-backed journal cannot satisfy the RunJournal contract."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


class MapperRunJournal:
    """RunJournal-shaped facade over ``MapperOperationsAdapter``."""

    def __init__(
        self,
        database: str | Path,
        *,
        adapter: MapperOperationsAdapter | None = None,
        auto_create: bool = False,
    ) -> None:
        self.database = Path(database).expanduser().absolute()
        self.adapter = adapter or MapperOperationsAdapter(
            self.database, auto_create=auto_create
        )

    def _raw_replay(self, run_id: str) -> dict[str, Any]:
        try:
            replay = self.adapter.replay(run_id)
        except (MapperOperationsError, QueueConflict) as error:
            raise MapperJournalError(f"mapper_replay_failed:{error}") from error
        if not replay.get("valid"):
            raise MapperJournalError("mapper_replay_invalid")
        if replay.get("compaction") is not None:
            raise MapperJournalError("MAPPER_JOURNAL_COMPACTION_REQUIRES_MIGRATION")
        return replay

    @staticmethod
    def _public_event(
        raw: Mapping[str, Any],
        previous: Mapping[str, Any] | None,
        *,
        run_id: str,
    ) -> dict[str, Any]:
        payload = dict(raw.get("payload") or {})
        idempotency_key = str(payload.pop(_IDEMPOTENCY_FIELD, ""))
        causal_parent = payload.pop(_CAUSAL_PARENT_FIELD, None)
        event_id = str(raw["event_id"])
        previous_hash = str(previous["event_hash"]) if previous else GENESIS_HASH
        parent = str(previous["event_id"]) if previous else None
        return {
            "schema": EVENT_SCHEMA,
            "event_id": event_id,
            "run_id": run_id,
            "sequence": int(raw["seq"]),
            "kind": str(raw["event_type"]),
            "causal_parent": causal_parent if causal_parent is not None else parent,
            "idempotency_key": idempotency_key or f"mapper:event:{event_id}",
            "payload": payload,
            "created_at": raw["created_at"],
            "previous_hash": previous_hash,
            "event_hash": str(raw["event_hash"]),
        }

    def events(self, run_id: str) -> list[dict[str, Any]]:
        raw = self._raw_replay(run_id).get("events") or []
        return [
            self._public_event(event, raw[index - 1] if index else None, run_id=run_id)
            for index, event in enumerate(raw)
        ]

    def append(
        self,
        run_id: str,
        kind: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        causal_parent: str | None = None,
        expected_sequence: int | None = None,
    ) -> dict[str, Any]:
        if not run_id.strip() or not kind.strip() or not idempotency_key.strip():
            raise ValueError("run_id, kind and idempotency_key are required")
        raw = self._raw_replay(run_id)
        raw_events = list(raw.get("events") or [])
        current = raw_events[-1] if raw_events else None
        for index, event in enumerate(raw_events):
            event_payload = event.get("payload") or {}
            if event_payload.get(_IDEMPOTENCY_FIELD) == idempotency_key:
                public = self._public_event(
                    event, raw_events[index - 1] if index else None, run_id=run_id
                )
                return {
                    "status": "DUPLICATE",
                    "reason_code": "idempotency_key_replayed",
                    "event": public,
                }
        if current and current.get("event_type") == "run_terminal":
            return {"status": "REJECTED", "reason_code": "terminal_receipt_exists"}
        sequence = int(current["seq"]) + 1 if current else 1
        actual_parent = str(current["event_id"]) if current else None
        if expected_sequence is not None and int(expected_sequence) != sequence:
            return {
                "status": "REJECTED",
                "reason_code": "sequence_out_of_order",
                "expected_sequence": sequence,
            }
        if causal_parent is not None and causal_parent != actual_parent:
            return {
                "status": "REJECTED",
                "reason_code": "causal_parent_mismatch",
                "expected_parent": actual_parent,
            }
        if sequence == 1 and kind != "run_started":
            return {"status": "REJECTED", "reason_code": "run_not_started"}
        enriched = dict(payload)
        if _IDEMPOTENCY_FIELD in enriched and enriched[_IDEMPOTENCY_FIELD] != idempotency_key:
            raise ValueError(f"{_IDEMPOTENCY_FIELD} is reserved")
        enriched[_IDEMPOTENCY_FIELD] = idempotency_key
        enriched[_CAUSAL_PARENT_FIELD] = actual_parent
        try:
            result = self.adapter.append_event(
                run_id,
                kind,
                enriched,
                expected_seq=sequence - 1,
            )
        except QueueConflict as error:
            return {"status": "REJECTED", "reason_code": "sequence_out_of_order", "detail": str(error)}
        except MapperOperationsError as error:
            raise MapperJournalError(f"mapper_append_failed:{error}") from error
        replayed = self._raw_replay(run_id)
        event_id = result.get("event_id")
        raw_event = next(
            (event for event in replayed.get("events", []) if event.get("event_id") == event_id),
            None,
        )
        if raw_event is None:
            raise MapperJournalError("mapper_append_not_observable")
        events = replayed["events"]
        index = events.index(raw_event)
        return {
            "status": "APPENDED",
            "reason_code": None,
            "event": self._public_event(
                raw_event, events[index - 1] if index else None, run_id=run_id
            ),
        }

    def replay(self, run_id: str) -> dict[str, Any]:
        events = self.events(run_id)
        pending: dict[str, dict[str, Any]] = {}
        committed: dict[str, dict[str, Any]] = {}
        terminal = None
        for event in events:
            payload = event["payload"]
            if event["kind"] == "effect_prepared":
                pending[payload["effect_id"]] = payload["intent"]
            elif event["kind"] == "effect_committed":
                effect_id = payload["effect_id"]
                if effect_id not in pending:
                    raise MapperJournalError(f"effect_commit_without_prepare:{effect_id}")
                pending.pop(effect_id)
                committed[effect_id] = payload["receipt"]
            elif event["kind"] == "run_terminal":
                terminal = event
        return {
            "schema": "simplicio.run-projection/v1",
            "run_id": run_id,
            "sequence": len(events),
            "pending_effects": pending,
            "committed_effects": committed,
            "terminal": terminal,
            "head_hash": events[-1]["event_hash"] if events else GENESIS_HASH,
            "replay_engine": "mapper-store-operations-replay",
            "llm_used": False,
        }

    def checkpoint_before_effect(self, run_id: str, effect_id: str, intent: dict[str, Any]) -> dict[str, Any]:
        return self.append(
            run_id,
            "effect_prepared",
            {"effect_id": effect_id, "intent": intent},
            idempotency_key=f"effect:{effect_id}:prepared",
        )

    def checkpoint_after_effect(self, run_id: str, effect_id: str, receipt: dict[str, Any]) -> dict[str, Any]:
        if effect_id not in self.replay(run_id)["pending_effects"]:
            return {"status": "REJECTED", "reason_code": "effect_not_prepared"}
        return self.append(
            run_id,
            "effect_committed",
            {"effect_id": effect_id, "receipt": receipt},
            idempotency_key=f"effect:{effect_id}:committed",
        )

    def terminal(self, run_id: str, verdict: str, evidence: Iterable[str]) -> dict[str, Any]:
        result = self.append(
            run_id,
            "run_terminal",
            {"verdict": verdict, "evidence": sorted(set(evidence))},
            idempotency_key="run:terminal",
        )
        if result["status"] not in {"APPENDED", "DUPLICATE"}:
            return result
        event = result["event"]
        receipt = {
            "schema": TERMINAL_SCHEMA,
            "run_id": run_id,
            "terminal_event_id": event["event_id"],
            "sequence": event["sequence"],
            "event_hash": event["event_hash"],
            "verdict": event["payload"]["verdict"],
            "evidence": event["payload"]["evidence"],
        }
        receipt["receipt_hash"] = _hash(receipt)
        return {"status": result["status"], "reason_code": result["reason_code"], "receipt": receipt}

    def snapshot_and_compact(self, run_id: str) -> dict[str, Any]:
        raise MapperJournalError("MAPPER_JOURNAL_COMPACTION_REQUIRES_EXPLICIT_MIGRATION")

    def backup(self, target: str | Path) -> dict[str, Any]:
        raise MapperJournalError("MAPPER_JOURNAL_BACKUP_OWNED_BY_MAPPERSTORE")

    @classmethod
    def restore(cls, backup_path: str | Path, target_path: str | Path) -> "MapperRunJournal":
        raise MapperJournalError("MAPPER_JOURNAL_RESTORE_OWNED_BY_MAPPERSTORE")


__all__ = ["MapperJournalError", "MapperRunJournal"]
