"""`simplicio.loop-extension/v1` manifest schema, loader and stage-graph composer.

This is the in-repo foundation for issue #557 ("Formalizar contrato de
extensoes de dominio para loop-oss e loop-marketing"): a versioned, strict
manifest schema an extension declares itself with, plus a deterministic
composer that folds an extension's declared ``stage_overlays`` onto the
Loop core's own stage graph without ever letting an extension weaken a gate
or remove a mandatory core stage.

This module does NOT migrate `simplicio-loop-oss` or `simplicio-loop-marketing`
(those are separate repos, out of reach from here) -- it defines and enforces
the contract those extensions would have to satisfy.

* ``validate_manifest(payload) -> list[str]`` -- structural + strict-unknown-field
  validation of a manifest dict against the ``simplicio.loop-extension/v1`` shape.
  Returns an empty list when valid.
* ``compose_stage_graph(core_stages, extensions) -> dict`` -- applies every
  extension's ``stage_overlays`` (``insert_before``/``insert_after``/``wrap``/
  ``refine``) onto the core stage list. Deterministic regardless of the order
  ``extensions`` is passed in (sorted by ``extension_id`` internally), rejects
  cycles, rejects removal/loss of a core ``mandatory`` stage, and rejects any
  gate-severity change that is not >= the core stage's own severity.

Contract: ``contracts/loop-extension/v1/schema.json``.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping, Sequence

SCHEMA_ID = "simplicio.loop-extension/v1"

_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

GATE_SEVERITY_LEVELS = ("off", "warn", "block", "fail_closed")
_GATE_SEVERITY_RANK = {level: rank for rank, level in enumerate(GATE_SEVERITY_LEVELS)}

_STAGE_OVERLAY_OPS = frozenset(("insert_before", "insert_after", "wrap", "refine"))

_TOP_LEVEL_FIELDS = frozenset((
    "schema", "extension_id", "name", "version", "domain", "requires_core",
    "capabilities", "source_adapters", "context_schemas", "stage_overlays",
    "role_bindings", "gates", "effect_handlers", "resource_classes",
    "receipt_schemas", "feature_flags",
))
_REQUIRED_TOP_LEVEL_FIELDS = ("schema", "extension_id", "name", "version", "domain", "requires_core")

_REQUIRES_CORE_FIELDS = frozenset(("min_version", "max_version"))
_CAPABILITIES_FIELDS = frozenset(("requires", "provides"))
_SOURCE_ADAPTER_FIELDS = frozenset(("adapter_id", "kind"))
_CONTEXT_SCHEMA_FIELDS = frozenset(("schema_id", "version", "migrations"))
_ROLE_BINDING_FIELDS = frozenset(("role_id", "specializes", "required_capabilities"))
_GATE_FIELDS = frozenset(("gate_id", "severity"))
_EFFECT_HANDLER_FIELDS = frozenset(("effect_id", "idempotent", "requires_fence_token", "requires_receipt", "compensation"))
_RESOURCE_CLASS_FIELDS = frozenset(("class_id", "concurrency_cap", "budget"))
_RECEIPT_SCHEMA_FIELDS = frozenset(("schema_id", "version"))
_FEATURE_FLAG_FIELDS = frozenset(("flag_id", "default"))
_STAGE_OVERLAY_FIELDS = frozenset(("op", "hook", "stage", "gates", "order"))
_OVERLAY_STAGE_FIELDS = frozenset(("stage_id", "depends_on", "gates", "mandatory"))

_CONTRACT_DIR_SOURCE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "contracts", "loop-extension", "v1")
_CONTRACT_DIR_PACKAGED = os.path.join(os.path.dirname(__file__), "_contracts", "loop-extension", "v1")
CONTRACT_DIR = (_CONTRACT_DIR_PACKAGED
                if os.path.isfile(os.path.join(_CONTRACT_DIR_PACKAGED, "schema.json"))
                else _CONTRACT_DIR_SOURCE)
SCHEMA_FILE = os.path.join(CONTRACT_DIR, "schema.json")


class ExtensionManifestError(ValueError):
    """Raised by callers that want a hard failure instead of an error list."""


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _unknown_fields(obj: Mapping[str, Any], allowed: frozenset, path: str) -> list[str]:
    if not isinstance(obj, Mapping):
        return [f"{path} must be an object"]
    unknown = sorted(set(obj) - allowed)
    if unknown:
        return [f"{path} contains unknown fields: {', '.join(unknown)}"]
    return []


def _validate_list_of(items: Any, path: str, field_check) -> list[str]:
    errors: list[str] = []
    if not isinstance(items, list):
        return [f"{path} must be an array"]
    for index, item in enumerate(items):
        errors.extend(field_check(item, f"{path}[{index}]"))
    return errors


def _validate_requires_core(value: Any, path: str) -> list[str]:
    errors = _unknown_fields(value, _REQUIRES_CORE_FIELDS, path)
    if errors:
        return errors
    for key in ("min_version", "max_version"):
        if key in value and not (isinstance(value[key], str) and _VERSION_RE.match(value[key])):
            errors.append(f"{path}.{key} must be a semver string")
    if "min_version" in value and "max_version" in value:
        if _semver_tuple(value["min_version"]) > _semver_tuple(value["max_version"]):
            errors.append(f"{path}: min_version must be <= max_version")
    return errors


def _semver_tuple(version: str) -> tuple:
    return tuple(int(part) for part in version.split("."))


def _validate_capabilities(value: Any, path: str) -> list[str]:
    errors = _unknown_fields(value, _CAPABILITIES_FIELDS, path)
    if errors:
        return errors
    for key in ("requires", "provides"):
        if key in value:
            if not isinstance(value[key], list) or not all(_nonempty_str(v) for v in value[key]):
                errors.append(f"{path}.{key} must be an array of non-empty strings")
    return errors


def _validate_source_adapter(item: Any, path: str) -> list[str]:
    errors = _unknown_fields(item, _SOURCE_ADAPTER_FIELDS, path)
    if errors:
        return errors
    for key in ("adapter_id", "kind"):
        if not _nonempty_str(item.get(key)):
            errors.append(f"{path}.{key} must be a non-empty string")
    return errors


def _validate_context_schema(item: Any, path: str) -> list[str]:
    errors = _unknown_fields(item, _CONTEXT_SCHEMA_FIELDS, path)
    if errors:
        return errors
    if not _nonempty_str(item.get("schema_id")):
        errors.append(f"{path}.schema_id must be a non-empty string")
    if not (isinstance(item.get("version"), str) and _VERSION_RE.match(item["version"])):
        errors.append(f"{path}.version must be a semver string")
    if "migrations" in item and not isinstance(item["migrations"], list):
        errors.append(f"{path}.migrations must be an array")
    return errors


def _validate_role_binding(item: Any, path: str) -> list[str]:
    errors = _unknown_fields(item, _ROLE_BINDING_FIELDS, path)
    if errors:
        return errors
    if not _nonempty_str(item.get("role_id")):
        errors.append(f"{path}.role_id must be a non-empty string")
    if "specializes" in item and not _nonempty_str(item["specializes"]):
        errors.append(f"{path}.specializes must be a non-empty string")
    if "required_capabilities" in item and not isinstance(item["required_capabilities"], list):
        errors.append(f"{path}.required_capabilities must be an array")
    return errors


def _validate_gate(item: Any, path: str) -> list[str]:
    errors = _unknown_fields(item, _GATE_FIELDS, path)
    if errors:
        return errors
    if not _nonempty_str(item.get("gate_id")):
        errors.append(f"{path}.gate_id must be a non-empty string")
    if item.get("severity") not in GATE_SEVERITY_LEVELS:
        errors.append(f"{path}.severity must be one of {GATE_SEVERITY_LEVELS}")
    return errors


def _validate_effect_handler(item: Any, path: str) -> list[str]:
    errors = _unknown_fields(item, _EFFECT_HANDLER_FIELDS, path)
    if errors:
        return errors
    if not _nonempty_str(item.get("effect_id")):
        errors.append(f"{path}.effect_id must be a non-empty string")
    if item.get("idempotent") is not True:
        errors.append(f"{path}.idempotent must be true -- an effect handler cannot publish without an idempotency key")
    if item.get("requires_fence_token") is not True:
        errors.append(f"{path}.requires_fence_token must be true -- an effect handler cannot publish without a fence token")
    if item.get("requires_receipt") is not True:
        errors.append(f"{path}.requires_receipt must be true -- an effect handler cannot publish without a durable receipt")
    return errors


def _validate_resource_class(item: Any, path: str) -> list[str]:
    errors = _unknown_fields(item, _RESOURCE_CLASS_FIELDS, path)
    if errors:
        return errors
    if not _nonempty_str(item.get("class_id")):
        errors.append(f"{path}.class_id must be a non-empty string")
    if "concurrency_cap" in item and not (isinstance(item["concurrency_cap"], int) and item["concurrency_cap"] >= 0):
        errors.append(f"{path}.concurrency_cap must be a non-negative integer")
    return errors


def _validate_receipt_schema(item: Any, path: str) -> list[str]:
    errors = _unknown_fields(item, _RECEIPT_SCHEMA_FIELDS, path)
    if errors:
        return errors
    if not _nonempty_str(item.get("schema_id")):
        errors.append(f"{path}.schema_id must be a non-empty string")
    if not (isinstance(item.get("version"), str) and _VERSION_RE.match(item["version"])):
        errors.append(f"{path}.version must be a semver string")
    return errors


def _validate_feature_flag(item: Any, path: str) -> list[str]:
    errors = _unknown_fields(item, _FEATURE_FLAG_FIELDS, path)
    if errors:
        return errors
    if not _nonempty_str(item.get("flag_id")):
        errors.append(f"{path}.flag_id must be a non-empty string")
    if "default" in item and not isinstance(item["default"], bool):
        errors.append(f"{path}.default must be a boolean")
    return errors


def _validate_stage_overlay(item: Any, path: str) -> list[str]:
    errors = _unknown_fields(item, _STAGE_OVERLAY_FIELDS, path)
    if errors:
        return errors
    op = item.get("op")
    if op not in _STAGE_OVERLAY_OPS:
        errors.append(f"{path}.op must be one of {sorted(_STAGE_OVERLAY_OPS)} (got {op!r})")
        return errors
    if not _nonempty_str(item.get("hook")):
        errors.append(f"{path}.hook must be a non-empty string naming the target stage")
    if op in ("insert_before", "insert_after"):
        stage = item.get("stage")
        if not isinstance(stage, Mapping):
            errors.append(f"{path}.stage is required for op={op}")
        else:
            errors.extend(_unknown_fields(stage, _OVERLAY_STAGE_FIELDS, f"{path}.stage"))
            if not _nonempty_str(stage.get("stage_id")):
                errors.append(f"{path}.stage.stage_id must be a non-empty string")
            if "depends_on" in stage and not isinstance(stage["depends_on"], list):
                errors.append(f"{path}.stage.depends_on must be an array")
            if "gates" in stage and not isinstance(stage["gates"], Mapping):
                errors.append(f"{path}.stage.gates must be an object")
            if stage.get("mandatory") is True:
                errors.append(f"{path}.stage.mandatory must not be set true by an extension-inserted stage")
    if op in ("wrap", "refine"):
        if "stage" in item:
            errors.append(f"{path}.stage is not valid for op={op}")
        if "gates" in item and not isinstance(item["gates"], Mapping):
            errors.append(f"{path}.gates must be an object")
        elif "gates" in item:
            for gate_id, severity in item["gates"].items():
                if severity not in GATE_SEVERITY_LEVELS:
                    errors.append(f"{path}.gates[{gate_id!r}] must be one of {GATE_SEVERITY_LEVELS}")
    if "order" in item and not isinstance(item["order"], int):
        errors.append(f"{path}.order must be an integer")
    return errors


def validate_manifest(payload: Any) -> list[str]:
    """Validate a `simplicio.loop-extension/v1` manifest. Empty list == valid."""
    if not isinstance(payload, Mapping):
        return ["manifest must be an object"]
    errors: list[str] = []
    unknown = sorted(set(payload) - _TOP_LEVEL_FIELDS)
    if unknown:
        errors.append("manifest contains unknown fields: " + ", ".join(unknown))
    for field in _REQUIRED_TOP_LEVEL_FIELDS:
        if field not in payload:
            errors.append(f"manifest is missing required field: {field}")
    if payload.get("schema") not in (None, SCHEMA_ID) and "schema" in payload:
        errors.append(f"manifest.schema must be {SCHEMA_ID!r} (got {payload.get('schema')!r})")
    if "extension_id" in payload and not (_nonempty_str(payload["extension_id"]) and _ID_RE.match(payload["extension_id"])):
        errors.append("manifest.extension_id must be a non-empty lower_snake identifier")
    if "name" in payload and not _nonempty_str(payload["name"]):
        errors.append("manifest.name must be a non-empty string")
    if "version" in payload and not (isinstance(payload["version"], str) and _VERSION_RE.match(payload["version"])):
        errors.append("manifest.version must be a semver string")
    if "domain" in payload and not _nonempty_str(payload["domain"]):
        errors.append("manifest.domain must be a non-empty string")
    if "requires_core" in payload:
        errors.extend(_validate_requires_core(payload["requires_core"], "manifest.requires_core"))
    if "capabilities" in payload:
        errors.extend(_validate_capabilities(payload["capabilities"], "manifest.capabilities"))
    if "source_adapters" in payload:
        errors.extend(_validate_list_of(payload["source_adapters"], "manifest.source_adapters", _validate_source_adapter))
    if "context_schemas" in payload:
        errors.extend(_validate_list_of(payload["context_schemas"], "manifest.context_schemas", _validate_context_schema))
    if "stage_overlays" in payload:
        errors.extend(_validate_list_of(payload["stage_overlays"], "manifest.stage_overlays", _validate_stage_overlay))
    if "role_bindings" in payload:
        errors.extend(_validate_list_of(payload["role_bindings"], "manifest.role_bindings", _validate_role_binding))
    if "gates" in payload:
        errors.extend(_validate_list_of(payload["gates"], "manifest.gates", _validate_gate))
    if "effect_handlers" in payload:
        errors.extend(_validate_list_of(payload["effect_handlers"], "manifest.effect_handlers", _validate_effect_handler))
    if "resource_classes" in payload:
        errors.extend(_validate_list_of(payload["resource_classes"], "manifest.resource_classes", _validate_resource_class))
    if "receipt_schemas" in payload:
        errors.extend(_validate_list_of(payload["receipt_schemas"], "manifest.receipt_schemas", _validate_receipt_schema))
    if "feature_flags" in payload:
        errors.extend(_validate_list_of(payload["feature_flags"], "manifest.feature_flags", _validate_feature_flag))
    return errors


def load_manifest(path: str) -> dict[str, Any]:
    """Load a manifest from disk and raise on any validation error."""
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    errors = validate_manifest(payload)
    if errors:
        raise ExtensionManifestError("invalid extension manifest: " + "; ".join(errors))
    return payload


# --------------------------------------------------------------------------- #
# Stage-graph composition
# --------------------------------------------------------------------------- #

def _gate_rank(severity: str) -> int:
    return _GATE_SEVERITY_RANK.get(severity, -1)


def _collect_overlay_ops(extensions: Sequence[Mapping[str, Any]]) -> list[tuple]:
    ops: list[tuple] = []
    for manifest in sorted(extensions, key=lambda m: str(m.get("extension_id", ""))):
        extension_id = str(manifest.get("extension_id", ""))
        overlays = manifest.get("stage_overlays") or []
        for index, overlay in enumerate(overlays):
            order = overlay.get("order", index)
            ops.append((order, extension_id, index, overlay))
    ops.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
    return ops


def compose_stage_graph(core_stages: Sequence[Mapping[str, Any]], extensions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Deterministically compose extension `stage_overlays` onto the core stage list.

    ``core_stages`` items: ``{"stage_id", "depends_on": [...], "mandatory": bool,
    "gates": {gate_id: severity}}``. ``extensions`` are already-validated
    `simplicio.loop-extension/v1` manifest dicts. Returns
    ``{"ok": bool, "errors": [...], "stages": [...]}`` -- composition never
    raises; callers gate on ``ok``. The result is identical regardless of the
    order ``extensions`` are passed in, since ops are sorted by
    ``(order, extension_id, declaration_index)`` before being applied.
    """
    errors: list[str] = []
    stage_order: list[str] = []
    stages: dict[str, dict[str, Any]] = {}
    mandatory_ids: set[str] = set()
    seen_ids: set[str] = set()
    for stage in core_stages:
        stage_id = stage.get("stage_id")
        if not _nonempty_str(stage_id):
            errors.append("core stage is missing a stage_id")
            continue
        if stage_id in seen_ids:
            errors.append(f"core stage_id is duplicated: {stage_id}")
            continue
        seen_ids.add(stage_id)
        stage_order.append(stage_id)
        stages[stage_id] = {
            "stage_id": stage_id,
            "depends_on": list(stage.get("depends_on") or []),
            "mandatory": bool(stage.get("mandatory", False)),
            "gates": dict(stage.get("gates") or {}),
            "wrapped_by": [],
            "refined_by": [],
        }
        if stage.get("mandatory"):
            mandatory_ids.add(stage_id)
    if errors:
        return {"ok": False, "errors": errors, "stages": []}

    for order, extension_id, index, overlay in _collect_overlay_ops(extensions):
        op = overlay.get("op")
        hook = overlay.get("hook")
        path = f"extension {extension_id!r} stage_overlays[{index}]"
        if hook not in stages:
            errors.append(f"{path}: hook target {hook!r} does not exist")
            continue
        if op in ("insert_before", "insert_after"):
            new_stage = overlay.get("stage") or {}
            new_id = new_stage.get("stage_id")
            if new_id in stages:
                errors.append(f"{path}: inserted stage_id {new_id!r} collides with an existing stage")
                continue
            depends_on = list(new_stage.get("depends_on") or [])
            if op == "insert_after" and hook not in depends_on:
                depends_on = depends_on + [hook]
            stages[new_id] = {
                "stage_id": new_id,
                "depends_on": depends_on,
                "mandatory": False,
                "gates": dict(new_stage.get("gates") or {}),
                "wrapped_by": [],
                "refined_by": [],
                "introduced_by": extension_id,
            }
            hook_pos = stage_order.index(hook)
            insert_pos = hook_pos if op == "insert_before" else hook_pos + 1
            stage_order.insert(insert_pos, new_id)
        elif op in ("wrap", "refine"):
            overlay_gates = overlay.get("gates") or {}
            target = stages[hook]
            for gate_id, new_severity in overlay_gates.items():
                current_severity = target["gates"].get(gate_id, "off")
                if _gate_rank(new_severity) < _gate_rank(current_severity):
                    errors.append(
                        f"{path}: cannot weaken gate {gate_id!r} on stage {hook!r} "
                        f"from {current_severity!r} to {new_severity!r}"
                    )
                    continue
                target["gates"][gate_id] = new_severity
            bucket = "wrapped_by" if op == "wrap" else "refined_by"
            target[bucket].append(extension_id)

    for stage_id in stage_order:
        for dep in stages[stage_id]["depends_on"]:
            if dep not in stages:
                errors.append(f"stage {stage_id!r} depends_on unknown stage {dep!r}")

    cycle_errors = _detect_cycles(stages)
    errors.extend(cycle_errors)

    composed_mandatory = {sid for sid in stage_order if stages[sid]["mandatory"]}
    if composed_mandatory != mandatory_ids:
        missing = mandatory_ids - composed_mandatory
        if missing:
            errors.append("mandatory core stages missing from composed graph: " + ", ".join(sorted(missing)))

    if errors:
        return {"ok": False, "errors": errors, "stages": []}
    return {"ok": True, "errors": [], "stages": [stages[sid] for sid in stage_order]}


def _detect_cycles(stages: Mapping[str, Mapping[str, Any]]) -> list[str]:
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {stage_id: WHITE for stage_id in stages}
    errors: list[str] = []

    def visit(stage_id: str, path: list[str]) -> None:
        if color.get(stage_id) == BLACK:
            return
        if color.get(stage_id) == GRAY:
            cycle = " -> ".join(path + [stage_id])
            errors.append(f"cycle detected in stage graph: {cycle}")
            return
        color[stage_id] = GRAY
        for dep in stages.get(stage_id, {}).get("depends_on", []):
            if dep in stages:
                visit(dep, path + [stage_id])
        color[stage_id] = BLACK

    for stage_id in stages:
        if color[stage_id] == WHITE:
            visit(stage_id, [])
    return errors
