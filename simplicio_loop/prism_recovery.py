"""Crash-safe hierarchical Prism journal and recovery decisions."""

from __future__ import annotations

import os
import struct
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .hbp_ledger import GENESIS_HASH, canonical_sha256
from .prism_contracts import (
    HBP_MAGIC,
    HBP_MAX_FRAME_BYTES,
    PrismContractError,
    decode_hbp_frame,
    encode_hbp_frame,
)

EVENT_SCHEMA = "simplicio.prism-journal-event/v1"
RECOVERY_SCHEMA = "simplicio.prism-recovery-report/v1"
EVENT_TYPES = frozenset(
    {
        "hierarchy_declared",
        "task_queued",
        "task_started",
        "task_terminal",
        "slot_terminal",
        "prism_terminal",
        "lease_acquired",
        "lease_heartbeat",
        "lease_takeover",
        "effect_intent",
        "effect_receipt",
        "cancel_requested",
        "checkpoint",
    }
)


class PrismRecoveryError(RuntimeError):
    reason_code = "PRISM_RECOVERY_ERROR"


@dataclass(frozen=True)
class RecoveryEvent:
    aggregate_id: str
    event_type: str
    payload: Mapping[str, Any]
    prism_id: str
    slot_id: str | None = None
    task_id: str | None = None
    attempt: int | None = None
    fence: int | None = None

    def __post_init__(self) -> None:
        if not self.aggregate_id or not self.prism_id:
            raise PrismRecoveryError("aggregate_id and prism_id are required")
        if self.event_type not in EVENT_TYPES:
            raise PrismRecoveryError("unknown recovery event type")
        if self.attempt is not None and self.attempt < 1:
            raise PrismRecoveryError("attempt must be positive")
        if self.fence is not None and self.fence < 1:
            raise PrismRecoveryError("fence must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "aggregate_id": self.aggregate_id,
            "event_type": self.event_type,
            "payload": dict(self.payload),
            "prism_id": self.prism_id,
            "slot_id": self.slot_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "fence": self.fence,
        }


