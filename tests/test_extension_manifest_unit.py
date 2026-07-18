"""Tests for simplicio_loop.extension_manifest (issue #557).

Covers: valid/unknown-field manifest validation, effect_handler idempotency
enforcement, and stage-graph composition (insert_before/after/wrap/refine)
including mandatory-stage protection, gate-weakening rejection, cycle
detection, and discovery-order independence.
"""
from __future__ import annotations

import copy
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from simplicio_loop import extension_manifest as em


def _valid_manifest(**overrides):
    manifest = {
        "schema": "simplicio.loop-extension/v1",
        "extension_id": "loop_oss",
        "name": "Loop OSS extension",
        "version": "1.0.0",
        "domain": "oss",
        "requires_core": {"min_version": "1.0.0", "max_version": "2.0.0"},
        "capabilities": {"requires": ["receipts"], "provides": ["oss_review"]},
        "stage_overlays": [
            {"op": "insert_after", "hook": "intake", "stage": {"stage_id": "oss_triage"}},
        ],
        "effect_handlers": [
            {"effect_id": "post_pr_comment", "idempotent": True, "requires_fence_token": True, "requires_receipt": True},
        ],
    }
    manifest.update(overrides)
    return manifest


def test_valid_manifest_passes():
    assert em.validate_manifest(_valid_manifest()) == []


def test_unknown_top_level_field_fails():
    manifest = _valid_manifest()
    manifest["totally_unknown_field"] = True
    errors = em.validate_manifest(manifest)
    assert any("unknown fields" in e for e in errors)


def test_unknown_nested_field_fails():
    manifest = _valid_manifest()
    manifest["effect_handlers"][0]["extra_junk"] = "x"
    errors = em.validate_manifest(manifest)
    assert any("unknown fields" in e for e in errors)


def test_missing_required_field_fails():
    manifest = _valid_manifest()
    del manifest["requires_core"]
    errors = em.validate_manifest(manifest)
    assert any("requires_core" in e for e in errors)


def test_effect_handler_without_idempotency_fails():
    manifest = _valid_manifest(effect_handlers=[
        {"effect_id": "post_pr_comment", "idempotent": False, "requires_fence_token": True, "requires_receipt": True},
    ])
    errors = em.validate_manifest(manifest)
    assert any("idempotent" in e for e in errors)


def test_stage_overlay_remove_op_is_rejected():
    manifest = _valid_manifest(stage_overlays=[{"op": "remove", "hook": "intake"}])
    errors = em.validate_manifest(manifest)
    assert any("op must be one of" in e for e in errors)


def test_bad_version_format_fails():
    manifest = _valid_manifest(version="1.0")
    errors = em.validate_manifest(manifest)
    assert any("version" in e for e in errors)


_CORE_STAGES = [
    {"stage_id": "intake", "depends_on": [], "mandatory": True, "gates": {"safety": "block"}},
    {"stage_id": "implement", "depends_on": ["intake"], "mandatory": True, "gates": {"safety": "warn"}},
    {"stage_id": "review", "depends_on": ["implement"], "mandatory": True, "gates": {}},
    {"stage_id": "delivery", "depends_on": ["review"], "mandatory": True, "gates": {}},
]


def test_compose_insert_before_and_after_deterministic():
    ext_a = _valid_manifest(extension_id="ext_a", stage_overlays=[
        {"op": "insert_after", "hook": "intake", "stage": {"stage_id": "a_triage"}},
    ])
    ext_b = _valid_manifest(extension_id="ext_b", stage_overlays=[
        {"op": "insert_before", "hook": "review", "stage": {"stage_id": "b_precheck"}},
    ])
    result_1 = em.compose_stage_graph(_CORE_STAGES, [ext_a, ext_b])
    result_2 = em.compose_stage_graph(_CORE_STAGES, [ext_b, ext_a])
    assert result_1["ok"] is True
    assert [s["stage_id"] for s in result_1["stages"]] == [s["stage_id"] for s in result_2["stages"]]
    order = [s["stage_id"] for s in result_1["stages"]]
    assert order.index("intake") < order.index("a_triage") < order.index("implement")
    assert order.index("b_precheck") < order.index("review")


