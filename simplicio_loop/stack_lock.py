"""Deterministic installed-stack lock and route freeze for issue #1032.

The caller supplies version/capability observations from the installed
operators. This module canonicalizes them, hashes executable artifacts when
available, and freezes the selected route before an effect. It never installs,
updates, or launches a component.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

STACK_LOCK_SCHEMA = "simplicio.stack-lock/v1"
ROUTES = frozenset({"standalone", "runtime-backed"})


class StackLockError(ValueError):
    """Raised when a stack lock is invalid or drifted after freeze."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


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
    ) -> "StackLock":
        normalized = tuple(sorted(components, key=lambda item: item.name))
        names = [item.name for item in normalized]
        if len(names) != len(set(names)):
            raise StackLockError("duplicate stack component")
        _validate_route(route, normalized)
        payload = {
            "schema": STACK_LOCK_SCHEMA,
            "route": route,
            "run_id": str(run_id),
            "components": [item.to_dict() for item in normalized],
        }
        digest = hashlib.sha256(_canonical(payload)).hexdigest()
        return cls(route=route, components=normalized, lock_hash=digest, run_id=str(run_id))

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


__all__ = [
    "ROUTES",
    "STACK_LOCK_SCHEMA",
    "StackComponent",
    "StackLock",
    "StackLockError",
    "observe_component",
    "validate_stack_lock",
]
