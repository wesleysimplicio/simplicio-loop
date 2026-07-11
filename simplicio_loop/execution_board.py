"""Event-sourced Execution Board projection for multi-item loop runs.

The board is a read model, not a second source of truth: callers append typed events and
``replay`` reconstructs one card per WorkItem, its attempts and the gate history.  This local
backend is intentionally useful for E2E fixtures and single-host runs.  A remote board adapter
can consume the same event stream; absence of that adapter is surfaced as ``UNVERIFIED`` rather
than being reported as an external-board pass.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

SCHEMA = "simplicio.execution-board/v1"
EVENT_SCHEMA = "simplicio.execution-board-event/v1"
TERMINAL = {"done", "blocked", "cancelled"}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class BoardError(ValueError):
    pass


class ExecutionBoard:
    """Append-only event log plus deterministic WorkItem projection."""

    def __init__(self, *, run_id: str, external: bool = False) -> None:
        if not run_id.strip():
            raise BoardError("run_id is required")
        self.run_id = run_id
        self.external = bool(external)
        self._events: List[Dict[str, Any]] = []

    @property
    def events(self) -> List[Dict[str, Any]]:
        return [dict(event) for event in self._events]

    def append(self, kind: str, *, item_id: str, payload: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        if not kind.strip() or not item_id.strip():
            raise BoardError("kind and item_id are required")
        body = {
            "schema": EVENT_SCHEMA,
            "run_id": self.run_id,
            "sequence": len(self._events) + 1,
            "kind": kind,
            "item_id": item_id,
            "payload": dict(payload or {}),
            "created_at": "fixture-%04d" % (len(self._events) + 1),
        }
        body["prev_hash"] = self._events[-1]["hash"] if self._events else ""
        body["hash"] = _hash(body)
        self._events.append(body)
        return dict(body)

    def replay(self, events: Optional[Iterable[Mapping[str, Any]]] = None) -> Dict[str, Any]:
        source = [dict(event) for event in (self._events if events is None else events)]
        cards: Dict[str, Dict[str, Any]] = {}
        previous = ""
        for expected, event in enumerate(source, 1):
            if event.get("schema") != EVENT_SCHEMA or event.get("run_id") != self.run_id:
                raise BoardError("invalid event schema or run binding")
            if event.get("sequence") != expected or event.get("prev_hash", "") != previous:
                raise BoardError("event sequence/hash chain mismatch")
            recorded = event.get("hash")
            body = dict(event)
            body.pop("hash", None)
            if recorded != _hash(body):
                raise BoardError("event hash mismatch")
            previous = str(recorded)
            item_id = str(event["item_id"])
            card = cards.setdefault(item_id, {
                "id": item_id, "title": item_id, "status": "queued", "depends_on": [],
                "attempts": [], "events": [], "failure_history": [], "gates": {
                    "evidence": False, "watcher": False, "human": False,
                },
            })
            payload = dict(event.get("payload") or {})
            card["events"].append({"sequence": expected, "kind": event["kind"], "payload": payload})
            if event["kind"] == "created":
                card["title"] = str(payload.get("title") or item_id)
                card["depends_on"] = list(payload.get("depends_on") or [])
            elif event["kind"] == "dependency_blocked":
                card["status"] = "blocked"
            elif event["kind"] == "claimed":
                if card["depends_on"] and any(cards.get(dep, {}).get("status") != "done" for dep in card["depends_on"]):
                    raise BoardError("claim before dependencies")
                card["status"] = "executing"
            elif event["kind"] == "attempt_started":
                card["status"] = "executing"
                card["attempts"].append({"id": payload.get("attempt_id"), "status": "running", "events": []})
            elif event["kind"] == "validation_failed":
                card["status"] = "retrying"
                failure = {"attempt_id": payload.get("attempt_id"), "reason": payload.get("reason", "validation failed")}
                card["failure_history"].append(failure)
                for attempt in reversed(card["attempts"]):
                    if attempt["id"] == payload.get("attempt_id"):
                        attempt["status"] = "failed"
                        attempt["events"].append({"kind": event["kind"], "reason": failure["reason"]})
                        break
            elif event["kind"] == "human_gate_blocked":
                card["status"] = "review"
            elif event["kind"] == "human_decision":
                card["gates"]["human"] = str(payload.get("decision", "")).lower() == "approve"
                card["status"] = "executing" if card["gates"]["human"] else "blocked"
            elif event["kind"] == "evidence_recorded":
                card["gates"]["evidence"] = bool(payload.get("verified"))
                card["status"] = "verifying"
            elif event["kind"] == "watcher_passed":
                card["gates"]["watcher"] = bool(payload.get("match"))
                card["status"] = "verifying"
            elif event["kind"] == "completed":
                if not (card["gates"]["evidence"] and card["gates"]["watcher"]):
                    raise BoardError("completion before evidence and watcher gates")
                if card["depends_on"] and any(cards.get(dep, {}).get("status") != "done" for dep in card["depends_on"]):
                    raise BoardError("completion before dependencies")
                card["status"] = "done"
                for attempt in reversed(card["attempts"]):
                    if attempt["status"] == "running":
                        attempt["status"] = "passed"
                        break
            elif event["kind"] == "delivery_recorded":
                card["delivery"] = dict(payload)
        projection = {
            "schema": SCHEMA,
            "run_id": self.run_id,
            "external_board": self.external,
            "external_status": "VERIFIED" if self.external else "UNVERIFIED",
            "cards": [cards[key] for key in sorted(cards)],
            "event_count": len(source),
        }
        projection["projection_hash"] = _hash(projection)
        return projection

    def export(self, directory: str | Path) -> Dict[str, Path]:
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        projection = self.replay()
        ledger = root / "execution-board-events.jsonl"
        ledger.write_text("".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in self._events), encoding="utf-8")
        board = root / "execution-board.json"
        board.write_text(json.dumps(projection, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        lines = ["Execution Board · %s · %s" % (self.run_id, projection["external_status"]), ""]
        for card in projection["cards"]:
            mark = "✅" if card["status"] == "done" else ("⛔" if card["status"] in {"blocked", "review"} else "🔄")
            lines.append("%s %s [%s] attempts=%d failures=%d" % (mark, card["id"], card["status"], len(card["attempts"]), len(card["failure_history"])))
        text = root / "execution-board.txt"
        text.write_text("\n".join(lines) + "\n", encoding="utf-8")
        receipt = root / "execution-board-receipt.json"
        receipt.write_text(json.dumps({
            "schema": "simplicio.execution-board-receipt/v1", "run_id": self.run_id,
            "projection_hash": projection["projection_hash"], "event_count": len(self._events),
            "external_status": projection["external_status"], "tag": "MEASURED",
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"events": ledger, "board": board, "text": text, "receipt": receipt}


__all__ = ["BoardError", "EVENT_SCHEMA", "ExecutionBoard", "SCHEMA"]
