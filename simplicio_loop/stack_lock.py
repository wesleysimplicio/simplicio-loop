"""Deterministic installed-stack lock and route freeze for issue #1032.

The caller supplies version/capability observations from the installed
operators. This module canonicalizes them, hashes executable artifacts when
available, and freezes the selected route before an effect. It never installs,
updates, or launches a component.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STACK_LOCK_SCHEMA = "simplicio.stack-lock/v1"
ROUTES = frozenset({"standalone", "runtime-backed"})


class StackLockError(ValueError):
    """Raised when a stack lock is invalid or drifted after freeze."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _lock_payload(route: str, run_id: str, components: Iterable[StackComponent]) -> dict[str, Any]:
    return {
        "schema": STACK_LOCK_SCHEMA,
        "route": route,
        "run_id": str(run_id),
        "components": [item.to_dict() for item in components],
    }


def _lock_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class StackComponent:
    name: str
    version: str
    executable: str
    build_sha: str
    artifact_sha256: str
    capabilities: tuple[str, ...] = ()
    available: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "executable": self.executable,
            "build_sha": self.build_sha,
            "artifact_sha256": self.artifact_sha256,
            "capabilities": list(self.capabilities),
            "available": self.available,
        }


def observe_component(
    name: str,
    version: str,
    executable: str | Path = "",
    *,
    build_sha: str = "",
    capabilities: Iterable[str] = (),
    artifact_sha256: str | None = None,
) -> StackComponent:
    """Create a component observation without invoking the executable."""
    path = Path(executable) if executable else None
    executable_text = str(path) if path is not None else ""
    artifact = artifact_sha256 or ""
    if not artifact and path is not None and path.is_file():
        artifact = _sha256_file(path)
    available = bool(version and executable_text and artifact)
    return StackComponent(
        name=str(name),
        version=str(version),
        executable=executable_text,
        build_sha=str(build_sha),
        artifact_sha256=artifact,
        capabilities=tuple(sorted({str(value) for value in capabilities if str(value)})),
        available=available,
    )


def _validate_route(route: str, components: tuple[StackComponent, ...]) -> None:
    if route not in ROUTES:
        raise StackLockError(f"invalid route: {route}")
    if route == "runtime-backed":
        runtime = next((item for item in components if item.name == "simplicio-runtime"), None)
        if runtime is None or not runtime.available:
            raise StackLockError("runtime-backed route requires an available simplicio-runtime")


