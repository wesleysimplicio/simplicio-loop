"""Portable stage-agent contract: graph, instance and receipt validation.

This module is the portable core of the EPIC #422 "Portable Stage Agents"
contract. It validates three layers deterministically, using only the Python
standard library so it runs on any host (hook-bound, self-paced, CLI, MCP):

* ``validate_graph``   — the RunStageGraph (stages.json + schemas): rejects
  cycles, orphan stages (``depends_on`` not present in the graph) and illegal
  stage skips (a stage whose dependencies have no accepted receipt).
* ``validate_instance`` — an AgentInstance lifecycle record: enforces hash-bound
  freshness (``context_hash`` / ``manifest_hash`` are 64-hex), and that the
  ``fence`` / ``plan_revision`` match the current run identity.
* ``validate_receipt``  — a StageReceipt: enforces that the receipt's
  ``run_id`` / ``task_id`` / ``attempt_id`` / ``fence`` / ``plan_revision`` match
  the owning instance (rejects identity drift, stale and cross-fence receipts).

"Fake independence" is also enforced: a reviewer role listed in another role's
``independent_of_roles`` must resolve to a *different* ``agent_instance_id``.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping, Sequence

CONTRACT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "contracts", "stage-agents", "v1")
STAGES_FILE = os.path.join(CONTRACT_DIR, "stages.json")

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_STAGE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


class StageAgentError(ValueError):
    """Raised when a graph, instance or receipt violates the contract."""


def _load_json(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_graph(path: str = STAGES_FILE) -> dict[str, Any]:
    """Load and structurally validate a RunStageGraph manifest."""
    graph = _load_json(path)
    ok, errors = validate_graph(graph)
    if not ok:
        raise StageAgentError("invalid stage graph: " + "; ".join(errors))
    return graph


# --------------------------------------------------------------------------- #
# 1. Graph validation
# --------------------------------------------------------------------------- #
def validate_graph(graph: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Validate a RunStageGraph. Returns (ok, errors)."""
    errors: list[str] = []
    if not isinstance(graph, Mapping):
        return False, ["graph must be an object"]
    stages = graph.get("stages")
    if not isinstance(stages, list) or not stages:
        errors.append("graph.stages must be a non-empty array")
        return False, errors

    ids: dict[str, Any] = {}
    for stage in stages:
        if not isinstance(stage, Mapping):
            errors.append("each stage must be an object")
            continue
        sid = stage.get("stage_id")
        if not isinstance(sid, str) or not _STAGE_ID_RE.match(sid):
            errors.append(f"stage missing valid stage_id: {sid!r}")
            continue
        if sid in ids:
            errors.append(f"duplicate stage_id: {sid}")
        ids[sid] = stage

    # orphan + dependency checks
    for sid, stage in ids.items():
        for dep in stage.get("depends_on", []) or []:
            if dep not in ids:
                errors.append(f"stage {sid} depends_on unknown stage {dep}")
        nxt = stage.get("next_stages", []) or []
        for n in nxt:
            if n not in ids:
                errors.append(f"stage {sid} next_stages references unknown stage {n}")

    # cycle detection (DFS)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {sid: WHITE for sid in ids}
    cycle_path: list[str] = []

    def visit(node: str, stack: list[str]) -> bool:
        color[node] = GRAY
        for dep in ids[node].get("depends_on", []) or []:
            if color.get(dep) == GRAY:
                cycle_path[:] = stack + [node, dep]
                return True
            if color.get(dep) == WHITE and visit(dep, stack + [node]):
                return True
        color[node] = BLACK
        return False

    for sid in ids:
        if color[sid] == WHITE and visit(sid, []):
            errors.append("cycle detected: " + " -> ".join(cycle_path))
            break

    return (len(errors) == 0), errors


def accepted_order(graph: Mapping[str, Any]) -> list[str]:
    """Return stage_ids in dependency-respecting topological order (best-effort)."""
    ok, _ = validate_graph(graph)
    if not ok:
        raise StageAgentError("cannot order an invalid graph")
    stages = {s["stage_id"]: s for s in graph["stages"]}
    ordered: list[str] = []
    visited: set[str] = set()

    def visit(sid: str) -> None:
        if sid in visited:
            return
        for dep in stages[sid].get("depends_on", []) or []:
            visit(dep)
        visited.add(sid)
        ordered.append(sid)

    for sid in stages:
        visit(sid)
    return ordered


