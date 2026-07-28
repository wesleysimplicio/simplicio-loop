"""Fast-backed canonical generation and isolated fan-out coordination."""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .fast_integration import FastIntegrationError, FastLoopIntegration, validate_changeset
from .checkpoint_lifecycle import CandidateSpec, CheckpointLifecycle, LifecycleError

SCHEMA = "simplicio.loop-fast-fanout/v1"
RECEIPT_SCHEMA = "simplicio.loop-fast-fanout-receipt/v1"

class FastFanoutError(RuntimeError):
    """Fan-out cannot safely use the pinned Fast generation."""

def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()

def _head(root: Path) -> str:
    try:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root),
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, close_fds=True,
                              timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""

@dataclass(frozen=True)
class CanonicalGeneration:
    generation: str
    context_hash: str
    source_commit: str
    plan_hash: str
    receipt_hash: str

    def to_dict(self) -> dict[str, str]:
        return {"generation": self.generation, "context_hash": self.context_hash,
                "source_commit": self.source_commit, "plan_hash": self.plan_hash,
                "receipt_hash": self.receipt_hash}

@dataclass
class _Slot:
    slot_id: str
    overlay_key: str
    dirty_files: tuple[str, ...]
    state: str = "held"

    def to_dict(self) -> dict[str, Any]:
        return {"slot_id": self.slot_id, "overlay_key": self.overlay_key,
                "dirty_files": list(self.dirty_files), "state": self.state}

