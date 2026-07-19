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

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

_SOURCE_CONTRACT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "contracts", "stage-agents", "v1")
_PACKAGED_CONTRACT_DIR = os.path.join(os.path.dirname(__file__), "_contracts", "stage-agents", "v1")
CONTRACT_DIR = (_PACKAGED_CONTRACT_DIR
                if os.path.isfile(os.path.join(_PACKAGED_CONTRACT_DIR, "stages.json"))
                else _SOURCE_CONTRACT_DIR)
STAGES_FILE = os.path.join(CONTRACT_DIR, "stages.json")

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_STAGE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_GRAPH_KEYS = frozenset(("schema", "graph_id", "version", "manifest_hash", "schema_pins", "roles", "stages"))
_ROLE_KEYS = frozenset(("schema", "role_id", "version", "title", "description", "required_capabilities", "forbidden_to_self_sign", "independent_of_roles"))
_STAGE_KEYS = frozenset(("schema", "stage_id", "role_id", "version", "description", "depends_on", "activation_condition", "required_capabilities", "optional_capabilities", "isolation_level", "input_schema", "output_schema", "accepted_receipts", "timeout_seconds", "retry_budget", "failure_policy", "allowed_mutations", "pre_gate", "post_gate", "next_stages", "compensation", "concurrency"))
# Allowed but NOT required on every stage (unlike _STAGE_KEYS, whose members
# must be present on every stage). "optional" backs completion_auditor.py's
# optional-stage-skip invariant (issue #431 "Optional skip exige condition
# receipt") -- the field was read by that consumer since #431 shipped, but
# never declared here or in stage-definition.schema.json, so any stage that
# actually set it would fail validate_graph as an "unknown field" (issue #458).
_STAGE_OPTIONAL_KEYS = frozenset(("optional",))
_INPUT_SCHEMA_REF = "simplicio.stage-input/v1"
_OUTPUT_SCHEMA_REF = "simplicio.stage-output/v1"
_RECEIPT_SCHEMA_REF = "simplicio.stage-receipt/v1"
_SCHEMA_REFS = frozenset((_INPUT_SCHEMA_REF, _OUTPUT_SCHEMA_REF, _RECEIPT_SCHEMA_REF))
_SCHEMA_FILES = {
    "simplicio.run-stage-graph/v1": "run-stage-graph.schema.json",
    "simplicio.agent-instance/v1": "agent-instance.schema.json",
    "simplicio.agent-role/v1": "agent-role.schema.json",
    "simplicio.stage-definition/v1": "stage-definition.schema.json",
    _INPUT_SCHEMA_REF: "stage-input.schema.json",
    _OUTPUT_SCHEMA_REF: "stage-output.schema.json",
    _RECEIPT_SCHEMA_REF: "stage-receipt.schema.json",
}
_PINNED_SCHEMA_REFS = frozenset(_SCHEMA_FILES)
_GRAPH_SCHEMA_REFS = frozenset((
    "simplicio.run-stage-graph/v1",
    "simplicio.agent-role/v1",
    "simplicio.stage-definition/v1",
))
_SCHEMA_AUTH_CACHE: dict[tuple[str, int, int], tuple[str, ...]] = {}
_SCHEMA_DIGEST_CACHE: dict[tuple[str, int, int], str] = {}
_SCHEMA_PIN_DIGEST_CACHE: dict[tuple[str, int, int, int], str | None] = {}
_SCHEMA_PARITY_CACHE: dict[tuple[str, str], tuple[str, ...]] = {}


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


class StageAgentError(ValueError):
    """Raised when a graph, instance or receipt violates the contract."""