def test_compose_wrap_and_refine_apply_deterministically():
    ext = _valid_manifest(extension_id="ext_wrap", stage_overlays=[
        {"op": "wrap", "hook": "implement"},
        {"op": "refine", "hook": "review", "gates": {"security": "warn"}},
    ])
    result = em.compose_stage_graph(_CORE_STAGES, [ext])
    assert result["ok"] is True
    by_id = {s["stage_id"]: s for s in result["stages"]}
    assert by_id["implement"]["wrapped_by"] == ["ext_wrap"]
    assert by_id["review"]["refined_by"] == ["ext_wrap"]
    assert by_id["review"]["gates"]["security"] == "warn"


def test_compose_cannot_remove_mandatory_stage():
    core = copy.deepcopy(_CORE_STAGES)
    ext = _valid_manifest(extension_id="ext_remove", stage_overlays=[
        {"op": "insert_after", "hook": "intake", "stage": {"stage_id": "shadow_intake"}},
    ])
    result = em.compose_stage_graph(core, [ext])
    composed_ids = {s["stage_id"] for s in result["stages"]}
    assert {"intake", "implement", "review", "delivery"}.issubset(composed_ids)


def test_compose_rejects_gate_weakening():
    ext = _valid_manifest(extension_id="ext_weak", stage_overlays=[
        {"op": "refine", "hook": "intake", "gates": {"safety": "warn"}},
    ])
    result = em.compose_stage_graph(_CORE_STAGES, [ext])
    assert result["ok"] is False
    assert any("cannot weaken gate" in e for e in result["errors"])


def test_compose_allows_gate_strengthening():
    ext = _valid_manifest(extension_id="ext_strong", stage_overlays=[
        {"op": "refine", "hook": "implement", "gates": {"safety": "fail_closed"}},
    ])
    result = em.compose_stage_graph(_CORE_STAGES, [ext])
    assert result["ok"] is True
    by_id = {s["stage_id"]: s for s in result["stages"]}
    assert by_id["implement"]["gates"]["safety"] == "fail_closed"


def test_compose_detects_cycles():
    core = copy.deepcopy(_CORE_STAGES)
    ext = _valid_manifest(extension_id="ext_cycle", stage_overlays=[
        {"op": "insert_after", "hook": "intake", "stage": {"stage_id": "loopy", "depends_on": ["delivery"]}},
    ])
    ext2 = _valid_manifest(extension_id="ext_cycle2", stage_overlays=[
        {"op": "insert_after", "hook": "delivery", "stage": {"stage_id": "loopy_hook"}},
    ])
    tampered_core = copy.deepcopy(core)
    tampered_core[3]["depends_on"] = ["review", "loopy"]
    result = em.compose_stage_graph(tampered_core, [ext, ext2])
    assert result["ok"] is False
    assert any("cycle" in e for e in result["errors"])


def test_compose_rejects_unknown_hook():
    ext = _valid_manifest(extension_id="ext_bad_hook", stage_overlays=[
        {"op": "insert_after", "hook": "does_not_exist", "stage": {"stage_id": "x"}},
    ])
    result = em.compose_stage_graph(_CORE_STAGES, [ext])
    assert result["ok"] is False
    assert any("does not exist" in e for e in result["errors"])


def test_compose_rejects_colliding_stage_id():
    ext = _valid_manifest(extension_id="ext_collide", stage_overlays=[
        {"op": "insert_after", "hook": "intake", "stage": {"stage_id": "implement"}},
    ])
    result = em.compose_stage_graph(_CORE_STAGES, [ext])
    assert result["ok"] is False
    assert any("collides" in e for e in result["errors"])
