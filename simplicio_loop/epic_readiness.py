"""Evidence-gated readiness audit for the inference-aware epic (#674)."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

SCHEMA = "simplicio.epic-readiness/v1"


def evaluate_epic_readiness(children: Iterable[Mapping[str, Any]], *, required: Iterable[int]) -> dict[str, Any]:
    """Return READY only when every required child is closed with evidence."""
    rows = {int(row.get("number")): dict(row) for row in children if str(row.get("number", "")).isdigit()}
    required_ids = sorted({int(value) for value in required})
    reasons = []
    checks = {}
    for number in required_ids:
        row = rows.get(number)
        if row is None:
            checks[str(number)] = False
            reasons.append(f"missing:{number}")
            continue
        closed = str(row.get("state", "")).upper() == "CLOSED"
        evidence = bool(row.get("merged_pr")) and bool(row.get("verification"))
        checks[str(number)] = closed and evidence
        if not closed:
            reasons.append(f"open:{number}")
        if not evidence:
            reasons.append(f"evidence:{number}")
    body = {"schema": SCHEMA, "status": "READY" if not reasons else "BLOCKED",
            "children": checks, "reasons": sorted(set(reasons)), "required": required_ids}
    body["audit_hash"] = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return body


__all__ = ["SCHEMA", "evaluate_epic_readiness"]
