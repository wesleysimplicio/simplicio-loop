"""Shared helper for staging a real, valid #284 planning-receipt fixture in tests.

`execute_operator()`/`execute_operator_batch()` are mandatory-by-default gated on a
valid `planning-receipt.json` (see `simplicio_loop/planning_gate.py`). Any test that
exercises the real dispatch path past that gate needs a receipt on disk whose
`mutation_authority` matches the run/attempt/task-contract/plan identity it is about
to execute with -- this module builds that fixture the same way the real planning
step would (via `validate_plan()` + `build_planning_receipt()`), so tests stage a
receipt earned from a real, passing validation rather than a stub.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional

from simplicio_loop.plan_contract import validate_plan
from simplicio_loop.planning_gate import build_planning_receipt, receipt_path


def stage_valid_planning_receipt(
    *,
    repo: Any,
    run_dir: Any,
    armed_payload: Mapping[str, Any],
    run_id: str,
    source_snapshot: Optional[Mapping[str, Any]] = None,
    plan_override: Optional[Mapping[str, Any]] = None,
) -> dict:
    """Compute a passing `validate_plan()` for the run's current on-disk task-contract
    and plan, build a `simplicio.planning-receipt/v1` from it, and persist it to
    `<run_dir>/planning-receipt.json` so the mutation-authority gate accepts the
    current run/attempt/contract/plan identity. Returns the receipt dict."""
    run_dir = Path(run_dir)
    contract = json.loads((run_dir / "task-contract.json").read_text(encoding="utf-8"))
    plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
    tasks = contract.get("tasks") or []
    plan_validation = validate_plan(plan, tasks, str(repo), contract_hash=contract.get("collection_hash", ""))
    attempt = int((armed_payload.get("state") or {}).get("attempts", 0)) + 1
    plan_for_receipt = plan if plan_override is None else plan_override
    receipt = build_planning_receipt(
        run_id=run_id, attempt=attempt, contract=contract, plan=plan_for_receipt,
        plan_validation=plan_validation, source_snapshot=source_snapshot,
    )
    receipt_path(run_dir).write_text(json.dumps(receipt), encoding="utf-8")
    return receipt


__all__ = ["stage_valid_planning_receipt"]
