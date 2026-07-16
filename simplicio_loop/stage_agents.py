"""Portable Stage Agents contract (issue #423, epic #422).

Turns a role described in a skill into a concrete, typed agent instance bound
to a stage of a run's stage graph.  This module EXTENDS the generic identity
and receipt binding already provided by :mod:`simplicio_loop.agent_contract`
(role/stage/run/task/attempt/fence/plan_revision, isolation, lifecycle, and a
stage-graph reducer that only unlocks a dependent stage on a valid, fresh,
same-fence receipt).  It intentionally does not create business-specific
agents; ``contracts/stage-agents/v1/stages.json`` is the canonical, versioned
manifest of stage/role definitions that later issues extend.

Pure, stdlib-only, no implicit I/O beyond explicit ``load_*`` helpers reading
the JSON schema/manifest files under ``contracts/stage-agents/v1/``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import agent_contract

SCHEMA_ROOT = Path(__file__).resolve().parent.parent / "contracts" / "stage-agents" / "v1"

STAGE_DEFINITION_SCHEMA = "simplicio.stage-definition/v1"
ROLE_DEFINITION_SCHEMA = "simplicio.role-definition/v1"
AGENT_INSTANCE_SCHEMA = "simplicio.agent-instance/v1"
STAGE_INPUT_SCHEMA = "simplicio.stage-input/v1"
STAGE_OUTPUT_SCHEMA = "simplicio.stage-output/v1"
STAGE_RECEIPT_SCHEMA = "simplicio.stage-receipt/v1"
RUN_STAGE_GRAPH_SCHEMA = "simplicio.run-stage-graph/v1"

ISOLATION_LEVELS = frozenset(("none", "fresh-context", "separate-process", "separate-actor", "human"))
FAILURE_POLICIES = frozenset(("block", "retry", "quarantine", "human-gate"))
RECEIPT_STATUSES = frozenset(("PASSED", "FAILED", "BLOCKED", "CANCELLED", "TIMED_OUT", "STALE"))
LIFECYCLE_STATUSES = ("created", "ready", "running", "passed", "failed", "blocked", "cancelled", "timed_out")
TERMINAL_STATUSES = frozenset(("passed", "failed", "blocked", "cancelled", "timed_out"))
LEGACY_RECEIPT_SCHEMA = agent_contract.RECEIPT_SCHEMA  # "simplicio.agent-receipt/v1"
LEGACY_STATUS = "legacy-unbound"

_SCHEMA_FILES = {
    STAGE_DEFINITION_SCHEMA: "stage-definition.schema.json",
    ROLE_DEFINITION_SCHEMA: "role-definition.schema.json",
    AGENT_INSTANCE_SCHEMA: "agent-instance.schema.json",
    STAGE_INPUT_SCHEMA: "stage-input.schema.json",
    STAGE_OUTPUT_SCHEMA: "stage-output.schema.json",
    STAGE_RECEIPT_SCHEMA: "stage-receipt.schema.json",
    RUN_STAGE_GRAPH_SCHEMA: "run-stage-graph.schema.json",
}


class StageAgentError(ValueError):
    """Raised on any schema violation or lifecycle/graph invariant breach."""

    def __init__(self, message: str, *, reason_code: str = "invalid"):
        super().__init__(message)
        self.reason_code = reason_code


# --------------------------------------------------------------------------
# Minimal, dependency-free JSON Schema subset validator.
#
# Supports: type, const, enum, required, properties, additionalProperties,
# items, pattern, minimum/maximum. This is deliberately a subset (not a full
# draft-2020-12 implementation) sized to exactly what the seven stage-agent
# schemas use, keeping the repo's stdlib-only posture (CLAUDE.md).
# --------------------------------------------------------------------------

_TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
}


def _schema_validate(instance: Any, schema: Mapping[str, Any], *, path: str = "$") -> list[str]:
    errors: list[str] = []
    if "const" in schema:
        if instance != schema["const"]:
            errors.append(f"{path}: expected const {schema['const']!r}, got {instance!r}")
        return errors
    if "enum" in schema:
        if instance not in schema["enum"]:
            errors.append(f"{path}: {instance!r} not in enum {schema['enum']}")
        return errors
    schema_type = schema.get("type")
    if schema_type:
        expected = _TYPE_MAP.get(schema_type)
        if expected is not None:
            if schema_type == "integer" and isinstance(instance, bool):
                errors.append(f"{path}: expected integer, got bool")
            elif schema_type == "number" and isinstance(instance, bool):
                errors.append(f"{path}: expected number, got bool")
            elif not isinstance(instance, expected):
                errors.append(f"{path}: expected {schema_type}, got {type(instance).__name__}")
                return errors
    if schema_type == "object" and isinstance(instance, dict):
        required = schema.get("required", ())
        missing = [field for field in required if field not in instance]
        if missing:
            errors.append(f"{path}: missing required field(s): {', '.join(missing)}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = sorted(set(instance.keys()) - set(properties.keys()))
            if extra:
                errors.append(f"{path}: unknown field(s) not allowed: {', '.join(extra)}")
        for key, sub_schema in properties.items():
            if key in instance:
                errors.extend(_schema_validate(instance[key], sub_schema, path=f"{path}.{key}"))
    elif schema_type == "array" and isinstance(instance, list):
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(instance):
                errors.extend(_schema_validate(item, item_schema, path=f"{path}[{index}]"))
    elif schema_type == "string" and isinstance(instance, str):
        pattern = schema.get("pattern")
        if pattern and not re.match(pattern, instance):
            errors.append(f"{path}: {instance!r} does not match pattern {pattern!r}")
    elif schema_type in ("integer", "number") and isinstance(instance, (int, float)):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: {instance} < minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: {instance} > maximum {schema['maximum']}")
    return errors


_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}


def load_schema(schema_name: str) -> dict[str, Any]:
    """Load and cache one of the seven canonical stage-agent JSON schemas."""
    if schema_name not in _SCHEMA_FILES:
        raise StageAgentError(f"unknown schema: {schema_name}", reason_code="unknown_schema")
    if schema_name not in _SCHEMA_CACHE:
        schema_path = SCHEMA_ROOT / _SCHEMA_FILES[schema_name]
        _SCHEMA_CACHE[schema_name] = json.loads(schema_path.read_text(encoding="utf-8"))
    return _SCHEMA_CACHE[schema_name]


def validate_against_schema(instance: Mapping[str, Any], schema_name: str) -> dict[str, Any]:
    """Validate ``instance`` against one of the canonical schemas. Fails closed."""
    schema = load_schema(schema_name)
    errors = _schema_validate(dict(instance), schema)
    if errors:
        raise StageAgentError(
            f"{schema_name} validation failed: " + "; ".join(errors), reason_code="schema_violation"
        )
    return dict(instance)


# --------------------------------------------------------------------------
# stages.json manifest: parsing + validation.
# --------------------------------------------------------------------------


def load_manifest(manifest_path: Path | None = None) -> dict[str, Any]:
    path = manifest_path or (SCHEMA_ROOT / "stages.json")
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    if manifest.get("schema") != "simplicio.stages-manifest/v1":
        raise StageAgentError("stages.json has unsupported schema", reason_code="unsupported_manifest_schema")
    roles = manifest.get("roles", [])
    stages = manifest.get("stages", [])
    for role in roles:
        validate_against_schema(role, ROLE_DEFINITION_SCHEMA)
    role_ids = {role["role_id"] for role in roles}
    stage_ids = set()
    for stage in stages:
        validate_against_schema(stage, STAGE_DEFINITION_SCHEMA)
        if stage["stage_id"] in stage_ids:
            raise StageAgentError(f"duplicate stage_id: {stage['stage_id']}", reason_code="duplicate_stage")
        stage_ids.add(stage["stage_id"])
        if stage["role_id"] not in role_ids:
            raise StageAgentError(
                f"stage {stage['stage_id']} references unknown role {stage['role_id']}",
                reason_code="unknown_role",
            )
    for stage in stages:
        for dep in stage.get("depends_on", ()):
            if dep not in stage_ids:
                raise StageAgentError(
                    f"stage {stage['stage_id']} depends on unknown stage {dep}",
                    reason_code="unknown_dependency",
                )
        for nxt in stage.get("next_stages", ()):
            if nxt not in stage_ids:
                raise StageAgentError(
                    f"stage {stage['stage_id']} names unknown next_stage {nxt}",
                    reason_code="unknown_dependency",
                )
    _detect_cycle({stage["stage_id"]: stage.get("depends_on", ()) for stage in stages})
    return dict(manifest)


def _detect_cycle(depends_on_by_stage: Mapping[str, Sequence[str]]) -> None:
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {stage_id: WHITE for stage_id in depends_on_by_stage}

    def visit(stage_id: str, chain: list[str]) -> None:
        color[stage_id] = GRAY
        for dep in depends_on_by_stage.get(stage_id, ()):
            if color.get(dep) == GRAY:
                raise StageAgentError(
                    f"dependency cycle detected: {' -> '.join(chain + [stage_id, dep])}",
                    reason_code="cycle_detected",
                )
            if color.get(dep) == WHITE:
                visit(dep, chain + [stage_id])
        color[stage_id] = BLACK

    for stage_id in depends_on_by_stage:
        if color[stage_id] == WHITE:
            visit(stage_id, [])


def stage_by_id(manifest: Mapping[str, Any], stage_id: str) -> dict[str, Any]:
    for stage in manifest.get("stages", ()):
        if stage["stage_id"] == stage_id:
            return dict(stage)
    raise StageAgentError(f"unknown stage_id: {stage_id}", reason_code="unknown_stage")


def role_by_id(manifest: Mapping[str, Any], role_id: str) -> dict[str, Any]:
    for role in manifest.get("roles", ()):
        if role["role_id"] == role_id:
            return dict(role)
    raise StageAgentError(f"unknown role_id: {role_id}", reason_code="unknown_role")


# --------------------------------------------------------------------------
# agent_contract.py extension: run/task/attempt/fence/revision + role/stage.
# --------------------------------------------------------------------------

STAGE_IDENTITY_FIELDS = (
    "role_id", "role_version", "stage_id", "stage_version",
    "run_id", "task_id", "attempt_id", "fence", "plan_revision",
)


def build_stage_context(
    *, base_context_pack: Mapping[str, Any], role_id: str, role_version: str,
    stage_id: str, stage_version: str, run_id: str, attempt_id: str,
    fence: int, plan_revision: int, isolation_level: str,
) -> dict[str, Any]:
    """Extend a validated ``agent_contract`` context pack with stage identity.

    Does not replace :func:`agent_contract.build_context_pack` / ``validate_context_pack``
    — it wraps their already-validated output with the additional fields this
    issue requires, so both contracts remain single-sourced.
    """
    if isolation_level not in ISOLATION_LEVELS:
        raise StageAgentError(f"unknown isolation_level: {isolation_level}", reason_code="invalid_isolation")
    context = dict(base_context_pack)
    extension = {
        "role_id": str(role_id).strip(),
        "role_version": str(role_version).strip(),
        "stage_id": str(stage_id).strip(),
        "stage_version": str(stage_version).strip(),
        "run_id": str(run_id).strip(),
        "attempt_id": str(attempt_id).strip(),
        "fence": int(fence),
        "plan_revision": int(plan_revision),
        "isolation_level": isolation_level,
    }
    missing = [key for key, value in extension.items() if value == "" ]
    if missing:
        raise StageAgentError("stage context missing: " + ", ".join(missing), reason_code="missing_field")
    if extension["fence"] < 0 or extension["plan_revision"] < 0:
        raise StageAgentError("fence and plan_revision must be >= 0", reason_code="invalid_field")
    context.update(extension)
    return context


def bind_stage_receipt(
    receipt: Mapping[str, Any], identity: Mapping[str, Any], *,
    stage_context: Mapping[str, Any], is_separate_actor_author: bool = False,
) -> dict[str, Any]:
    """Extend :func:`agent_contract.bind_receipt` with stage/role/fence binding.

    Invariants enforced (issue #423):
      1. A receipt without role_id/stage_id does not authorize a transition.
      2. A receipt is rejected if it names a different run/task/attempt/fence/revision.
      3. The implementer cannot author a receipt for a role marked separate-actor.
    """
    result = agent_contract.bind_receipt(receipt, identity, context_pack=None)
    missing = [field for field in STAGE_IDENTITY_FIELDS if not str(stage_context.get(field, "")).strip()
               and stage_context.get(field) != 0]
    if missing:
        raise StageAgentError(
            "stage receipt missing role/stage identity: " + ", ".join(missing),
            reason_code="missing_stage_identity",
        )
    isolation_level = stage_context.get("isolation_level")
    if isolation_level == "separate-actor" and is_separate_actor_author is False:
        raise StageAgentError(
            "receipt for a separate-actor stage must be authored by a distinct actor",
            reason_code="separate_actor_violation",
        )
    for field in STAGE_IDENTITY_FIELDS:
        result[field] = stage_context[field]
    result["isolation_level"] = isolation_level
    return result


def check_receipt_freshness(receipt: Mapping[str, Any], *, expected: Mapping[str, Any]) -> None:
    """Reject a receipt from another run/task/attempt/fence/plan_revision (invariant 2)."""
    for field in ("run_id", "task_id", "attempt_id", "fence", "plan_revision"):
        if field not in expected:
            continue
        if receipt.get(field) != expected[field]:
            raise StageAgentError(
                f"stale receipt: {field} mismatch (receipt={receipt.get(field)!r}, "
                f"expected={expected[field]!r})",
                reason_code="stale_receipt",
            )


def classify_receipt_schema(receipt: Mapping[str, Any]) -> str:
    """Return the receipt's schema, mapping the legacy schema to its label (invariant 10)."""
    schema = receipt.get("schema")
    if schema == LEGACY_RECEIPT_SCHEMA:
        return LEGACY_STATUS
    if schema == STAGE_RECEIPT_SCHEMA:
        return STAGE_RECEIPT_SCHEMA
    raise StageAgentError(f"unrecognized receipt schema: {schema!r}", reason_code="unknown_receipt_schema")


# --------------------------------------------------------------------------
# Stage graph reducer: only accepted receipts unlock dependent stages.
# --------------------------------------------------------------------------


class StageGraphState:
    """Deterministic reducer over a stage graph + a stream of stage receipts.

    Replay is idempotent: feeding the same accepted receipt twice does not
    change state. Only a receipt with ``status == PASSED`` for the *current*
    fence/plan_revision of a stage unlocks its dependents (invariant 9).
    """

    def __init__(self, manifest: Mapping[str, Any], *, run_id: str, task_id: str,
                 expected_attempts: Mapping[str, str] | None = None):
        self.manifest = validate_manifest(manifest)
        self.run_id = run_id
        self.task_id = task_id
        self.passed_stages: dict[str, dict[str, Any]] = {}
        self.rejected: list[dict[str, Any]] = []
        # Per-stage expected attempt_id (invariant 2/5: a receipt from another attempt is
        # stale/rejected). Seeded from `expected_attempts` when the caller already knows the
        # authoritative attempt (e.g. from the stage input it handed out); otherwise the FIRST
        # receipt seen for a stage establishes the fence for that stage, and every later receipt
        # for that same stage is checked against it — never against its own value.
        self._stage_attempt: dict[str, str] = dict(expected_attempts or {})

    def _stage(self, stage_id: str) -> dict[str, Any]:
        return stage_by_id(self.manifest, stage_id)

    def is_unlocked(self, stage_id: str) -> bool:
        stage = self._stage(stage_id)
        return all(dep in self.passed_stages for dep in stage.get("depends_on", ()))

    def apply_receipt(self, receipt: Mapping[str, Any], *, fence: int, plan_revision: int) -> bool:
        """Apply one stage-receipt. Returns True iff it newly unlocks progress."""
        stage_id = receipt.get("stage_id")
        try:
            stage = self._stage(stage_id)
        except StageAgentError:
            self.rejected.append({"receipt": dict(receipt), "reason_code": "unknown_stage"})
            return False
        # The expected attempt_id for this stage is fixed the first time a receipt is seen for
        # it (or pre-seeded via `expected_attempts`) — never re-derived from the receipt under
        # test, or a receipt could never be rejected for naming the wrong attempt (invariant 2).
        expected_attempt_id = self._stage_attempt.setdefault(stage_id, receipt.get("attempt_id"))
        try:
            check_receipt_freshness(
                receipt,
                expected={
                    "run_id": self.run_id, "task_id": self.task_id,
                    "fence": fence, "plan_revision": plan_revision,
                    "attempt_id": expected_attempt_id,
                },
            )
        except StageAgentError as exc:
            self.rejected.append({"receipt": dict(receipt), "reason_code": exc.reason_code})
            return False
        if not self.is_unlocked(stage_id):
            self.rejected.append({"receipt": dict(receipt), "reason_code": "dependency_skip"})
            return False
        if receipt.get("status") != "PASSED":
            self.rejected.append({"receipt": dict(receipt), "reason_code": "not_passed"})
            return False
        if stage_id in self.passed_stages:
            return False  # idempotent replay: already accepted, no state change
        self.passed_stages[stage_id] = dict(receipt)
        return True

    def terminal_reached(self) -> bool:
        """True only when every stage with empty next_stages has PASSED."""
        terminal_ids = [s["stage_id"] for s in self.manifest.get("stages", ()) if not s.get("next_stages")]
        return bool(terminal_ids) and all(sid in self.passed_stages for sid in terminal_ids)

    def unlocked_ready_stages(self) -> list[str]:
        return [
            stage["stage_id"] for stage in self.manifest.get("stages", ())
            if stage["stage_id"] not in self.passed_stages and self.is_unlocked(stage["stage_id"])
        ]


def build_run_stage_graph(manifest: Mapping[str, Any], *, run_id: str, task_id: str,
                          generated_at: str, source_manifest_hash: str) -> dict[str, Any]:
    validated = validate_manifest(manifest)
    stages = [stage["stage_id"] for stage in validated.get("stages", ())]
    edges = [
        {"from": dep, "to": stage["stage_id"]}
        for stage in validated.get("stages", ())
        for dep in stage.get("depends_on", ())
    ]
    graph = {
        "schema": RUN_STAGE_GRAPH_SCHEMA,
        "run_id": run_id,
        "task_id": task_id,
        "stages": stages,
        "edges": edges,
        "generated_at": generated_at,
        "source_manifest_hash": source_manifest_hash,
    }
    return validate_against_schema(graph, RUN_STAGE_GRAPH_SCHEMA)


__all__ = [
    "AGENT_INSTANCE_SCHEMA", "FAILURE_POLICIES", "ISOLATION_LEVELS", "LEGACY_RECEIPT_SCHEMA",
    "LEGACY_STATUS", "RECEIPT_STATUSES", "ROLE_DEFINITION_SCHEMA", "RUN_STAGE_GRAPH_SCHEMA",
    "STAGE_DEFINITION_SCHEMA", "STAGE_IDENTITY_FIELDS", "STAGE_INPUT_SCHEMA", "STAGE_OUTPUT_SCHEMA",
    "STAGE_RECEIPT_SCHEMA", "TERMINAL_STATUSES", "StageAgentError", "StageGraphState",
    "bind_stage_receipt", "build_run_stage_graph", "build_stage_context", "check_receipt_freshness",
    "classify_receipt_schema", "load_manifest", "load_schema", "role_by_id", "stage_by_id",
    "validate_against_schema", "validate_manifest",
]