class PrismJournal:
    """Append-before-effect binary HBP journal with a SHA-256 chain."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock_ns: Callable[[], int] = time.time_ns,
        max_frame_bytes: int = HBP_MAX_FRAME_BYTES,
    ) -> None:
        self.path = Path(path)
        self.clock_ns = clock_ns
        self.max_frame_bytes = int(max_frame_bytes)
        if not 1 <= self.max_frame_bytes <= HBP_MAX_FRAME_BYTES:
            raise PrismRecoveryError("invalid max_frame_bytes")

    def _frames(self) -> list[bytes]:
        if not self.path.exists():
            return []
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            raise PrismRecoveryError("journal unreadable") from exc
        frames: list[bytes] = []
        offset = 0
        while offset < len(raw):
            if len(raw) - offset < 8:
                raise PrismRecoveryError("journal truncated header")
            if raw[offset : offset + 4] != HBP_MAGIC:
                raise PrismRecoveryError("journal frame magic mismatch")
            size = struct.unpack(">I", raw[offset + 4 : offset + 8])[0]
            if size > self.max_frame_bytes:
                raise PrismRecoveryError("journal frame exceeds limit")
            end = offset + 8 + size + 32
            if end > len(raw):
                raise PrismRecoveryError("journal truncated frame")
            frames.append(raw[offset:end])
            offset = end
        return frames

    def replay(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        previous = GENESIS_HASH
        for sequence, frame in enumerate(self._frames(), start=1):
            try:
                row = decode_hbp_frame(frame)
            except PrismContractError as exc:
                raise PrismRecoveryError(str(exc)) from exc
            event_hash = row.pop("event_hash", None)
            if (
                row.get("schema") != EVENT_SCHEMA
                or row.get("sequence") != sequence
                or row.get("previous_event_hash") != previous
                or row.get("event_type") not in EVENT_TYPES
                or canonical_sha256(row) != event_hash
            ):
                raise PrismRecoveryError(f"journal hash-chain mismatch at {sequence}")
            row["event_hash"] = event_hash
            rows.append(row)
            previous = str(event_hash)
        return rows

    def append(self, event: RecoveryEvent) -> dict[str, Any]:
        rows = self.replay()
        row = {
            "schema": EVENT_SCHEMA,
            "sequence": len(rows) + 1,
            "previous_event_hash": rows[-1]["event_hash"] if rows else GENESIS_HASH,
            "observed_at_ns": int(self.clock_ns()),
            **event.to_dict(),
        }
        row["event_hash"] = canonical_sha256(row)
        frame = encode_hbp_frame(row)
        if len(frame) > self.max_frame_bytes + 40:
            raise PrismRecoveryError("encoded journal frame exceeds limit")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            str(self.path),
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            written = os.write(descriptor, frame)
            if written != len(frame):
                raise PrismRecoveryError("partial journal write")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return row

    def effect_intent(
        self,
        *,
        prism_id: str,
        slot_id: str,
        task_id: str,
        attempt: int,
        fence: int,
        effect_id: str,
        effect_hash: str,
    ) -> dict[str, Any]:
        return self.append(
            RecoveryEvent(
                aggregate_id=task_id,
                event_type="effect_intent",
                prism_id=prism_id,
                slot_id=slot_id,
                task_id=task_id,
                attempt=attempt,
                fence=fence,
                payload={"effect_id": effect_id, "effect_hash": effect_hash},
            )
        )

    def effect_receipt(
        self,
        *,
        prism_id: str,
        slot_id: str,
        task_id: str,
        attempt: int,
        fence: int,
        effect_id: str,
        receipt_hash: str,
        recovered: bool = False,
    ) -> dict[str, Any]:
        return self.append(
            RecoveryEvent(
                aggregate_id=task_id,
                event_type="effect_receipt",
                prism_id=prism_id,
                slot_id=slot_id,
                task_id=task_id,
                attempt=attempt,
                fence=fence,
                payload={
                    "effect_id": effect_id,
                    "receipt_hash": receipt_hash,
                    "recovered": bool(recovered),
                },
            )
        )

    def checkpoint(self, state: Mapping[str, Any], *, prism_id: str) -> dict[str, Any]:
        rows = self.replay()
        return self.append(
            RecoveryEvent(
                aggregate_id=prism_id,
                event_type="checkpoint",
                prism_id=prism_id,
                payload={
                    "state_digest": canonical_sha256(state),
                    "covered_sequence": len(rows),
                    "covered_head": rows[-1]["event_hash"] if rows else GENESIS_HASH,
                },
            )
        )

    def doctor(self) -> dict[str, Any]:
        started = time.perf_counter_ns()
        try:
            rows = self.replay()
        except PrismRecoveryError as exc:
            return {
                "schema": RECOVERY_SCHEMA,
                "status": "CORRUPT",
                "reason_code": "JOURNAL_CORRUPT",
                "detail": str(exc),
                "event_count": None,
                "head": None,
                "verification_ns": time.perf_counter_ns() - started,
            }
        return {
            "schema": RECOVERY_SCHEMA,
            "status": "VERIFIED",
            "reason_code": "OK",
            "detail": "",
            "event_count": len(rows),
            "head": rows[-1]["event_hash"] if rows else GENESIS_HASH,
            "verification_ns": time.perf_counter_ns() - started,
        }


def recover_state(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Rebuild task/fence/effect state without inferring terminal success."""
    tasks: dict[str, str] = {}
    fences: dict[str, int] = {}
    intents: dict[str, dict[str, Any]] = {}
    receipts: dict[str, dict[str, Any]] = {}
    active_children: set[str] = set()
    for row in rows:
        task_id = str(row.get("task_id") or "")
        event_type = row.get("event_type")
        payload = dict(row.get("payload") or {})
        if task_id and event_type in {"task_queued", "task_started"}:
            tasks[task_id] = "queued" if event_type == "task_queued" else "running"
            active_children.add(task_id)
        elif task_id and event_type == "task_terminal":
            if not payload.get("receipt_hash"):
                raise PrismRecoveryError("terminal event missing receipt")
            tasks[task_id] = str(payload.get("state") or "blocked")
            active_children.discard(task_id)
        if task_id and row.get("fence") is not None:
            fence = int(row["fence"])
            if fence < fences.get(task_id, 0):
                raise PrismRecoveryError("fence regressed during replay")
            fences[task_id] = fence
        if event_type == "effect_intent":
            effect_id = str(payload.get("effect_id") or "")
            if not effect_id:
                raise PrismRecoveryError("effect intent missing id")
            intents[effect_id] = dict(row)
        elif event_type == "effect_receipt":
            effect_id = str(payload.get("effect_id") or "")
            if not effect_id or effect_id not in intents:
                raise PrismRecoveryError("effect receipt has no prior intent")
            receipts[effect_id] = dict(row)
    payload = {
        "schema": "simplicio.prism-recovered-state/v1",
        "tasks": dict(sorted(tasks.items())),
        "fences": dict(sorted(fences.items())),
        "active_children": sorted(active_children),
        "effect_intents": sorted(intents),
        "effect_receipts": sorted(receipts),
        "orphan_intents": sorted(set(intents) - set(receipts)),
    }
    payload["state_digest"] = canonical_sha256(payload)
    return payload


def reconcile_orphan_intents(
    journal: PrismJournal,
    lookup: Callable[[str], str | None],
) -> dict[str, Any]:
    """Consult Dev CLI receipts; never re-run an unresolved effect."""
    rows = journal.replay()
    state = recover_state(rows)
    reconciled: list[str] = []
    required: list[str] = []
    by_effect = {
        str(row["payload"]["effect_id"]): row
        for row in rows
        if row["event_type"] == "effect_intent"
    }
    for effect_id in state["orphan_intents"]:
        receipt_hash = lookup(effect_id)
        if not receipt_hash:
            required.append(effect_id)
            continue
        intent = by_effect[effect_id]
        journal.effect_receipt(
            prism_id=str(intent["prism_id"]),
            slot_id=str(intent["slot_id"]),
            task_id=str(intent["task_id"]),
            attempt=int(intent["attempt"]),
            fence=int(intent["fence"]),
            effect_id=effect_id,
            receipt_hash=receipt_hash,
            recovered=True,
        )
        reconciled.append(effect_id)
    payload = {
        "schema": RECOVERY_SCHEMA,
        "status": "RECOVERY_REQUIRED" if required else "RECONCILED",
        "reason_code": "ORPHAN_EFFECT_INTENT" if required else "OK",
        "reconciled_effects": sorted(reconciled),
        "required_effects": sorted(required),
        "effects_reexecuted": 0,
    }
    payload["report_hash"] = canonical_sha256(payload)
    return payload


def assert_current_fence(state: Mapping[str, Any], task_id: str, fence: int) -> None:
    current = dict(state.get("fences") or {}).get(task_id)
    if current is None or int(current) != int(fence):
        raise PrismRecoveryError("STALE_FENCE")


__all__ = [
    "EVENT_SCHEMA",
    "RECOVERY_SCHEMA",
    "PrismJournal",
    "PrismRecoveryError",
    "RecoveryEvent",
    "assert_current_fence",
    "reconcile_orphan_intents",
    "recover_state",
]
