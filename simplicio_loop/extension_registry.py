"""Public extension registry for the `simplicio.loop-extension/v1` contract.

Issue #614 -- part 1 of the productive composition work: a public
``ExtensionRegistry`` the runner / stage coordinator can use to *discover*
external extensions on the productive path, instead of forcing an external
package to copy the core graph and schemas.

Two discovery mechanisms are supported:

* **Explicit registration** -- ``register(manifest)`` for manifests the
  caller already holds (e.g. loaded from a known path or config).
* **Entry-point discovery** -- ``discover_entry_points()`` scans installed
  packages for the ``simplicio.loop-extension`` group and loads each
  declared manifest. This is the "productive" path: an extension package
  simply declares an entry point and is picked up automatically.

Every manifest -- explicit or discovered -- is validated with
``validate_manifest`` from :mod:`simplicio_loop.extension_manifest` before it
is admitted. Invalid manifests are rejected (never silently kept), and the
rejection reason is surfaced to the caller.

This module deliberately does NOT compose graphs (that is
``compose_stage_graph`` in ``extension_manifest``) nor does it mutate core
stages. It only collects trusted, validated manifests so the coordinator can
later compose them.
"""
from __future__ import annotations

import importlib.metadata
from typing import Any, Iterable, Mapping, Sequence

from .extension_manifest import (
    SCHEMA_ID,
    ExtensionManifestError,
    validate_manifest,
)


class ExtensionRegistryError(ValueError):
    """Raised when a registry operation fails and the caller wants a hard stop."""


class ExtensionRegistry:
    """Collects and validates ``simplicio.loop-extension/v1`` manifests.

    A registry instance is the single source of truth for "which extensions
    are available and trusted" on the productive path. It is safe to reuse
    across runs; ``register`` is idempotent for the same ``extension_id``.
    """

    def __init__(self) -> None:
        # Keep the validated declaration and its loaded runtime object together.
        self._by_id: dict[str, dict[str, Any]] = {}
        self._runtime_by_id: dict[str, Any] = {}

    # -- explicit registration -------------------------------------------- #

    def register(self, manifest: Mapping[str, Any], *, runtime: Any = None,
                 strict: bool = True) -> dict[str, Any]:
        """Register a declaration and, when available, its operational runtime."""
        errors = validate_manifest(manifest)
        if errors:
            if not strict:
                return {"ok": False, "errors": errors, "manifest": dict(manifest)}
            raise ExtensionManifestError(
                f"invalid extension manifest {manifest.get('extension_id')!r}: "
                + "; ".join(errors)
            )
        manifest = dict(manifest)
        extension_id = str(manifest["extension_id"])
        self._by_id[extension_id] = manifest
        if runtime is not None:
            self._runtime_by_id[extension_id] = runtime
        else:
            self._runtime_by_id.pop(extension_id, None)
        return manifest

    # -- entry-point discovery -------------------------------------------- #

    def discover_entry_points(
        self, group: str = "simplicio.loop-extension", *, strict: bool = True
    ) -> list[dict[str, Any]]:
        """Discover extensions declared via the named entry-point group.

        Each entry point must resolve to either a manifest ``dict`` or a
        callable returning one. Invalid manifests are rejected; with
        ``strict=False`` they are skipped and a per-entry error is recorded in
        the returned dict under ``discovery_errors``.
        """
        discovered: list[dict[str, Any]] = []
        discovery_errors: list[dict[str, Any]] = []
        try:
            eps = importlib.metadata.entry_points(group=group)
        except TypeError:  # pragma: no cover - py<3.10 shim
            eps = importlib.metadata.entry_points().get(group, [])  # type: ignore[attr-defined]
        for ep in eps:
            try:
                loaded = ep.load()
                obj = loaded() if callable(loaded) else loaded
                manifest = obj if isinstance(obj, Mapping) else getattr(obj, "manifest", None)
                if not isinstance(manifest, Mapping):
                    raise ExtensionManifestError(
                        f"entry point {ep.name} did not resolve to a manifest or provider runtime"
                    )
                runtime = None if isinstance(obj, Mapping) else obj
                result = self.register(manifest, runtime=runtime, strict=strict)
                if isinstance(result, dict) and result.get("ok") is False:
                    discovery_errors.append({"entry_point": ep.name, "errors": result["errors"]})
                    continue
                discovered.append(result)
            except Exception as exc:  # noqa: BLE001 - surface, don't crash the scan
                discovery_errors.append({"entry_point": getattr(ep, "name", "?"), "error": str(exc)})
        if discovery_errors and strict:
            raise ExtensionRegistryError(
                "entry-point discovery rejected manifests: " + str(discovery_errors)
            )
        if discovery_errors:
            discovered.append({"discovery_errors": discovery_errors})  # type: ignore[arg-type]
        return discovered

    # -- access ----------------------------------------------------------- #

    def get(self, extension_id: str) -> dict[str, Any] | None:
        """Return the validated manifest for ``extension_id`` or ``None``."""
        return self._by_id.get(extension_id)

    def runtime(self, extension_id: str) -> Any:
        """Return the exact loaded provider object, never a second import."""
        return self._runtime_by_id.get(extension_id)

    def all(self) -> list[dict[str, Any]]:
        """Return every registered (validated) manifest."""
        return list(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)

    def clear(self) -> None:
        """Drop all declarations and runtime bindings."""
        self._by_id.clear()
        self._runtime_by_id.clear()


def load_graph_compatible(
    registry: ExtensionRegistry, schema_id: str = SCHEMA_ID
) -> Sequence[dict[str, Any]]:
    """Return only manifests matching the expected schema (defensive helper)."""
    return [m for m in registry.all() if m.get("schema") == schema_id]
