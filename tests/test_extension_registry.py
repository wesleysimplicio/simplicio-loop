"""Tests for ``simplicio_loop.extension_registry`` (issue #614, part 1)."""
from __future__ import annotations

import importlib.metadata
from unittest import mock

import pytest

from simplicio_loop.extension_manifest import validate_manifest
from simplicio_loop.extension_registry import (
    ENTRY_POINT_GROUP,
    LEGACY_ENTRY_POINT_GROUP,
    ExtensionRegistry,
    ExtensionRegistryError,
)


VALID_MANIFEST = {
    "schema": "simplicio.loop-extension/v1",
    "extension_id": "demo_ext",
    "name": "Demo Extension",
    "version": "1.0.0",
    "domain": "demo",
    "requires_core": {"min_version": "0.1.0", "max_version": "2.0.0"},
    "stage_overlays": [
        {"op": "wrap", "hook": "core_stage", "gates": {"quality": "warn"}}
    ],
}


def test_register_valid_manifest():
    reg = ExtensionRegistry()
    result = reg.register(dict(VALID_MANIFEST))
    assert result["extension_id"] == "demo_ext"
    assert len(reg) == 1
    assert reg.get("demo_ext") is result


def test_register_rejects_invalid_strict():
    reg = ExtensionRegistry()
    bad = dict(VALID_MANIFEST)
    bad.pop("domain")  # required field
    with pytest.raises(Exception):
        reg.register(bad, strict=True)
    assert len(reg) == 0


def test_register_rejects_invalid_non_strict():
    reg = ExtensionRegistry()
    bad = dict(VALID_MANIFEST)
    bad.pop("domain")
    result = reg.register(bad, strict=False)
    assert result["ok"] is False
    assert any("domain" in e for e in result["errors"])
    assert len(reg) == 0


def test_get_and_all():
    reg = ExtensionRegistry()
    reg.register(dict(VALID_MANIFEST))
    assert reg.get("demo_ext")["name"] == "Demo Extension"
    assert reg.get("missing") is None
    assert len(reg.all()) == 1


def test_register_idempotent():
    reg = ExtensionRegistry()
    reg.register(dict(VALID_MANIFEST))
    reg.register(dict(VALID_MANIFEST))
    assert len(reg) == 1


def test_clear():
    reg = ExtensionRegistry()
    reg.register(dict(VALID_MANIFEST))
    reg.clear()
    assert len(reg) == 0


def test_discover_entry_points_loads_manifest_dict():
    class FakeEP:
        name = "demo_ep"
        def load(self):
            return dict(VALID_MANIFEST)
    with mock.patch.object(importlib.metadata, "entry_points", return_value=[FakeEP()]):
        reg = ExtensionRegistry()
        found = reg.discover_entry_points()
    assert len(found) == 1
    assert found[0]["extension_id"] == "demo_ext"
    got = reg.get("demo_ext")
    assert got is not None
    assert got["extension_id"] == "demo_ext"


def test_default_entry_point_group_is_pep_compliant():
    with mock.patch.object(importlib.metadata, "entry_points", return_value=[]) as entry_points:
        ExtensionRegistry().discover_entry_points()
    assert entry_points.call_args_list == [
        mock.call(group="simplicio.loop_extension"),
        mock.call(group="simplicio.loop-extension"),
    ]
    assert ENTRY_POINT_GROUP == "simplicio.loop_extension"


def test_default_discovery_bridges_legacy_group():
    class FakeEP:
        name = "legacy_ep"
        value = "legacy:provider"
        def load(self):
            return dict(VALID_MANIFEST)

    def entry_points(*, group):
        return [FakeEP()] if group == LEGACY_ENTRY_POINT_GROUP else []

    with mock.patch.object(importlib.metadata, "entry_points", side_effect=entry_points):
        found = ExtensionRegistry().discover_entry_points()
    assert [item["extension_id"] for item in found] == ["demo_ext"]


def test_discovery_rejects_conflicting_duplicate_ids():
    conflicting = dict(VALID_MANIFEST, name="Conflicting Extension")

    class FakeEP:
        def __init__(self, name, manifest):
            self.name = name
            self.value = f"{name}:provider"
            self._manifest = manifest
        def load(self):
            return dict(self._manifest)

    def entry_points(*, group):
        if group == ENTRY_POINT_GROUP:
            return [FakeEP("canonical", VALID_MANIFEST)]
        return [FakeEP("legacy", conflicting)]

    with mock.patch.object(importlib.metadata, "entry_points", side_effect=entry_points):
        with pytest.raises(ExtensionRegistryError, match="conflicting extension registration"):
            ExtensionRegistry().discover_entry_points()


def test_discover_entry_points_callable_factory():
    class FakeEP:
        name = "demo_ep"
        def load(self):
            return lambda: dict(VALID_MANIFEST)
    with mock.patch.object(importlib.metadata, "entry_points", return_value=[FakeEP()]):
        reg = ExtensionRegistry()
        found = reg.discover_entry_points()
    assert found[0]["extension_id"] == "demo_ext"


def test_discover_entry_points_rejects_invalid():
    bad = dict(VALID_MANIFEST)
    bad.pop("domain")
    class FakeEP:
        name = "bad_ep"
        def load(self):
            return bad
    with mock.patch.object(importlib.metadata, "entry_points", return_value=[FakeEP()]):
        reg = ExtensionRegistry()
        with pytest.raises(ExtensionRegistryError):
            reg.discover_entry_points(strict=True)


def test_discover_entry_points_non_strict_collects_errors():
    bad = dict(VALID_MANIFEST)
    bad.pop("domain")
    class FakeEP:
        name = "bad_ep"
        def load(self):
            return bad
    with mock.patch.object(importlib.metadata, "entry_points", return_value=[FakeEP()]):
        reg = ExtensionRegistry()
        found = reg.discover_entry_points(strict=False)
    assert any("discovery_errors" in f for f in found)


def test_load_graph_compatible_filters_schema():
    reg = ExtensionRegistry()
    reg.register(dict(VALID_MANIFEST))
    other = dict(VALID_MANIFEST)
    other["schema"] = "other/v1"
    other["extension_id"] = "other_ext"
    reg.register(other, strict=False)
    compat = [m for m in reg.all() if m.get("schema") == "simplicio.loop-extension/v1"]
    assert len(compat) == 1


def test_validate_manifest_importable():
    # sanity: the contract dependency the registry relies on is intact
    assert validate_manifest(VALID_MANIFEST) == []