class FastFanoutCoordinator:
    """Share one Fast prepare across slots and promote only a verified winner."""

    def __init__(self, root: str | Path, *, integration: FastLoopIntegration | None = None,
                 lifecycle: CheckpointLifecycle | None = None) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError("Fast fan-out root must be a directory")
        self.integration = integration or FastLoopIntegration(self.root)
        self.lifecycle = lifecycle
        self._canonical: CanonicalGeneration | None = None
        self._slots: dict[str, _Slot] = {}
        self._candidates: dict[str, dict[str, Any]] = {}
        self._winner: str | None = None
        self._rollout: dict[str, Any] | None = None
        self._metrics = {"canonical_builds": 0, "slot_leases": 0,
                         "candidate_count": 0, "loser_skips": 0,
                         "invalidations": 0}

    @property
    def generation(self) -> str:
        return self._canonical.generation if self._canonical else ""

    @property
    def context_hash(self) -> str:
        return self._canonical.context_hash if self._canonical else ""

    def prepare(self, task: str) -> dict[str, Any]:
        if self._canonical is not None:
            return {"schema": RECEIPT_SCHEMA, "status": "REUSED",
                    "canonical": self._canonical.to_dict(), "metrics": dict(self._metrics)}
        try:
            prepared = self.integration.prepare(task)
        except FastIntegrationError as exc:
            raise FastFanoutError(str(exc)) from exc
        if prepared.get("status") != "READY":
            raise FastFanoutError(str(prepared.get("reason") or "fast_prepare_not_ready"))
        values = {"generation": str(prepared.get("generation") or ""),
                  "context_hash": str(prepared.get("context_hash") or ""),
                  "source_commit": _head(self.root),
                  "plan_hash": str(prepared.get("plan_hash") or "")}
        if not values["generation"] or not values["context_hash"]:
            raise FastFanoutError("Fast prepare omitted generation or context hash")
        values["receipt_hash"] = _hash(values)
        self._canonical = CanonicalGeneration(**values)
        self._metrics["canonical_builds"] += 1
        return {"schema": RECEIPT_SCHEMA, "status": "MEASURED",
                "canonical": self._canonical.to_dict(), "fast": prepared,
                "metrics": dict(self._metrics), "local_llm": False}

    def acquire_slot(self, slot_id: str, *, overlay_tree_hash: str,
                     dirty_files: Sequence[str] = ()) -> dict[str, Any]:
        if self._canonical is None:
            raise FastFanoutError("prepare canonical generation before acquiring slots")
        slot_id = str(slot_id).strip()
        if not slot_id or slot_id in self._slots:
            raise FastFanoutError("slot_id must be unique and non-empty")
        files = tuple(sorted({str(path).strip().replace(chr(92), "/") for path in dirty_files if str(path).strip()}))
        overlay_key = _hash({"generation": self.generation, "slot_id": slot_id,
                             "tree_hash": str(overlay_tree_hash), "dirty_files": files})
        slot = _Slot(slot_id, overlay_key, files)
        self._slots[slot_id] = slot
        self._metrics["slot_leases"] += 1
        overlay_path = None
        if self.lifecycle is not None:
            overlay_path = str(self.lifecycle.create_overlay(slot_id))
        return {"schema": RECEIPT_SCHEMA, "status": "LEASED",
                "generation": self.generation, "slot": slot.to_dict(),
                "overlay_path": overlay_path,
                "canonical_receipt_hash": self._canonical.receipt_hash}

    def checkpoint(self, slot_id: str, *, generation: str | None = None) -> dict[str, Any]:
        slot = self._slots.get(str(slot_id))
        if slot is None or slot.state not in {"held", "winner"}:
            raise FastFanoutError("slot is not active")
        if generation is not None and str(generation) != self.generation:
            raise FastFanoutError("slot checkpoint generation is stale")
        durable = None
        if self.lifecycle is not None:
            durable = self.lifecycle.checkpoint(
                slot.slot_id, "slot", "PLANNED", receipts=[slot.overlay_key])
        return {"schema": RECEIPT_SCHEMA, "status": "CHECKPOINT",
                "slot_id": slot.slot_id, "generation": self.generation,
                "overlay_key": slot.overlay_key, "durable": durable}

    def record_candidate(self, slot_id: str, candidate_id: str, changeset: Mapping[str, Any], *, verified: bool) -> dict[str, Any]:
        self.checkpoint(slot_id)
        candidate_id = str(candidate_id).strip()
        if not candidate_id or candidate_id in self._candidates:
            raise FastFanoutError("candidate_id must be unique and non-empty")
        try:
            raw = validate_changeset(changeset, generation=self.generation,
                                     context_hash=self.context_hash, require_binding=True)
        except FastIntegrationError as exc:
            raise FastFanoutError(str(exc)) from exc
        row = {"candidate_id": candidate_id, "slot_id": str(slot_id),
               "generation": self.generation, "verified": bool(verified),
               "changeset": raw, "state": "eligible" if verified else "rejected"}
        row["receipt_hash"] = _hash({k: v for k, v in row.items() if k != "changeset"})
        self._candidates[candidate_id] = row
        if self.lifecycle is not None:
            self.lifecycle.checkpoint(
                candidate_id, "candidate", "READY_TO_PROMOTE" if verified else "HELD",
                receipts=[row["receipt_hash"]], work_units=len(raw.get("changes", [])))
        self._metrics["candidate_count"] += 1
        return {"schema": RECEIPT_SCHEMA, "status": "RECORDED",
                "candidate": {k: v for k, v in row.items() if k != "changeset"}}

    def select_winner(self) -> dict[str, Any]:
        eligible = [row for row in self._candidates.values()
                    if row.get("verified") is True and row.get("generation") == self.generation]
        eligible.sort(key=lambda row: str(row["candidate_id"]))
        winner = eligible[0] if eligible else None
        self._winner = str(winner["candidate_id"]) if winner else None
        return {"schema": RECEIPT_SCHEMA, "status": "WINNER_SELECTED" if winner else "BLOCKED",
                "winner": self._winner, "eligible": [row["candidate_id"] for row in eligible],
                "reason": None if winner else "no_verified_candidate",
                "generation": self.generation}

    def promote_winner(self) -> dict[str, Any]:
        if self._rollout and self._rollout.get("mode") in {"disabled", "rollback", "fallback"}:
            return {"schema": RECEIPT_SCHEMA, "status": "BLOCKED",
                    "reason": "rollout_not_promotable", "rollout": dict(self._rollout)}
        selection = self.select_winner()
        if not self._winner:
            return selection
        winner = self._candidates[self._winner]
        result = self.integration.apply(winner["changeset"], winner=True,
                                        generation=self.generation, context_hash=self.context_hash)
        if result.get("status") != "READY":
            return {"schema": RECEIPT_SCHEMA, "status": "BLOCKED",
                    "winner": self._winner, "apply": result,
                    "reason": "winner_apply_not_verified"}
        winner["state"] = "promoted"
        self._slots[winner["slot_id"]].state = "winner"
        lifecycle_receipt = None
        if self.lifecycle is not None:
            candidate_rows = sorted(
                (row for row in self._candidates.values() if row.get("generation") == self.generation),
                key=lambda row: str(row["candidate_id"]))
            specs = [CandidateSpec(str(row["candidate_id"]), 0.0 if row.get("verified") else 1.0,
                                   0.0, stalled=index == 0 and len(candidate_rows) > 1)
                     for index, row in enumerate(candidate_rows)]
            try:
                lifecycle_receipt = self.lifecycle.converge(
                    specs, expected_shards=["candidate"], cancel_callback=self._release_candidate)
            except LifecycleError as exc:
                return {"schema": RECEIPT_SCHEMA, "status": "BLOCKED", "winner": self._winner,
                        "apply": result, "reason": "checkpoint_lifecycle_failed", "error": str(exc)}
        skipped = []
        for candidate_id, row in self._candidates.items():
            if candidate_id != self._winner:
                row["state"] = "skipped"
                self._metrics["loser_skips"] += 1
                skipped.append(candidate_id)
        return {"schema": RECEIPT_SCHEMA, "status": "PROMOTED",
                "winner": self._winner, "generation": self.generation,
                "apply": result, "losers_skipped": skipped,
                "checkpoint_lifecycle": lifecycle_receipt,
                "metrics": dict(self._metrics)}

    def _release_candidate(self, candidate_id: str) -> None:
        row = self._candidates.get(str(candidate_id))
        if row is None:
            raise FastFanoutError("candidate is unknown")
        slot = self._slots.get(str(row.get("slot_id") or ""))
        if slot is None:
            raise FastFanoutError("candidate slot is unknown")
        slot.state = "released"
        row["state"] = "cancelled"

    def invalidate(self, *, source_commit: str = "") -> dict[str, Any]:
        if self._canonical is None:
            raise FastFanoutError("no canonical generation to invalidate")
        refreshed = self.integration.refresh()
        if refreshed.get("status") != "MEASURED":
            raise FastFanoutError(str(refreshed.get("reason") or "fast_refresh_not_ready"))
        try:
            prepared = self.integration.prepare("refresh Fast fan-out context")
        except FastIntegrationError as exc:
            raise FastFanoutError(str(exc)) from exc
        if prepared.get("status") != "READY":
            raise FastFanoutError(str(prepared.get("reason") or "fast_prepare_not_ready"))
        generation = str(prepared.get("generation") or refreshed.get("generation") or "")
        context_hash = str(prepared.get("context_hash") or "")
        if not generation or not context_hash:
            raise FastFanoutError("Fast refresh omitted generation or context hash")
        values = {"generation": generation, "context_hash": context_hash,
                  "source_commit": str(source_commit or _head(self.root)),
                  "plan_hash": str(prepared.get("plan_hash") or "")}
        self._canonical = CanonicalGeneration(
            **values, receipt_hash=_hash(values),
        )
        self._candidates.clear()
        self._winner = None
        self._metrics["invalidations"] += 1
        return {"schema": RECEIPT_SCHEMA, "status": "INVALIDATED",
                "canonical": self._canonical.to_dict(), "metrics": dict(self._metrics)}

    def transition_rollout(self, mode: str, *, reason: str = "") -> dict[str, Any]:
        """Record shadow/canary/disable/rollback state with Fast atomically."""
        requested = str(mode).strip().lower()
        if requested not in {"shadow", "canary", "integrated", "disable", "rollback"}:
            raise FastFanoutError("unsupported rollout mode")
        fast_mode = "fallback" if requested == "disable" else requested
        try:
            receipt = self.integration.rollout(
                fast_mode,
                generation=self.generation or None,
                reason=reason or None,
            )
        except (AttributeError, FastIntegrationError) as exc:
            raise FastFanoutError(str(exc)) from exc
        expected_status = "rolled-back" if requested == "rollback" else "accepted"
        if receipt.get("status") != expected_status:
            raise FastFanoutError(str(receipt.get("reason") or "rollout_transition_not_accepted"))
        self._rollout = {"mode": requested, "fast_mode": fast_mode,
                         "generation": self.generation or None,
                         "reason": reason or None, "receipt": receipt}
        return {"schema": RECEIPT_SCHEMA, "status": "ROLLOUT_UPDATED",
                "rollout": dict(self._rollout), "local_llm": False}

    def snapshot(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "canonical": self._canonical.to_dict() if self._canonical else None,
                "slots": [slot.to_dict() for slot in self._slots.values()],
                "candidates": list(self._candidates.values()), "winner": self._winner,
                "rollout": dict(self._rollout) if self._rollout else None,
                "metrics": dict(self._metrics), "local_llm": False}

    @classmethod
    def from_snapshot(cls, root: str | Path, snapshot: Mapping[str, Any], *,
                      integration: FastLoopIntegration | None = None) -> "FastFanoutCoordinator":
        """Restore a journal without rebuilding Fast's canonical generation."""
        if not isinstance(snapshot, Mapping) or snapshot.get("schema") != SCHEMA:
            raise FastFanoutError("invalid Fast fan-out snapshot schema")
        coordinator = cls(root, integration=integration)
        canonical = snapshot.get("canonical")
        if canonical is not None:
            if not isinstance(canonical, Mapping):
                raise FastFanoutError("snapshot canonical must be an object")
            fields = ("generation", "context_hash", "source_commit", "plan_hash", "receipt_hash")
            if any(not str(canonical.get(field) or "")
                   for field in ("generation", "context_hash", "receipt_hash")):
                raise FastFanoutError("snapshot canonical is incomplete")
            coordinator._canonical = CanonicalGeneration(
                **{field: str(canonical[field]) for field in fields})
        elif snapshot.get("slots") or snapshot.get("candidates"):
            raise FastFanoutError("snapshot without canonical generation has state")
        for raw_slot in snapshot.get("slots", []):
            if not isinstance(raw_slot, Mapping):
                raise FastFanoutError("snapshot slot must be an object")
            slot_id = str(raw_slot.get("slot_id") or "").strip()
            overlay_key = str(raw_slot.get("overlay_key") or "")
            state = str(raw_slot.get("state") or "held")
            dirty_files = raw_slot.get("dirty_files", [])
            if (not slot_id or slot_id in coordinator._slots or not overlay_key or
                    state not in {"held", "winner", "released"} or
                    not isinstance(dirty_files, list)):
                raise FastFanoutError("invalid snapshot slot")
            coordinator._slots[slot_id] = _Slot(
                slot_id, overlay_key,
                tuple(str(path) for path in dirty_files), state)
        for raw_candidate in snapshot.get("candidates", []):
            if not isinstance(raw_candidate, Mapping):
                raise FastFanoutError("snapshot candidate must be an object")
            candidate_id = str(raw_candidate.get("candidate_id") or "").strip()
            generation = str(raw_candidate.get("generation") or "")
            slot_id = str(raw_candidate.get("slot_id") or "")
            changeset = raw_candidate.get("changeset")
            if (not candidate_id or candidate_id in coordinator._candidates or
                    coordinator._canonical is None or generation != coordinator.generation or
                    slot_id not in coordinator._slots or not isinstance(changeset, Mapping)):
                raise FastFanoutError("invalid snapshot candidate")
            try:
                changeset = validate_changeset(changeset, generation=coordinator.generation,
                                               context_hash=coordinator.context_hash,
                                               require_binding=True)
            except FastIntegrationError as exc:
                raise FastFanoutError(str(exc)) from exc
            row = dict(raw_candidate)
            row["candidate_id"] = candidate_id
            row["changeset"] = changeset
            coordinator._candidates[candidate_id] = row
        metrics = snapshot.get("metrics", {})
        if not isinstance(metrics, Mapping):
            raise FastFanoutError("snapshot metrics must be an object")
        for key in coordinator._metrics:
            value = metrics.get(key, 0)
            if not isinstance(value, int) or value < 0:
                raise FastFanoutError("snapshot metric must be a non-negative integer")
            coordinator._metrics[key] = value
        winner = snapshot.get("winner")
        if winner is not None and str(winner) not in coordinator._candidates:
            raise FastFanoutError("snapshot winner is unknown")
        coordinator._winner = str(winner) if winner is not None else None
        rollout = snapshot.get("rollout")
        if rollout is not None:
            if not isinstance(rollout, Mapping) or str(rollout.get("mode") or "") not in {
                    "shadow", "canary", "integrated", "disable", "rollback"}:
                raise FastFanoutError("invalid snapshot rollout")
            coordinator._rollout = dict(rollout)
        return coordinator

    @classmethod
    def restore(cls, root: str | Path, snapshot: Mapping[str, Any], *,
                integration: FastLoopIntegration | None = None) -> "FastFanoutCoordinator":
        return cls.from_snapshot(root, snapshot, integration=integration)

    def status(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "generation": self.generation,
                "active_slots": sorted(slot_id for slot_id, slot in self._slots.items() if slot.state in {"held", "winner"}),
                "winner": self._winner, "metrics": dict(self._metrics),
                "rollout": dict(self._rollout) if self._rollout else None,
                "local_llm": False}

__all__ = ["CanonicalGeneration", "FastFanoutCoordinator", "FastFanoutError",
           "RECEIPT_SCHEMA", "SCHEMA"]
