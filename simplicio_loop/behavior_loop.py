"""Explicit, gated skill-behavior feedback loop (issue #902).

Observation and measurement are local and deterministic. Promotion is always an
explicit Action Gate decision; feedback can archive a weak proposal but cannot
silently create or activate a skill.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "simplicio.loop-behavior-loop/v1"
STATES = frozenset({"proposed", "promoted", "archived"})


class BehaviorLoopError(ValueError):
    """The behavior loop input or lifecycle transition is invalid."""


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class BehaviorLoop:
    def __init__(self, path: str | Path, *, archive_threshold: float = 0.5,
                 minimum_samples: int = 3) -> None:
        if not 0 <= archive_threshold <= 1 or minimum_samples < 1:
            raise ValueError("invalid behavior loop thresholds")
        self.path = Path(path)
        self.archive_threshold = float(archive_threshold)
        self.minimum_samples = int(minimum_samples)

    def _append(self, event: Mapping[str, Any]) -> dict[str, Any]:
        row = {"schema": SCHEMA, **dict(event), "recorded_at": time.time()}
        row["event_hash"] = _hash({key: value for key, value in row.items()
                                    if key != "event_hash"})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        return row

    def events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if row.get("schema") != SCHEMA:
                    raise BehaviorLoopError("unsupported behavior loop schema")
                rows.append(row)
        return rows

    def propose(self, pattern: str, skill_digest: str, *, source: str = "feedback") -> dict[str, Any]:
        pattern = str(pattern or "").strip()
        skill_digest = str(skill_digest or "").strip()
        if not pattern or not skill_digest:
            raise BehaviorLoopError("pattern and skill_digest are required")
        proposal_id = _hash({"pattern": pattern, "skill_digest": skill_digest})
        existing = [row for row in self.events()
                    if row.get("event") == "proposal" and row.get("proposal_id") == proposal_id]
        if existing:
            return dict(existing[-1])
        return self._append({"event": "proposal", "proposal_id": proposal_id,
                             "pattern": pattern, "skill_digest": skill_digest,
                             "source": str(source), "state": "proposed",
                             "action_gate": "required_for_promotion"})

    def promote(self, proposal_id: str, *, action_gate: bool = False,
                authorization_digest: str = "") -> dict[str, Any]:
        proposal = self._proposal(proposal_id)
        if not action_gate:
            raise BehaviorLoopError("action gate required for promotion")
        if not authorization_digest:
            raise BehaviorLoopError("authorization digest required for promotion")
        return self._append({"event": "promotion", "proposal_id": proposal_id,
                             "state": "promoted", "skill_digest": proposal["skill_digest"],
                             "authorization_digest": authorization_digest})

    def feedback(self, proposal_id: str, *, accepted: bool,
                 evidence_digest: str) -> dict[str, Any]:
        self._proposal(proposal_id)
        if not evidence_digest:
            raise BehaviorLoopError("evidence digest required")
        return self._append({"event": "feedback", "proposal_id": proposal_id,
                             "accepted": bool(accepted), "evidence_digest": evidence_digest})

    def evaluate(self, proposal_id: str) -> dict[str, Any]:
        proposal = self._proposal(proposal_id)
        feedback = [row for row in self.events() if row.get("event") == "feedback"
                    and row.get("proposal_id") == proposal_id]
        accepted = sum(bool(row.get("accepted")) for row in feedback)
        rate = accepted / len(feedback) if feedback else None
        archived = len(feedback) >= self.minimum_samples and rate is not None and rate < self.archive_threshold
        state = "archived" if archived else ("promoted" if any(
            row.get("event") == "promotion" and row.get("proposal_id") == proposal_id
            for row in self.events()) else "proposed")
        if archived and proposal.get("state") != "archived":
            self._append({"event": "archive", "proposal_id": proposal_id, "state": "archived",
                         "acceptance_rate": rate, "reason_code": "acceptance_below_threshold"})
        result = {"schema": SCHEMA, "proposal_id": proposal_id, "state": state,
                  "samples": len(feedback), "accepted": accepted,
                  "acceptance_rate": rate, "archive_threshold": self.archive_threshold,
                  "minimum_samples": self.minimum_samples}
        result["receipt_hash"] = _hash(result)
        return result

    def _proposal(self, proposal_id: str) -> dict[str, Any]:
        rows = [row for row in self.events() if row.get("event") == "proposal"
                and row.get("proposal_id") == proposal_id]
        if not rows:
            raise BehaviorLoopError("unknown behavior proposal")
        return rows[-1]


__all__ = ["BehaviorLoop", "BehaviorLoopError", "SCHEMA", "STATES"]