def _load_json(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _schema_file_errors(contract_dir: str, reference: str) -> tuple[str, ...]:
    """Open and authenticate a referenced schema file on every validation.

    The cache key includes the file's nanosecond mtime and size, so a removed or
    altered schema is re-opened and re-authenticated rather than trusting a prior
    validation result.
    """
    filename = _SCHEMA_FILES.get(reference)
    if filename is None:
        return (f"schema reference is unknown: {reference}",)
    path = os.path.join(contract_dir, filename)
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        return (f"schema file is missing: {filename}",)
    except OSError as exc:
        return (f"schema file is unreadable: {filename} ({exc.__class__.__name__})",)
    cache_key = (path, stat.st_mtime_ns, stat.st_size)
    cached = _SCHEMA_AUTH_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        schema = _load_json(path)
    except FileNotFoundError:
        return (f"schema file is missing: {filename}",)
    except (OSError, json.JSONDecodeError) as exc:
        return (f"schema file is unreadable: {filename} ({exc.__class__.__name__})",)
    if not isinstance(schema, Mapping):
        return (f"schema file is not an object: {filename}",)
    errors: list[str] = []
    expected_id = "https://simplicio.dev/contracts/stage-agents/v1/" + filename
    if schema.get("$schema") != "http://json-schema.org/draft-07/schema#":
        errors.append(f"schema {filename} has an invalid draft declaration")
    if schema.get("$id") != expected_id:
        errors.append(f"schema {filename} has an invalid id")
    if schema.get("title") != reference:
        errors.append(f"schema {filename} has an invalid title")
    if schema.get("type") != "object":
        errors.append(f"schema {filename} must declare object type")
    if schema.get("additionalProperties") is not False:
        errors.append(f"schema {filename} must be closed (additionalProperties=false)")
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        errors.append(f"schema {filename} must declare object properties")
    else:
        schema_property = properties.get("schema")
        if not isinstance(schema_property, Mapping) or schema_property.get("const") != reference:
            errors.append(f"schema {filename} has wrong schema const")
    required = schema.get("required")
    if not isinstance(required, list) or "schema" not in required:
        errors.append(f"schema {filename} must require schema")
    result = tuple(errors)
    _SCHEMA_AUTH_CACHE[cache_key] = result
    return result


def load_graph(path: str = STAGES_FILE) -> dict[str, Any]:
    """Load and structurally validate a RunStageGraph manifest."""
    graph = _load_json(path)
    ok, errors = validate_graph(graph, contract_dir=os.path.dirname(os.path.abspath(path)))
    if not ok:
        raise StageAgentError("invalid stage graph: " + "; ".join(errors))
    return graph


# --------------------------------------------------------------------------- #
# 1. Graph validation
# --------------------------------------------------------------------------- #
def canonical_manifest_hash(graph: Mapping[str, Any], contract_dir: str | None = None) -> str:
    """Hash the graph and the exact bytes of every referenced authority schema."""
    payload = dict(graph)
    payload.pop("manifest_hash", None)
    directory = contract_dir or CONTRACT_DIR
    references: set[str] = set(_PINNED_SCHEMA_REFS)
    schema_hashes: dict[str, str | None] = {}
    for reference in sorted(ref for ref in references if isinstance(ref, str)):
        filename = _SCHEMA_FILES.get(reference)
        if filename is None:
            schema_hashes[reference] = None
            continue
        path = os.path.join(directory, filename)
        try:
            stat = os.stat(path)
            cache_key = (path, stat.st_mtime_ns, stat.st_size)
            digest = _SCHEMA_DIGEST_CACHE.get(cache_key)
            if digest is None:
                with open(path, "rb") as handle:
                    digest = hashlib.sha256(handle.read()).hexdigest()
                _SCHEMA_DIGEST_CACHE[cache_key] = digest
            schema_hashes[reference] = digest
        except OSError:
            schema_hashes[reference] = None
    payload["schema_hashes"] = schema_hashes
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _schema_pin_errors(graph: Mapping[str, Any], directory: str) -> list[str]:
    """Validate declared schema digests and production source/package parity."""
    pins = graph.get("schema_pins")
    if not isinstance(pins, Mapping):
        return ["graph.schema_pins must be an object"]
    expected = set(_PINNED_SCHEMA_REFS)
    errors: list[str] = []
    actual = set(pins)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        if missing:
            errors.append("graph.schema_pins missing references: " + ", ".join(missing))
        if unknown:
            errors.append("graph.schema_pins has unknown references: " + ", ".join(unknown))
    # AgentInstance is validated at its own boundary; graph validation only
    # needs the graph/role/stage/input/output/receipt authority set on its hot path.
    graph_references = expected - {"simplicio.agent-instance/v1"}
    digests: dict[str, str | None] = {}
    for reference in sorted(graph_references):
        filename = _SCHEMA_FILES[reference]
        path = os.path.join(directory, filename)
        try:
            stat = os.stat(path)
            cache_key = (path, stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns)
            digest = _SCHEMA_PIN_DIGEST_CACHE.get(cache_key)
            if digest is None:
                with open(path, "rb") as handle:
                    digest = hashlib.sha256(handle.read()).hexdigest()
                _SCHEMA_PIN_DIGEST_CACHE[cache_key] = digest
            digests[filename] = digest
        except OSError:
            digests[filename] = None
    for reference in sorted(graph_references):
        pin = pins.get(reference)
        if not _HEX64_RE.match(str(pin or "")):
            errors.append(f"graph.schema_pins[{reference!r}] must be a 64-hex sha256")
            continue
        filename = _SCHEMA_FILES[reference]
        actual_hash = digests[filename]
        if actual_hash is None:
            continue  # _schema_file_errors reports the authoritative I/O error.
        if pin != actual_hash:
            errors.append(f"graph.schema_pins[{reference!r}] does not match {filename}")

    # A distributable contract cannot silently diverge from its source authority.
    canonical_dirs = {os.path.abspath(_SOURCE_CONTRACT_DIR), os.path.abspath(_PACKAGED_CONTRACT_DIR)}
    if os.path.abspath(directory) in canonical_dirs and os.path.isdir(_SOURCE_CONTRACT_DIR) and os.path.isdir(_PACKAGED_CONTRACT_DIR):
        parity_key = (os.path.abspath(_SOURCE_CONTRACT_DIR), os.path.abspath(_PACKAGED_CONTRACT_DIR))
        parity_errors = _SCHEMA_PARITY_CACHE.get(parity_key)
        if parity_errors is None:
            parity_errors_list: list[str] = []
            for reference in sorted(graph_references):
                filename = _SCHEMA_FILES[reference]
                try:
                    with open(os.path.join(_SOURCE_CONTRACT_DIR, filename), "rb") as source, open(
                        os.path.join(_PACKAGED_CONTRACT_DIR, filename), "rb"
                    ) as packaged:
                        if source.read() != packaged.read():
                            parity_errors_list.append("source/package schema parity mismatch: " + filename)
                except OSError:
                    parity_errors_list.append("source/package schema parity unreadable: " + filename)
            parity_errors = tuple(parity_errors_list)
            _SCHEMA_PARITY_CACHE[parity_key] = parity_errors
        errors.extend(parity_errors)
    return errors


def validate_graph(graph: Mapping[str, Any], *, contract_dir: str | None = None) -> tuple[bool, list[str]]:
    """Validate the complete authority graph, schemas, topology and manifest pin."""
    errors: list[str] = []
    if not isinstance(graph, Mapping):
        return False, ["graph must be an object"]
    unknown_graph = sorted(set(graph) - _GRAPH_KEYS)
    if unknown_graph:
        errors.append("graph contains unknown fields: " + ", ".join(unknown_graph))
    for field in _GRAPH_KEYS:
        if field not in graph:
            errors.append(f"graph.{field} is required")
    if graph.get("schema") != "simplicio.run-stage-graph/v1":
        errors.append("graph.schema has an invalid value")
    if not _nonempty_string(graph.get("graph_id")):
        errors.append("graph.graph_id must be a non-empty string")
    if not _VERSION_RE.match(str(graph.get("version", ""))):
        errors.append("graph.version has an invalid value")
    if not _HEX64_RE.match(str(graph.get("manifest_hash", ""))):
        errors.append("graph.manifest_hash must be a 64-hex sha256")
    elif graph.get("manifest_hash") != canonical_manifest_hash(graph, contract_dir):
        errors.append("graph manifest_hash does not match canonical graph content")
    directory = contract_dir or CONTRACT_DIR
    errors.extend(_schema_pin_errors(graph, directory))

    roles = graph.get("roles")
    if not isinstance(roles, list) or not roles:
        errors.append("graph.roles must be a non-empty array")
        roles = []
    role_ids: set[str] = set()
    for role in roles:
        if not isinstance(role, Mapping):
            errors.append("each role must be an object")
            continue
        unknown = sorted(set(role) - _ROLE_KEYS)
        if unknown:
            errors.append("role contains unknown fields: " + ", ".join(unknown))
        for field in _ROLE_KEYS:
            if field not in role:
                errors.append(f"role {role.get('role_id', '<unknown>')} missing {field}")
        role_id = role.get("role_id")
        if not isinstance(role_id, str) or not _STAGE_ID_RE.match(role_id):
            errors.append(f"role missing valid role_id: {role_id!r}")
            continue
        if role_id in role_ids:
            errors.append(f"duplicate role_id: {role_id}")
        role_ids.add(role_id)
        if role.get("schema") != "simplicio.agent-role/v1":
            errors.append(f"role {role_id} has an invalid schema")
        if not _VERSION_RE.match(str(role.get("version", ""))):
            errors.append(f"role {role_id} has an invalid version")
        for field in ("title", "description"):
            if not _nonempty_string(role.get(field)):
                errors.append(f"role {role_id}.{field} must be a non-empty string")
        for field in ("required_capabilities", "forbidden_to_self_sign", "independent_of_roles"):
            values = role.get(field)
            if not isinstance(values, list) or any(not _nonempty_string(value) for value in values):
                errors.append(f"role {role_id}.{field} must be an array of non-empty strings")
    for role in roles:
        if isinstance(role, Mapping):
            for other in role.get("independent_of_roles", []) if isinstance(role.get("independent_of_roles"), list) else []:
                if other not in role_ids:
                    errors.append(f"role {role.get('role_id')} references unknown independent role {other}")

    stages = graph.get("stages")
    if not isinstance(stages, list) or not stages:
        errors.append("graph.stages must be a non-empty array")
        return False, errors
    ids: dict[str, Any] = {}
    for stage in stages:
        if not isinstance(stage, Mapping):
            errors.append("each stage must be an object")
            continue
        unknown = sorted(set(stage) - _STAGE_KEYS - _STAGE_OPTIONAL_KEYS)
        if unknown:
            errors.append("stage contains unknown fields: " + ", ".join(unknown))
        if "optional" in stage and not isinstance(stage["optional"], bool):
            errors.append(f"stage {stage.get('stage_id', '<unknown>')}.optional must be a boolean")
        for field in _STAGE_KEYS:
            if field not in stage:
                errors.append(f"stage {stage.get('stage_id', '<unknown>')} missing {field}")
        sid = stage.get("stage_id")
        if not isinstance(sid, str) or not _STAGE_ID_RE.match(sid):
            errors.append(f"stage missing valid stage_id: {sid!r}")
            continue
        if sid in ids:
            errors.append(f"duplicate stage_id: {sid}")
        ids[sid] = stage
        if stage.get("schema") != "simplicio.stage-definition/v1":
            errors.append(f"stage {sid} has an invalid schema")
        if not isinstance(stage.get("role_id"), str) or stage.get("role_id") not in role_ids:
            errors.append(f"stage {sid} references unknown role_id")
        if not _VERSION_RE.match(str(stage.get("version", ""))):
            errors.append(f"stage {sid} has an invalid version")
        for field in ("description", "activation_condition", "input_schema", "output_schema", "pre_gate", "post_gate", "compensation"):
            if not _nonempty_string(stage.get(field)):
                errors.append(f"stage {sid}.{field} must be a non-empty string")
        if stage.get("input_schema") != _INPUT_SCHEMA_REF:
            errors.append(f"stage {sid} references an invalid input schema")
        if stage.get("output_schema") != _OUTPUT_SCHEMA_REF:
            errors.append(f"stage {sid} references an invalid output schema")
        for field in ("depends_on", "next_stages", "required_capabilities", "optional_capabilities", "accepted_receipts", "allowed_mutations"):
            values = stage.get(field)
            if not isinstance(values, list) or any(not _nonempty_string(value) for value in values):
                errors.append(f"stage {sid}.{field} must be an array of non-empty strings")
        if not isinstance(stage.get("timeout_seconds"), int) or isinstance(stage.get("timeout_seconds"), bool) or stage.get("timeout_seconds", 0) < 1:
            errors.append(f"stage {sid} timeout_seconds must be positive")
        if not isinstance(stage.get("retry_budget"), int) or isinstance(stage.get("retry_budget"), bool) or stage.get("retry_budget", -1) < 0:
            errors.append(f"stage {sid} retry_budget must be non-negative")
        if stage.get("failure_policy") == "quarantine":
            errors.append(f"stage {sid} uses disabled quarantine failure policy")
        elif stage.get("failure_policy") not in ("block", "retry", "skip", "handoff"):
            errors.append(f"stage {sid} has an invalid failure policy")
        if stage.get("isolation_level") not in ("process", "session", "worker", "command", "human"):
            errors.append(f"stage {sid} has an invalid isolation_level")
        if stage.get("concurrency") not in ("serial", "independent", "wave"):
            errors.append(f"stage {sid} has an invalid concurrency")
        accepted = stage.get("accepted_receipts")
        if isinstance(accepted, list) and (not accepted or any(value != _RECEIPT_SCHEMA_REF for value in accepted)):
            errors.append(f"stage {sid}.accepted_receipts references an invalid schema")

    references: set[str] = set(_PINNED_SCHEMA_REFS)
    for stage in ids.values():
        for field in ("input_schema", "output_schema", "accepted_receipts"):
            value = stage.get(field)
            references.update(value if isinstance(value, list) else [value])
    for reference in sorted(ref for ref in references if isinstance(ref, str)):
        errors.extend(_schema_file_errors(directory, reference))

    for sid, stage in ids.items():
        depends = stage.get("depends_on")
        nxt = stage.get("next_stages")
        if not isinstance(depends, list) or not isinstance(nxt, list):
            continue
        for dep in depends:
            if dep not in ids:
                errors.append(f"stage {sid} depends_on unknown stage {dep}")
        for n in nxt:
            if n not in ids:
                errors.append(f"stage {sid} next_stages references unknown stage {n}")
    reverse: dict[str, set[str]] = {sid: set() for sid in ids}
    for sid, stage in ids.items():
        for dep in stage.get("depends_on", []) if isinstance(stage.get("depends_on"), list) else []:
            if dep in reverse:
                reverse[dep].add(sid)
    for sid, stage in ids.items():
        declared_next = set(stage.get("next_stages", [])) if isinstance(stage.get("next_stages"), list) else set()
        if declared_next != reverse.get(sid, set()):
            errors.append(f"stage {sid} next_stages does not match dependency edges")
    roots = [sid for sid, stage in ids.items() if isinstance(stage.get("depends_on"), list) and not stage["depends_on"]]
    if len(roots) != 1:
        errors.append("graph must have exactly one root stage")
    else:
        reachable: set[str] = set()
        pending = [roots[0]]
        while pending:
            current = pending.pop()
            if current in reachable:
                continue
            reachable.add(current)
            pending.extend(sorted(reverse.get(current, set()) - reachable))
        missing = sorted(set(ids) - reachable)
        if missing:
            errors.append("orphan stages are unreachable from root: " + ", ".join(missing))

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {sid: WHITE for sid in ids}
    cycle_path: list[str] = []

    def visit(node: str, stack: list[str]) -> bool:
        color[node] = GRAY
        for dep in ids[node].get("depends_on", []) if isinstance(ids[node].get("depends_on"), list) else []:
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
def make_agent_instance(
    *, agent_instance_id: str, role_id: str, stage_id: str, run_id: str, task_id: str,
    attempt_id: str, attempt_ordinal: int, fence: str, plan_revision: int,
    context_hash: str, manifest_hash: str,
    role_version: str = "1.0.0", stage_version: str = "1.0.0",
    work_item_id: str | None = None, runtime: str = "command", provider: str = "n/a",
    model: str = "n/a", driver: str = "command", parent_agent_id: str = "coordinator",
    coordinator_agent_id: str = "coordinator", parent_instance_id: str = "coordinator",
    idempotency_key: str | None = None, isolation_level: str = "command",
    negotiated_capabilities: Sequence[str] = ("receipts",),
    terminal_status: str = "completed", reason_code: str = "ok",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a fully schema-compliant ``simplicio.agent-instance/v1``.

    Mirrors :func:`make_stage_receipt`: the single place any stage-agent
    producer/harness should build an instance from, rather than hand-rolling a
    dict field-by-field (issue #458 -- ``scripts/conformance_suite.py``'s own
    synthetic instance, written before ``agent-instance.schema.json`` grew
    ``role_version``/``stage_version``/``work_item_id``/``runtime``/
    ``provider``/``model``/``driver``/``parent_agent_id``/
    ``coordinator_agent_id``/``parent_instance_id``/``idempotency_key``/
    ``isolation_level``/``ready_at``/``started_at``/``ended_at``, failed
    ``validate_instance`` for exactly the same reason every hand-rolled
    stage-receipt did).
    """
    ts = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    instance: dict[str, Any] = {
        "schema": "simplicio.agent-instance/v1",
        "agent_instance_id": str(agent_instance_id),
        "role_id": str(role_id),
        "role_version": str(role_version),
        "stage_id": str(stage_id),
        "stage_version": str(stage_version),
        "run_id": str(run_id),
        "task_id": str(task_id),
        "work_item_id": str(work_item_id or task_id),
        "attempt_id": str(attempt_id),
        "attempt_ordinal": int(attempt_ordinal),
        "fence": str(fence),
        "plan_revision": int(plan_revision),
        "runtime": str(runtime),
        "provider": str(provider),
        "model": str(model),
        "driver": str(driver),
        "parent_agent_id": str(parent_agent_id),
        "coordinator_agent_id": str(coordinator_agent_id),
        "parent_instance_id": str(parent_instance_id),
        "idempotency_key": str(idempotency_key or f"{run_id}:{stage_id}:{attempt_id}"),
        "isolation_level": str(isolation_level),
        "negotiated_capabilities": [str(c) for c in negotiated_capabilities] or ["receipts"],
        "context_hash": str(context_hash),
        "manifest_hash": str(manifest_hash),
        "created_at": ts,
        "ready_at": ts,
        "terminal_status": str(terminal_status),
        "reason_code": str(reason_code),
    }
    if terminal_status != "ready":
        instance["started_at"] = ts
    if terminal_status not in ("ready", "running"):
        instance["ended_at"] = ts
    return instance



def validate_instance(inst: Mapping[str, Any], run_identity: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Validate a complete AgentInstance against the contract and run identity."""
    errors: list[str] = []
    if not isinstance(inst, Mapping):
        return False, ["instance must be an object"]
    allowed = {"schema", "agent_instance_id", "role_id", "role_version", "stage_id", "stage_version", "run_id", "task_id", "work_item_id", "attempt_id", "attempt_ordinal", "fence", "plan_revision", "runtime", "provider", "model", "driver", "parent_agent_id", "coordinator_agent_id", "parent_instance_id", "idempotency_key", "isolation_level", "negotiated_capabilities", "context_hash", "manifest_hash", "created_at", "ready_at", "started_at", "ended_at", "terminal_status", "reason_code"}
    unknown = sorted(set(inst) - allowed)
    if unknown:
        errors.append("instance contains unknown fields: " + ", ".join(unknown))
    required = tuple(field for field in allowed if field not in {"started_at", "ended_at"})
    for field in required:
        value = inst.get(field)
        if field in {"plan_revision", "attempt_ordinal"}:
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                errors.append(f"instance.{field} must be a non-negative integer")
        elif field == "negotiated_capabilities":
            if not isinstance(value, list) or any(not _nonempty_string(item) for item in value):
                errors.append("instance.negotiated_capabilities must be an array of non-empty strings")
        elif field == "terminal_status":
            if value not in ("ready", "running", "completed", "failed", "blocked", "cancelled", "timed_out"):
                errors.append("instance.terminal_status has an invalid value")
        elif not _nonempty_string(value):
            errors.append(f"instance.{field} is required")
    if inst.get("schema") != "simplicio.agent-instance/v1":
        errors.append("instance.schema has an invalid value")
    if not isinstance(inst.get("attempt_ordinal"), int) or isinstance(inst.get("attempt_ordinal"), bool) or inst.get("attempt_ordinal", 0) < 1:
        errors.append("instance.attempt_ordinal must be a positive integer")
    for field in ("agent_instance_id", "role_id", "stage_id"):
        if not isinstance(inst.get(field), str) or not _STAGE_ID_RE.match(inst.get(field, "")):
            errors.append(f"instance.{field} has an invalid value")
    for field in ("role_version", "stage_version"):
        if not _VERSION_RE.match(str(inst.get(field, ""))):
            errors.append(f"instance.{field} has an invalid value")
    if inst.get("isolation_level") not in ("process", "session", "worker", "command", "human"):
        errors.append("instance.isolation_level has an invalid value")
    if not _HEX64_RE.match(str(inst.get("context_hash", ""))):
        errors.append("instance.context_hash must be a 64-hex sha256")
    if not _HEX64_RE.match(str(inst.get("manifest_hash", ""))):
        errors.append("instance.manifest_hash must be a 64-hex sha256")
    timestamps: dict[str, datetime] = {}
    for field in ("created_at", "ready_at", "started_at", "ended_at"):
        if field not in inst:
            continue
        try:
            parsed = datetime.fromisoformat(str(inst.get(field, "")).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError
            timestamps[field] = parsed
        except (TypeError, ValueError):
            errors.append(f"instance.{field} must be a timezone-aware timestamp")
    # identity binding — reject drift / stale / cross-fence
    for key in ("run_id", "task_id", "attempt_id", "attempt_ordinal", "fence"):
        expected = run_identity.get(key)
        actual = inst.get(key)
        if expected is not None and actual != expected:
            errors.append(f"instance.{key} {actual!r} != run identity {expected!r}")
    pr = inst.get("plan_revision")
    if isinstance(pr, int) and isinstance(run_identity.get("plan_revision"), int):
        if pr != run_identity["plan_revision"]:
            errors.append(f"instance.plan_revision {pr} != run identity {run_identity['plan_revision']}")
    lifecycle = inst.get("terminal_status")
    if lifecycle == "ready" and ("started_at" in inst or "ended_at" in inst):
        errors.append("ready instance cannot have started_at or ended_at")
    elif lifecycle == "running":
        if "started_at" not in inst:
            errors.append("running instance requires started_at")
        if "ended_at" in inst:
            errors.append("running instance cannot have ended_at")
    elif lifecycle in ("completed", "failed", "blocked", "cancelled", "timed_out"):
        if "started_at" not in inst:
            errors.append("terminal instance requires started_at")
        if "ended_at" not in inst:
            errors.append("terminal instance requires ended_at")
    if {"created_at", "ready_at"}.issubset(timestamps) and timestamps["ready_at"] < timestamps["created_at"]:
        errors.append("instance.ready_at must not precede created_at")
    if {"ready_at", "started_at"}.issubset(timestamps) and timestamps["started_at"] < timestamps["ready_at"]:
        errors.append("instance.started_at must not precede ready_at")
    if {"started_at", "ended_at"}.issubset(timestamps) and timestamps["ended_at"] < timestamps["started_at"]:
        errors.append("instance.ended_at must not precede started_at")
    return (len(errors) == 0), errors


# --------------------------------------------------------------------------- #
# 3. Receipt validation (with fake-independence enforcement)
# --------------------------------------------------------------------------- #
def receipt_fingerprint(receipt: Mapping[str, Any]) -> str:
    """Hash the complete canonical receipt to detect altered replays."""
    payload = json.dumps(dict(receipt), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def receipt_integrity_hash(receipt: Mapping[str, Any]) -> str:
    """Hash a receipt with its self-reported integrity field removed."""
    payload = dict(receipt)
    payload.pop("integrity_hash", None)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


DEFAULT_RECEIPT_TTL_SECONDS = 3600


def _content_hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def make_stage_receipt(
    *, receipt_id: str, agent_instance_id: str, role_id: str, stage_id: str,
    run_id: str, task_id: str, attempt_id: str, attempt_ordinal: int, fence: str,
    plan_revision: int, context_hash: str, manifest_hash: str,
    verdict: str, evidence_refs: Sequence[str] = (), reason_code: str = "ok",
    input_payload: Any = None, output_payload: Any = None,
    covered_acceptance_criteria: Sequence[str] = ("n/a",),
    commands: Sequence[str] = ("n/a",), exit_codes: Mapping[str, int] | None = None,
    artifact_refs: Sequence[str] = (), next_stage_recommendation: str = "unknown",
    previous_receipt_hashes: Sequence[str] = (), rejection_reason: str | None = None,
    supersedes_receipt_hash: str | None = None, ttl_seconds: int = DEFAULT_RECEIPT_TTL_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a fully schema-compliant ``simplicio.stage-receipt/v1``.

    The single place any stage-agent producer should build a receipt from,
    rather than hand-rolling a dict field-by-field: it computes every hash
    (``input_hash``/``output_hash``/``integrity_hash``), timestamp
    (``created_at``/``observed_at``), and default the schema requires but a
    hand-written dict tends to drift out of sync with (issue #458 --
    ``stage_agent_coordinator.py``'s own receipt path, and every per-role
    ``to_stage_receipt``-style projector written before the schema grew these
    fields in #447, failed ``validate_receipt`` for exactly this reason).
    """
    ts = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    accepted = verdict == "pass"
    receipt: dict[str, Any] = {
        "schema": "simplicio.stage-receipt/v1",
        "receipt_id": str(receipt_id),
        "agent_instance_id": str(agent_instance_id),
        "role_id": str(role_id),
        "stage_id": str(stage_id),
        "run_id": str(run_id),
        "task_id": str(task_id),
        "attempt_id": str(attempt_id),
        "attempt_ordinal": int(attempt_ordinal),
        "fence": str(fence),
        "plan_revision": int(plan_revision),
        "created_at": ts,
        "observed_at": ts,
        "ttl_seconds": int(ttl_seconds),
        "context_hash": str(context_hash),
        "manifest_hash": str(manifest_hash),
        "verdict": str(verdict),
        "evidence_refs": [str(e) for e in evidence_refs] or (["n/a"] if accepted else []),
        "accepted": accepted,
        "reason_code": str(reason_code),
        "input_hash": _content_hash(input_payload),
        "output_hash": _content_hash(output_payload),
        "previous_receipt_hashes": [str(h) for h in previous_receipt_hashes],
        "covered_acceptance_criteria": [str(a) for a in covered_acceptance_criteria],
        "commands": [str(c) for c in commands],
        "exit_codes": dict(exit_codes) if exit_codes else {},
        "artifact_refs": [str(a) for a in artifact_refs],
        "next_stage_recommendation": str(next_stage_recommendation),
    }
    if not accepted:
        receipt["rejection_reason"] = str(rejection_reason or reason_code or "not_accepted")
    if supersedes_receipt_hash:
        receipt["supersedes_receipt_hash"] = str(supersedes_receipt_hash)
    receipt["integrity_hash"] = receipt_integrity_hash(receipt)
    return receipt


def validate_receipt(rec: Mapping[str, Any], instance: Mapping[str, Any],
                     graph: Mapping[str, Any] | None = None,
                     *, now: datetime | None = None) -> tuple[bool, list[str]]:
    """Validate a complete StageReceipt, identity, owner and freshness provenance."""
    errors: list[str] = []
    if not isinstance(rec, Mapping):
        return False, ["receipt must be an object"]
    allowed = {"schema", "receipt_id", "agent_instance_id", "role_id", "stage_id",
               "run_id", "task_id", "attempt_id", "attempt_ordinal", "fence", "plan_revision",
               "created_at", "observed_at", "ttl_seconds", "context_hash",
               "manifest_hash", "verdict", "evidence_refs", "accepted", "reason_code",
               "input_hash", "output_hash", "integrity_hash", "previous_receipt_hashes",
               "covered_acceptance_criteria", "commands", "exit_codes", "artifact_refs",
               "next_stage_recommendation", "rejection_reason", "supersedes_receipt_hash"}
    unknown = sorted(set(rec) - allowed)
    if unknown:
        errors.append("receipt contains unknown fields: " + ", ".join(unknown))
    required = ("schema", "receipt_id", "agent_instance_id", "role_id", "stage_id",
                "run_id", "task_id", "attempt_id", "attempt_ordinal", "fence", "plan_revision",
                "created_at", "observed_at", "ttl_seconds", "context_hash",
                "manifest_hash", "verdict", "evidence_refs", "accepted", "reason_code",
                "input_hash", "output_hash", "integrity_hash", "previous_receipt_hashes",
                "covered_acceptance_criteria", "commands", "exit_codes", "artifact_refs",
                "next_stage_recommendation")
    for field in required:
        if field not in rec or (isinstance(rec.get(field), str) and not rec.get(field).strip()):
            errors.append(f"receipt.{field} is required")
    if rec.get("schema") != "simplicio.stage-receipt/v1":
        errors.append("receipt.schema has an invalid value")
    for field in ("receipt_id", "agent_instance_id", "role_id", "stage_id", "run_id", "task_id", "attempt_id", "fence", "created_at", "observed_at"):
        if not _nonempty_string(rec.get(field)):
            errors.append(f"receipt.{field} must be a non-empty string")
    if not isinstance(rec.get("plan_revision"), int) or isinstance(rec.get("plan_revision"), bool) or rec.get("plan_revision", -1) < 0:
        errors.append("receipt.plan_revision must be a non-negative integer")
    if not isinstance(rec.get("attempt_ordinal"), int) or isinstance(rec.get("attempt_ordinal"), bool) or rec.get("attempt_ordinal", 0) < 1:
        errors.append("receipt.attempt_ordinal must be a positive integer")
    verdict = rec.get("verdict")
    if verdict not in ("pass", "fail", "blocked", "skip", "timed_out", "cancelled", "stale"):
        errors.append("receipt.verdict has an invalid value")
    if not isinstance(rec.get("accepted"), bool):
        errors.append("receipt.accepted must be boolean")
    elif (verdict == "pass" and rec.get("accepted") is not True) or (verdict != "pass" and rec.get("accepted") is True):
        errors.append("receipt.accepted is inconsistent with verdict")
    evidence = rec.get("evidence_refs")
    if not isinstance(evidence, list) or any(not isinstance(item, str) or not item.strip() for item in evidence):
        errors.append("receipt.evidence_refs must be an array of non-empty strings")
    elif verdict == "pass" and not evidence:
        errors.append("accepted pass receipt requires evidence_refs")
    if verdict in ("fail", "blocked", "skip", "timed_out", "cancelled", "stale") and not _nonempty_string(rec.get("rejection_reason")):
        errors.append("non-pass receipt requires rejection_reason")
    expected_terminal = {
        "fail": "failed",
        "blocked": "blocked",
        "cancelled": "cancelled",
        "timed_out": "timed_out",
    }.get(verdict)
    if expected_terminal is not None and instance.get("terminal_status") != expected_terminal:
        errors.append(f"{verdict} receipt requires {expected_terminal} instance")
    for key in ("run_id", "task_id", "attempt_id", "attempt_ordinal", "fence", "plan_revision",
                "agent_instance_id", "role_id", "stage_id"):
        if str(rec.get(key, "")) != str(instance.get(key, "")):
            errors.append(f"receipt.{key} does not match owning instance")
    for field in ("context_hash", "manifest_hash", "input_hash", "output_hash", "integrity_hash"):
        if not _HEX64_RE.match(str(rec.get(field, ""))):
            errors.append(f"receipt.{field} must be a 64-hex sha256")
        if field in ("context_hash", "manifest_hash") and str(rec.get(field, "")) != str(instance.get(field, "")):
            errors.append(f"receipt.{field} does not match owning instance provenance")
    for field in ("previous_receipt_hashes", "covered_acceptance_criteria", "commands", "artifact_refs"):
        values = rec.get(field)
        if not isinstance(values, list) or any(not _nonempty_string(value) for value in values):
            errors.append(f"receipt.{field} must be an array of non-empty strings")
    if isinstance(rec.get("previous_receipt_hashes"), list) and any(not _HEX64_RE.match(str(value)) for value in rec["previous_receipt_hashes"]):
        errors.append("receipt.previous_receipt_hashes must contain 64-hex sha256 values")
    if isinstance(rec.get("previous_receipt_hashes"), list) and len(set(rec["previous_receipt_hashes"])) != len(rec["previous_receipt_hashes"]):
        errors.append("receipt.previous_receipt_hashes must not contain duplicates")
    if not isinstance(rec.get("exit_codes"), Mapping) or any(not isinstance(value, int) or isinstance(value, bool) for value in rec.get("exit_codes", {}).values()):
        errors.append("receipt.exit_codes must map commands to integer exit codes")
    if str(rec.get("integrity_hash")) != receipt_integrity_hash(rec):
        errors.append("receipt.integrity_hash does not match canonical receipt content")
    supersedes = rec.get("supersedes_receipt_hash")
    if supersedes is not None and not _HEX64_RE.match(str(supersedes)):
        errors.append("receipt.supersedes_receipt_hash must be a 64-hex sha256 when present")
    if verdict == "pass" and rec.get("accepted") is True and instance.get("terminal_status") != "completed":
        errors.append("accepted pass receipt requires completed instance")
    if graph is not None:
        if graph.get("manifest_hash") is not None:
            expected_manifest = str(graph["manifest_hash"])
            if str(rec.get("manifest_hash")) != expected_manifest or str(instance.get("manifest_hash")) != expected_manifest:
                errors.append("receipt manifest_hash does not match canonical manifest")
        stage_by_id = {s.get("stage_id"): s for s in graph.get("stages", []) if isinstance(s, Mapping)}
        stage = stage_by_id.get(rec.get("stage_id"))
        if stage is None:
            errors.append("receipt.stage_id is not present in graph")
        else:
            if rec.get("role_id") != stage.get("role_id"):
                errors.append("receipt.role_id does not own graph stage")
            required_capabilities = stage.get("required_capabilities", [])
            negotiated_capabilities = instance.get("negotiated_capabilities", [])
            if isinstance(required_capabilities, list) and isinstance(negotiated_capabilities, list):
                missing_capabilities = sorted(set(required_capabilities) - set(negotiated_capabilities))
                if missing_capabilities:
                    errors.append("instance capabilities do not satisfy stage requirements: " + ", ".join(missing_capabilities))
    try:
        created = datetime.fromisoformat(str(rec.get("created_at", "")).replace("Z", "+00:00"))
        observed = datetime.fromisoformat(str(rec.get("observed_at", "")).replace("Z", "+00:00"))
        current = now or datetime.now(timezone.utc)
        ttl = rec.get("ttl_seconds")
        if created.tzinfo is None or observed.tzinfo is None:
            raise ValueError("timestamps require timezone")
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 1:
            raise ValueError("ttl_seconds must be positive")
        if observed < created or observed > current or (observed - created).total_seconds() > ttl or (verdict != "stale" and (current - observed).total_seconds() > ttl):
            errors.append("receipt is stale or outside freshness TTL")
    except (TypeError, ValueError):
        errors.append("receipt timestamps/ttl are invalid")
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
                shared = sorted(set(mine).intersection(theirs))
                errors.append(f"fake independence: {role_id} and {other} share instance {shared}")
    return (len(errors) == 0), errors


def reduce_receipts(graph: Mapping[str, Any], receipts: Sequence[Mapping[str, Any]], instances: Sequence[Mapping[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    ok, errors = validate_graph(graph)
    if not ok:
        raise StageAgentError("cannot reduce invalid graph: " + "; ".join(errors))
    if len(receipts) != len(instances):
        raise StageAgentError("each receipt must have one owning instance")
    stages = {stage["stage_id"]: stage for stage in graph["stages"]}
    seen_receipts: dict[str, str] = {}
    stage_receipts: dict[str, list[Mapping[str, Any]]] = {}
    reduction_identity: dict[str, Any] | None = None
    for receipt, instance in zip(receipts, instances):
        valid, receipt_errors = validate_receipt(receipt, instance, graph, now=now)
        if not valid:
            raise StageAgentError("receipt rejected: " + "; ".join(receipt_errors))
        identity = {key: receipt[key] for key in ("run_id", "task_id", "attempt_id", "attempt_ordinal", "fence", "plan_revision")}
        instance_valid, instance_errors = validate_instance(instance, identity)
        if not instance_valid:
            raise StageAgentError("instance rejected: " + "; ".join(instance_errors))
        stage_id = str(receipt["stage_id"])
        if stage_id not in stages:
            raise StageAgentError("receipt references unknown stage: " + stage_id)
        shared_identity = {key: receipt[key] for key in ("run_id", "task_id", "fence", "plan_revision")}
        if reduction_identity is None:
            reduction_identity = shared_identity
        elif shared_identity != reduction_identity:
            raise StageAgentError("receipt crosses run, task, fence, or plan revision")
        receipt_id = str(receipt["receipt_id"])
        fingerprint = receipt_fingerprint(receipt)
        previous_fingerprint = seen_receipts.get(receipt_id)
        if previous_fingerprint is not None:
            if previous_fingerprint != fingerprint:
                raise StageAgentError("receipt replay changed immutable content: " + receipt_id)
            continue
        seen_receipts[receipt_id] = fingerprint
        stage_receipts.setdefault(stage_id, []).append(receipt)
    independence_ok, independence_errors = enforce_independence(instances, graph)
    if not independence_ok:
        raise StageAgentError("independence rejected: " + "; ".join(sorted(independence_errors)))

    # Retry lineage is a fail-closed sequence, independent of input ordering.
    # attempt_id is an opaque transport identity; attempt_ordinal is the sole
    # authority for budget, order and terminal supersession semantics.
    for stage_id, values in stage_receipts.items():
        attempts_by_ordinal: dict[int, Mapping[str, Any]] = {}
        attempt_ids: set[str] = set()
        owners: set[str] = set()
        for receipt in values:
            ordinal = receipt["attempt_ordinal"]
            if ordinal in attempts_by_ordinal:
                previous = attempts_by_ordinal[ordinal]
                if previous.get("agent_instance_id") != receipt.get("agent_instance_id"):
                    raise StageAgentError("attempt ordinal has conflicting agent instance: " + stage_id)
                raise StageAgentError("multiple receipts for stage without explicit retry lineage: " + stage_id)
            attempt_id = str(receipt["attempt_id"])
            owner = str(receipt["agent_instance_id"])
            if attempt_id in attempt_ids:
                raise StageAgentError("retry repeats attempt_id: " + stage_id)
            if owner in owners:
                raise StageAgentError("retry requires a distinct agent instance: " + stage_id)
            attempts_by_ordinal[ordinal] = receipt
            attempt_ids.add(attempt_id)
            owners.add(owner)
        ordinals = sorted(attempts_by_ordinal)
        if ordinals != list(range(1, len(ordinals) + 1)):
            raise StageAgentError("attempt ordinal lineage must be contiguous from 1: " + stage_id)
        if len(ordinals) > int(stages[stage_id]["retry_budget"]) + 1:
            raise StageAgentError("retry budget exceeded: " + stage_id)
        prior_blocked: Mapping[str, Any] | None = None
        for ordinal in ordinals:
            receipt = attempts_by_ordinal[ordinal]
            if prior_blocked is not None:
                if not (receipt.get("verdict") == "pass" and receipt.get("accepted") is True):
                    raise StageAgentError("blocked stage cannot be re-executed without an accepted superseding pass: " + stage_id)
                if receipt.get("supersedes_receipt_hash") != prior_blocked.get("integrity_hash"):
                    raise StageAgentError("accepted pass must explicitly supersede blocked receipt: " + stage_id)
                prior_blocked = None
            elif receipt.get("supersedes_receipt_hash") is not None:
                raise StageAgentError("supersedes_receipt_hash does not name a prior blocked receipt: " + stage_id)
            if receipt.get("verdict") == "blocked":
                prior_blocked = receipt
            if receipt.get("verdict") == "pass" and ordinal != ordinals[-1]:
                raise StageAgentError("accepted stage cannot be retried: " + stage_id)

    completed: set[str] = set()
    pass_stages = {stage_id for stage_id, values in stage_receipts.items() if any(value.get("verdict") == "pass" and value.get("accepted") is True for value in values)}
    progress = True
    while progress:
        progress = False
        for stage_id in sorted(pass_stages):
            dependencies = set(stages[stage_id].get("depends_on", []))
            if stage_id not in completed and dependencies.issubset(completed):
                completed.add(stage_id)
                progress = True
    blocked = sorted(stage_id for stage_id in pass_stages if stage_id not in completed)
    if blocked:
        missing = sorted({dependency for stage_id in blocked for dependency in stages[stage_id].get("depends_on", []) if dependency not in completed})
        raise StageAgentError("stage receipt skips unaccepted dependencies: " + ", ".join(missing))

    accepted_receipts: dict[str, Mapping[str, Any]] = {}
    for stage_id, values in stage_receipts.items():
        accepted = [value for value in values if value.get("verdict") == "pass" and value.get("accepted") is True]
        if len(accepted) > 1:
            raise StageAgentError("multiple accepted receipts for stage: " + stage_id)
        if accepted:
            accepted_receipts[stage_id] = accepted[0]
    for stage_id, receipt in accepted_receipts.items():
        dependencies = stages[stage_id].get("depends_on", [])
        expected_hashes = {accepted_receipts[dependency]["integrity_hash"] for dependency in dependencies}
        actual_hashes = set(receipt.get("previous_receipt_hashes", []))
        if actual_hashes != expected_hashes:
            raise StageAgentError("previous receipt hashes do not bind accepted dependencies: " + stage_id)

    blocked_stages = {
        stage_id for stage_id, values in stage_receipts.items()
        if stage_id not in accepted_receipts and any(value.get("verdict") != "pass" for value in values)
    }
    released = sorted(
        stage_id for stage_id, stage in stages.items()
        if stage_id not in completed
        and stage_id not in blocked_stages
        and not (set(stage.get("depends_on", [])) & blocked_stages)
        and set(stage.get("depends_on", [])).issubset(completed)
    )
    terminals = {stage_id for stage_id, stage in stages.items() if not (stage.get("next_stages", []) or [])}
    return {"schema": "simplicio.stage-reduction/v1", "completed_stages": sorted(completed), "released_stages": released, "blocked_stages": sorted(blocked_stages), "terminal": terminals.issubset(completed), "receipt_count": len(seen_receipts)}

# --------------------------------------------------------------------------- #
# Public surface
# --------------------------------------------------------------------------- #
__all__ = [
    "CONTRACT_DIR",
    "STAGES_FILE",
    "StageAgentError",
    "receipt_fingerprint",
    "receipt_integrity_hash",
    "reduce_receipts",
    "accepted_order",
    "enforce_independence",
    "load_graph",
    "validate_graph",
    "validate_instance",
    "validate_receipt",
]
