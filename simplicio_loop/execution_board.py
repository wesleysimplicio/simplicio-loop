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
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

SCHEMA = "simplicio.execution-board/v1"
EVENT_SCHEMA = "simplicio.execution-board-event/v1"
TERMINAL = {"done", "blocked", "cancelled"}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _normalize_delivery(payload: Mapping[str, Any], *, gates: Mapping[str, Any]) -> Dict[str, Any]:
    target = str(payload.get("target") or "").strip() or "unknown"
    satisfied = bool(payload.get("satisfied"))
    merge_queue = dict(payload.get("merge_queue") or {})
    merge_receipt = str(merge_queue.get("receipt_sha") or payload.get("merge_queue_receipt_sha") or "").strip()
    merge_status = str(merge_queue.get("status") or payload.get("merge_queue_status") or "").strip().lower()
    evidence_gate = bool(gates.get("evidence")) and bool(gates.get("watcher"))
    if target == "local-fixture":
        convergence = "local-fixture" if satisfied and evidence_gate else "UNVERIFIED"
    elif merge_receipt and merge_status == "accepted":
        convergence = "merge-queue-verified" if satisfied and evidence_gate else "UNVERIFIED"
    else:
        convergence = "UNVERIFIED"
    return {
        "target": target,
        "satisfied": satisfied,
        "evidence_gate": evidence_gate,
        "merge_queue": merge_queue,
        "merge_queue_receipt_sha": merge_receipt,
        "merge_queue_status": merge_status,
        "convergence": convergence,
        "external": convergence == "merge-queue-verified",
        "verified": convergence in {"local-fixture", "merge-queue-verified"},
    }


def _read_backlog(path: str | Path) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Read the portable JSONL backlog format without importing CLI internals."""
    master: Dict[str, Any] = {}
    items: List[Dict[str, Any]] = []
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("kind") == "master":
                    master = row
                elif row.get("kind") == "item":
                    items.append(row)
    except (OSError, ValueError, TypeError) as exc:
        raise BoardError("invalid backlog JSONL: %s" % exc) from exc
    return master, items


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

    @classmethod
    def from_backlog(cls, path: str | Path, *, run_id: Optional[str] = None,
                     external: bool = False) -> "ExecutionBoard":
        """Import a frozen backlog into a deterministic local board projection.

        The backlog remains the source of truth.  Each canonical WorkItem is carried
        losslessly in a ``created`` event so a Runtime/desktop consumer can render one
        card without reconstructing or dropping acceptance criteria and metadata.
        """
        master, items = _read_backlog(path)
        if not master or not items:
            raise BoardError("backlog must contain one master and at least one WorkItem")
        if master.get("schema") != "simplicio.backlog/v2":
            raise BoardError("unsupported backlog schema")
        if (master.get("contract") or {}).get("name") != "simplicio.work-items/v1":
            raise BoardError("unsupported WorkItem contract")
        ids = [str(item.get("id") or "") for item in items]
        if not all(ids) or len(set(ids)) != len(ids):
            raise BoardError("backlog contains missing or duplicate WorkItem ids")
        board = cls(run_id=run_id or str(master.get("goal_fp") or master.get("goal") or "backlog"),
                    external=external)
        for item in sorted(items, key=lambda row: str(row.get("id"))):
            board.append("created", item_id=str(item["id"]), payload={
                "title": str(item.get("goal") or item["id"]),
                "status": str(item.get("status") or "ready"),
                "work_item": item,
                "goal": item.get("goal"), "goal_fp": item.get("goal_fp"),
                "acs": list(item.get("acs") or []),
                "depends_on": list(item.get("depends_on") or []),
                "source_refs": list(item.get("source_refs") or []),
                "required_evidence": list(item.get("required_evidence") or []),
                "risks": list(item.get("risks") or []),
                "estimate": item.get("estimate"),
                "scheduling_hints": dict(item.get("scheduling_hints") or {}),
            })
        return board

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
                }, "delivery": {"target": "unknown", "satisfied": False, "evidence_gate": False,
                                 "merge_queue": {}, "merge_queue_receipt_sha": "",
                                 "merge_queue_status": "", "convergence": "UNVERIFIED",
                                 "external": False, "verified": False},
            })
            payload = dict(event.get("payload") or {})
            card["events"].append({"sequence": expected, "kind": event["kind"], "payload": payload})
            if event["kind"] == "created":
                card["title"] = str(payload.get("title") or item_id)
                card["depends_on"] = list(payload.get("depends_on") or [])
                if isinstance(payload.get("work_item"), Mapping):
                    card["work_item"] = dict(payload["work_item"])
                for field in ("goal", "goal_fp", "acs", "source_refs", "required_evidence",
                              "risks", "estimate", "scheduling_hints"):
                    if field in payload:
                        value = payload[field]
                        card[field] = (dict(value) if isinstance(value, Mapping)
                                       else list(value) if isinstance(value, list) else value)
                initial_status = str(payload.get("status") or "queued")
                if initial_status in {"queued", "ready", "blocked", "done", "skipped", "cancelled"}:
                    card["status"] = initial_status
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
                card["delivery"] = _normalize_delivery(payload, gates=card["gates"])
        cards_sorted = [cards[key] for key in sorted(cards)]
        total_cards = len(cards_sorted)
        converged_cards = sum(
            1 for card in cards_sorted if card["status"] == "done" and bool(card["delivery"].get("verified"))
        )
        summary = {
            "total_cards": total_cards,
            "done_cards": sum(1 for card in cards_sorted if card["status"] == "done"),
            "delivery_converged_cards": sum(1 for card in cards_sorted if bool(card["delivery"].get("verified"))),
            "merge_queue_verified_cards": sum(
                1 for card in cards_sorted if card["delivery"].get("convergence") == "merge-queue-verified"
            ),
            "local_fixture_cards": sum(
                1 for card in cards_sorted if card["delivery"].get("convergence") == "local-fixture"
            ),
            "converged_cards": converged_cards,
            "completion_percent": (100 if total_cards and converged_cards == total_cards
                                   else int((100 * converged_cards) / total_cards) if total_cards else 0),
        }
        summary["fronts_converged"] = (
            total_cards > 0
            and summary["done_cards"] == total_cards
            and summary["delivery_converged_cards"] == total_cards
        )
        projection = {
            "schema": SCHEMA,
            "run_id": self.run_id,
            "external_board": self.external,
            "external_status": "VERIFIED" if self.external else "UNVERIFIED",
            "cards": cards_sorted,
            "event_count": len(source),
            "summary": summary,
            "status": "COMPLETE" if summary["fronts_converged"] else "INCOMPLETE",
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
            "external_status": projection["external_status"], "status": projection["status"],
            "completion_percent": projection["summary"]["completion_percent"],
            "fronts_converged": projection["summary"]["fronts_converged"], "tag": "MEASURED",
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"events": ledger, "board": board, "text": text, "receipt": receipt}


__all__ = ["BoardError", "EVENT_SCHEMA", "ExecutionBoard", "SCHEMA"]