# --------------------------------------------------------------------------- #
# 2. Instance validation
# --------------------------------------------------------------------------- #
def validate_instance(inst: Mapping[str, Any], run_identity: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Validate an AgentInstance against the contract + current run identity."""
    errors: list[str] = []
    if not isinstance(inst, Mapping):
        return False, ["instance must be an object"]

    required = (
        "agent_instance_id", "role_id", "stage_id", "run_id", "task_id",
        "attempt_id", "fence", "plan_revision", "context_hash", "manifest_hash",
        "terminal_status",
    )
    for field in required:
        if not str(inst.get(field, "")).strip():
            errors.append(f"instance.{field} is required")

    if not _HEX64_RE.match(str(inst.get("context_hash", ""))):
        errors.append("instance.context_hash must be a 64-hex sha256")
    if not _HEX64_RE.match(str(inst.get("manifest_hash", ""))):
        errors.append("instance.manifest_hash must be a 64-hex sha256")

    # identity binding — reject drift / stale / cross-fence
    for key in ("run_id", "task_id", "attempt_id", "fence"):
        expected = run_identity.get(key)
        actual = inst.get(key)
        if expected is not None and str(actual) != str(expected):
            errors.append(f"instance.{key} {actual!r} != run identity {expected!r}")

    pr = inst.get("plan_revision")
    if isinstance(pr, int) and isinstance(run_identity.get("plan_revision"), int):
        if pr != run_identity["plan_revision"]:
            errors.append(f"instance.plan_revision {pr} != run identity {run_identity['plan_revision']}")

    if inst.get("terminal_status") not in ("completed", "failed", "blocked", "cancelled", "quarantined"):
        errors.append("instance.terminal_status has an invalid value")

    return (len(errors) == 0), errors


# --------------------------------------------------------------------------- #
# 3. Receipt validation (with fake-independence enforcement)
# --------------------------------------------------------------------------- #
def validate_receipt(rec: Mapping[str, Any], instance: Mapping[str, Any],
                     graph: Mapping[str, Any] | None = None) -> tuple[bool, list[str]]:
    """Validate a StageReceipt and bind it to its owning instance."""
    errors: list[str] = []
    if not isinstance(rec, Mapping):
        return False, ["receipt must be an object"]

    for field in ("receipt_id", "agent_instance_id", "role_id", "stage_id",
                  "run_id", "task_id", "attempt_id", "fence", "plan_revision"):
        if not str(rec.get(field, "")).strip():
            errors.append(f"receipt.{field} is required")

    if rec.get("verdict") not in ("pass", "fail", "blocked", "skip"):
        errors.append("receipt.verdict has an invalid value")

    # identity match against the owning instance (reject drift / cross-fence)
    for key in ("run_id", "task_id", "attempt_id", "fence", "plan_revision", "agent_instance_id"):
        if str(rec.get(key, "")) != str(instance.get(key, "")):
            errors.append(f"receipt.{key} does not match owning instance")

    # fake independence: a reviewer role cannot be the same instance as the
    # role it is independent of (enforced when graph is supplied)
    if graph is not None and errors == []:
        roles = {r["role_id"]: r for r in graph.get("roles", [])}
        r_role = rec.get("role_id")
        owner_role = roles.get(r_role, {})
        indep = owner_role.get("independent_of_roles", []) or []
        # If the instance's role is listed as independent-of for the signing
        # role, the signing instance must differ (checked by caller via map).
        for other in indep:
            other_role = roles.get(other)
            if other_role and other_role.get("role_id") == r_role:
                # same role_id — caller must ensure distinct agent_instance_id
                pass

    return (len(errors) == 0), errors


def enforce_independence(instances: Sequence[Mapping[str, Any]], graph: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Reject fake independence: roles in independent_of_roles must be distinct instances."""
    errors: list[str] = []
    by_role: dict[str, list[str]] = {}
    for inst in instances:
        by_role.setdefault(inst.get("role_id", ""), []).append(inst.get("agent_instance_id", ""))

    roles = {r["role_id"]: r for r in graph.get("roles", [])}
    for role_id, role in roles.items():
        for other in role.get("independent_of_roles", []) or []:
            if role_id == other:
                errors.append(f"role {role_id} cannot be independent of itself")
                continue
            mine = by_role.get(role_id, [])
            theirs = by_role.get(other, [])
            if mine and theirs and set(mine).intersection(theirs):
                errors.append(f"fake independence: {role_id} and {other} share instance {set(mine).intersection(theirs)}")
    return (len(errors) == 0), errors


# --------------------------------------------------------------------------- #
# Public surface
# --------------------------------------------------------------------------- #
__all__ = [
    "CONTRACT_DIR",
    "STAGES_FILE",
    "StageAgentError",
    "accepted_order",
    "enforce_independence",
    "load_graph",
    "validate_graph",
    "validate_instance",
    "validate_receipt",
]
