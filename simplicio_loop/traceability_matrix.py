"""AC <-> step <-> test <-> evidence matrix (#284, "matriz de testes e evidencias").

`plan_contract.validate_plan()` already fails closed when a scenario needing a
code change has no `read_paths`/`change_paths`/`test_commands` planned for it
(see `scenario_unplanned_read`/`scenario_unplanned_change`/`scenario_unplanned_test`
in `simplicio_loop/plan_contract.py`). What was missing is a persisted,
reviewable *artifact* that makes that bidirectional coverage explicit per AC —
issue #284's "matriz de testes e evidencias" table — instead of it only living
inside a pass/fail validator receipt.

This module is data-only and model-free: it re-projects the SAME task-contract
scenarios and plan steps `validate_plan()` already checked into one row per AC,
carrying the step it is planned under, the test commands planned for it, and
an `evidence_status` derived from what the plan step actually declares (never
fabricated). A scenario explicitly marked `no_code_change` is `not_applicable`
with that justification; anything else without a declared `evidence_type` is
`missing` — a real, visible gap rather than a silently-assumed pass.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Mapping, Optional

MATRIX_SCHEMA = "simplicio.ac-matrix/v1"
MATRIX_FILENAME = "ac-matrix.json"


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_hash(obj: Any) -> str:
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def build_matrix(contract: Mapping[str, Any], plan: Mapping[str, Any]) -> Dict[str, Any]:
    """Build the `simplicio.ac-matrix/v1` artifact from a task contract and its plan.

    One row per scenario (AC). `gaps` lists AC IDs that need a code change but
    carry no test command AND no declared evidence type -- callers can treat a
    non-empty `gaps` list as a hard block the same way `validate_plan()` does,
    or surface it as a reviewable warning; this module only computes the fact.
    """
    tasks = list(contract.get("tasks") or [])
    steps = list(plan.get("steps") or [])
    rows: List[Dict[str, Any]] = []
    gaps: List[str] = []

    for index, task in enumerate(tasks):
        step = steps[index] if index < len(steps) and isinstance(steps[index], Mapping) else {}
        step_id = str(step.get("id") or task.get("id") or f"T{index + 1}")
        scenario_steps: Dict[str, Mapping[str, Any]] = {}
        for s in step.get("steps") or []:
            if isinstance(s, Mapping):
                sid = str(s.get("scenario_id") or s.get("id") or "")
                if sid:
                    scenario_steps[sid] = s

        for scenario in task.get("scenarios") or []:
            sid = str(scenario.get("id") or "")
            if not sid:
                continue
            sstep = scenario_steps.get(sid, {})
            splan = dict(sstep.get("plan") or {})
            no_code_change = bool(splan.get("no_code_change"))
            test_commands = list(splan.get("test_commands") or [])
            evidence_type = str(splan.get("evidence_type") or splan.get("evidence") or "")
            if no_code_change:
                evidence_status = "not_applicable"
                evidence_justification = str(splan.get("no_code_change_reason") or "no code change planned for this AC")
            elif evidence_type:
                evidence_status = "declared"
                evidence_justification = ""
            else:
                evidence_status = "missing"
                evidence_justification = ""

            covered = bool(no_code_change or test_commands)
            if not covered:
                gaps.append(sid)

            rows.append({
                "ac_id": sid,
                "task_id": str(task.get("id") or ""),
                "step_id": step_id,
                "text": str(scenario.get("title") or ""),
                "rule_ids": list(scenario.get("rule_refs") or []),
                "read_paths": list(splan.get("read_paths") or []),
                "change_paths": list(splan.get("change_paths") or []),
                "test_commands": test_commands,
                "evidence_type": evidence_type,
                "evidence_status": evidence_status,
                "evidence_justification": evidence_justification,
                "no_code_change": no_code_change,
                "covered": covered,
            })

    matrix: Dict[str, Any] = {
        "schema": MATRIX_SCHEMA,
        "rows": rows,
        "counts": {
            "acceptance_criteria": len(rows),
            "covered": sum(1 for r in rows if r["covered"]),
            "gaps": len(gaps),
        },
        "gaps": gaps,
        "coverage_ok": not gaps,
    }
    matrix["matrix_hash"] = content_hash(matrix)
    return matrix


def matrix_path(run_dir: Any) -> Any:
    from pathlib import Path
    return Path(run_dir) / MATRIX_FILENAME


__all__ = ["MATRIX_SCHEMA", "MATRIX_FILENAME", "content_hash", "build_matrix", "matrix_path"]
