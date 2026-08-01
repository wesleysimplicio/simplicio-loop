"""Crash-safe broker for shared canonical generations and isolated overlays."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Mapping

from .checkpoint_lifecycle import (
    CheckpointLifecycle,
    LifecycleError,
    validate_candidate_id,
)
from .fast_fanout import CanonicalGeneration
from .map_service import MapServiceRegistry

SCHEMA = "simplicio.loop.generation-binding/v1"


def _digest(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


@dataclass(frozen=True)
class GenerationBinding:
    schema: str
    repository: str
    identity_key: str
    tree_hash: str
    config_identity: str
    mapper_generation: str
    fast_generation: str
    canonical_cache_key: str
    source_commit: str
    context_hash: str
    plan_hash: str
    generation_receipt_hash: str
    worktree: str
    attempt_id: str
    candidate_id: str
    overlay_path: str
    lease_expires_ns: int
    receipt_hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def verify(cls, value: Mapping[str, Any]) -> "GenerationBinding":
        payload = dict(value)
        supplied = payload.pop("receipt_hash", "")
        if payload.get("schema") != SCHEMA or supplied != _digest(payload):
            raise LifecycleError("generation binding receipt mismatch")
        payload["receipt_hash"] = supplied
        try:
            return cls(**payload)
        except TypeError as exc:
            raise LifecycleError("generation binding schema mismatch") from exc


class GenerationBroker:
    """Own one canonical build per identity/tree/config and durable attempt pins."""

    def __init__(self, registry: MapServiceRegistry, lifecycle: CheckpointLifecycle) -> None:
        self.registry = registry
        self.lifecycle = lifecycle
        self._lock = RLock()
        self._canonical: dict[tuple[str, str, tuple[str, ...]], str] = {}
        self._bindings: dict[str, GenerationBinding] = {}
        self._events: list[dict[str, Any]] = []
        self._promoted_generation = lifecycle.fast_generation
        self._metrics = {"cache_hits": 0, "cache_misses": 0, "build_wait_ns": 0}

    def _record(self, event: str, **details: Any) -> None:
        self._events.append({"event": event, "created_ns": time.time_ns(), **details})

    def bind(
        self,
        identity_key: str,
        *,
        tree_hash: str,
        files: Iterable[str],
        candidate_id: str,
        generation: CanonicalGeneration,
        lease_expires_ns: int | None = None,
    ) -> GenerationBinding:
        started = time.perf_counter_ns()
        with self._lock:
            candidate_id = validate_candidate_id(candidate_id)
            if generation.generation != self.lifecycle.fast_generation:
                raise LifecycleError("stale canonical generation")
            identity = self.registry.identity(identity_key)
            worktree = str(Path(identity.worktree_root or identity.canonical_root).resolve())
            if Path(worktree) != self.lifecycle.base_path:
                raise LifecycleError("cross-worktree generation binding")
            existing = self._bindings.get(candidate_id)
            if existing is not None:
                if (
                    existing.identity_key != identity_key
                    or existing.tree_hash != str(tree_hash)
                    or existing.mapper_generation != generation.generation
                    or existing.attempt_id != self.lifecycle.attempt_id
                ):
                    raise LifecycleError("candidate generation fence mismatch")
                return self.inspect(candidate_id)
            normalized_files = tuple(sorted(set(map(str, files))))
            cache_identity = (identity_key, str(tree_hash), normalized_files)
            cache_key = self._canonical.get(cache_identity)
            if cache_key:
                self.registry.get_view(cache_key, acquire=False)
                self._metrics["cache_hits"] += 1
            else:
                canonical = self.registry.build_canonical(
                    identity_key, tree_hash=tree_hash, files=normalized_files
                )
                cache_key = canonical.cache_key
                self._canonical[cache_identity] = cache_key
                self._metrics["cache_misses"] += 1
                base_manifest = {
                    "schema": SCHEMA,
                    "identity_key": identity_key,
                    "repository": identity.repository,
                    "tree_hash": str(tree_hash),
                    "config_identity": _digest({"mapper_config": identity.mapper_config}),
                    "canonical_cache_key": cache_key,
                    "mapper_generation": generation.generation,
                    "fast_generation": generation.generation,
                    "source_commit": generation.source_commit,
                    "context_hash": generation.context_hash,
                    "plan_hash": generation.plan_hash,
                    "generation_receipt_hash": generation.receipt_hash,
                }
                base_manifest["receipt_hash"] = _digest(base_manifest)
                _write_json(self.lifecycle.attempt / "generation-manifest.json", base_manifest)
            overlay = self.lifecycle.create_overlay(candidate_id).resolve()
            expiry = int(lease_expires_ns or time.time_ns() + 3_600_000_000_000)
            self.lifecycle.lease(candidate_id, expires_ns=expiry)
            config_identity = _digest({"mapper_config": identity.mapper_config})
            payload = {
                "schema": SCHEMA,
                "repository": identity.repository,
                "identity_key": identity_key,
                "tree_hash": str(tree_hash),
                "config_identity": config_identity,
                "mapper_generation": generation.generation,
                "fast_generation": generation.generation,
                "canonical_cache_key": cache_key,
                "source_commit": generation.source_commit,
                "context_hash": generation.context_hash,
                "plan_hash": generation.plan_hash,
                "generation_receipt_hash": generation.receipt_hash,
                "worktree": worktree,
                "attempt_id": self.lifecycle.attempt_id,
                "candidate_id": candidate_id,
                "overlay_path": str(overlay),
                "lease_expires_ns": expiry,
            }
            binding = GenerationBinding(**payload, receipt_hash=_digest(payload))
            _write_json(overlay / "generation-binding.json", binding.to_dict())
            self._bindings[candidate_id] = binding
            self._metrics["build_wait_ns"] += time.perf_counter_ns() - started
            self._record("binding", candidate_id=candidate_id, cache_key=cache_key)
            return binding

    def inspect(self, candidate_id: str) -> GenerationBinding:
        with self._lock:
            candidate_id = validate_candidate_id(candidate_id)
            binding = self._bindings.get(candidate_id)
            path = self.lifecycle.overlays / candidate_id / "generation-binding.json"
            if binding is None:
                try:
                    binding = GenerationBinding.verify(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError) as exc:
                    raise LifecycleError("generation binding missing or corrupt") from exc
            verified = GenerationBinding.verify(binding.to_dict())
            if Path(verified.overlay_path).resolve() != path.parent.resolve():
                raise LifecycleError("generation binding overlay mismatch")
            if verified.worktree != str(self.lifecycle.base_path):
                raise LifecycleError("cross-worktree generation binding")
            return verified

    def pin(self, candidate_id: str, *, expires_ns: int) -> GenerationBinding:
        with self._lock:
            current = self.inspect(candidate_id)
            self.lifecycle.lease(candidate_id, expires_ns=expires_ns)
            payload = current.to_dict()
            payload.pop("receipt_hash")
            payload["lease_expires_ns"] = int(expires_ns)
            updated = GenerationBinding(**payload, receipt_hash=_digest(payload))
            _write_json(Path(updated.overlay_path) / "generation-binding.json", updated.to_dict())
            self._bindings[candidate_id] = updated
            self._record("pin", candidate_id=candidate_id)
            return updated

    def release(self, candidate_id: str) -> None:
        with self._lock:
            binding = self.inspect(candidate_id)
            self.lifecycle.lease(candidate_id, expires_ns=0)
            self.registry.release(binding.canonical_cache_key)
            self._record("release", candidate_id=candidate_id)

    def event(self, event: str, **details: Any) -> dict[str, Any]:
        """Record a Mapper/Fast foreground or background event without repinning."""
        with self._lock:
            self._record(str(event), **details)
            return dict(self._events[-1])

    def reconcile(self) -> dict[str, Any]:
        with self._lock:
            recovered, corrupt = [], []
            for path in sorted(self.lifecycle.overlays.glob("*/generation-binding.json")):
                try:
                    binding = GenerationBinding.verify(json.loads(path.read_text(encoding="utf-8")))
                    self._bindings[binding.candidate_id] = binding
                    recovered.append(binding.candidate_id)
                except (OSError, json.JSONDecodeError, LifecycleError):
                    corrupt.append(path.parent.name)
            self._record("reconcile", recovered=len(recovered), corrupt=len(corrupt))
            return {"schema": SCHEMA, "recovered": recovered, "corrupt": corrupt}

    def promote(self, generation: CanonicalGeneration) -> dict[str, Any]:
        with self._lock:
            if not all(generation.to_dict().values()):
                raise LifecycleError("generation promotion parity failed")
            previous = self._promoted_generation
            self._promoted_generation = generation.generation
            self._record("promotion", previous=previous, generation=generation.generation)
            return {"previous": previous, "generation": generation.generation}

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema": SCHEMA,
                "promoted_generation": self._promoted_generation,
                "active_bindings": sorted(self._bindings),
                "canonical_bases": len(self._canonical),
                "metrics": dict(self._metrics),
                "events": list(self._events),
            }

    def doctor(self) -> dict[str, Any]:
        result = self.reconcile()
        return {"schema": SCHEMA, "healthy": not result["corrupt"], **result}

    def gc(self, *, retention_ns: int, now_ns: int | None = None, apply: bool = False) -> dict[str, Any]:
        with self._lock:
            result = self.lifecycle.gc(retention_ns=retention_ns, now_ns=now_ns, apply=apply)
            for candidate in result["removed"]:
                self._bindings.pop(candidate, None)
            self._record("gc", removed=list(result["removed"]))
            return result