@dataclass(frozen=True)
class StackLock:
    route: str
    components: tuple[StackComponent, ...]
    lock_hash: str
    run_id: str = ""

    @classmethod
    def create(
        cls,
        components: Iterable[StackComponent],
        route: str,
        *,
        run_id: str = "",
    ) -> StackLock:
        normalized = tuple(sorted(components, key=lambda item: item.name))
        names = [item.name for item in normalized]
        if len(names) != len(set(names)):
            raise StackLockError("duplicate stack component")
        _validate_route(route, normalized)
        digest = _lock_hash(_lock_payload(route, str(run_id), normalized))
        return cls(route=route, components=normalized, lock_hash=digest, run_id=str(run_id))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> StackLock:
        """Load a lock only when its structure and canonical hash are valid."""
        errors = validate_stack_lock(payload)
        if errors:
            raise StackLockError("invalid stack lock: " + ", ".join(errors))
        components = tuple(_component_from_dict(item) for item in payload["components"])
        _validate_route(str(payload["route"]), components)
        return cls(
            route=str(payload["route"]),
            components=components,
            lock_hash=str(payload["lock_hash"]),
            run_id=str(payload.get("run_id", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": STACK_LOCK_SCHEMA,
            "route": self.route,
            "run_id": self.run_id,
            "components": [item.to_dict() for item in self.components],
            "lock_hash": self.lock_hash,
            "frozen": True,
        }

    def verify_unchanged(
        self,
        components: Iterable[StackComponent],
        route: str,
    ) -> None:
        """Fail closed when artifacts, capabilities, or route drift after freeze."""
        current = StackLock.create(components, route, run_id=self.run_id)
        if current.lock_hash != self.lock_hash:
            raise StackLockError(
                f"stack drift after route freeze: expected {self.lock_hash}, found {current.lock_hash}"
            )


def validate_stack_lock(lock: Mapping[str, Any]) -> list[str]:
    """Validate a serialized lock without probing or mutating installed state."""
    errors: list[str] = []
    if lock.get("schema") != STACK_LOCK_SCHEMA:
        errors.append("schema_mismatch")
    if lock.get("frozen") is not True:
        errors.append("lock_not_frozen")
    if not isinstance(lock.get("lock_hash"), str) or len(lock["lock_hash"]) != 64:
        errors.append("lock_hash_invalid")
    if lock.get("route") not in ROUTES:
        errors.append("route_invalid")
    components = lock.get("components")
    if not isinstance(components, list) or not components:
        errors.append("components_missing")
        return errors

    names: list[str] = []
    for component in components:
        if not isinstance(component, Mapping):
            errors.append("component_invalid")
            continue
        required = ("name", "version", "executable", "build_sha", "artifact_sha256", "capabilities", "available")
        if any(field not in component for field in required):
            errors.append("component_fields_missing")
            continue
        name = component.get("name")
        if not isinstance(name, str) or not name:
            errors.append("component_name_invalid")
        else:
            names.append(name)
        if not isinstance(component.get("capabilities"), list):
            errors.append("component_capabilities_invalid")
        if not isinstance(component.get("available"), bool):
            errors.append("component_availability_invalid")
    if len(names) != len(set(names)):
        errors.append("duplicate_component")

    if not errors and isinstance(lock.get("lock_hash"), str) and len(lock["lock_hash"]) == 64:
        expected = _lock_hash(_lock_payload(str(lock["route"]), str(lock.get("run_id", "")), (
            _component_from_dict(item) for item in components
        )))
        if lock["lock_hash"] != expected:
            errors.append("lock_hash_mismatch")
    return errors


def _component_from_dict(payload: Mapping[str, Any]) -> StackComponent:
    return StackComponent(
        name=str(payload["name"]),
        version=str(payload["version"]),
        executable=str(payload["executable"]),
        build_sha=str(payload["build_sha"]),
        artifact_sha256=str(payload["artifact_sha256"]),
        capabilities=tuple(str(item) for item in payload["capabilities"]),
        available=bool(payload["available"]),
    )


def load_stack_lock(path: str | Path) -> StackLock:
    """Read and verify a persisted lock without changing it."""
    lock_path = Path(path)
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StackLockError(f"cannot read stack lock: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise StackLockError("invalid stack lock: root_not_object")
    return StackLock.from_dict(payload)


def write_stack_lock(lock: StackLock, path: str | Path) -> Path:
    """Persist a lock atomically and never replace a different existing lock."""
    lock_path = Path(path)
    if lock_path.exists():
        existing = load_stack_lock(lock_path)
        if existing.lock_hash != lock.lock_hash:
            raise StackLockError("stack lock already exists with a different hash")
        return lock_path

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=lock_path.parent, prefix=f".{lock_path.name}.", delete=False
        ) as handle:
            temporary_name = handle.name
            json.dump(lock.to_dict(), handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, lock_path)
    except OSError as exc:
        if temporary_name:
            try:
                Path(temporary_name).unlink()
            except OSError:
                pass
        raise StackLockError(f"cannot persist stack lock: {exc}") from exc
    return lock_path


__all__ = [
    "ROUTES",
    "STACK_LOCK_SCHEMA",
    "StackComponent",
    "StackLock",
    "StackLockError",
    "load_stack_lock",
    "observe_component",
    "validate_stack_lock",
    "write_stack_lock",
]
