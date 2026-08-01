"""Crash-safe broker for shared canonical generations and isolated overlays."""

from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
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
    repository_base_sha: str
    identity_key: str
    tree_hash: str
    files_digest: str
    config_identity: str
    mapper_generation: str
    fast_generation: str
    canonical_cache_key: str
    source_commit: str
    context_hash: str
    plan_hash: str
    generation_receipt_hash: str
    worktree: str
    task_id: str
    attempt_id: str
    candidate_id: str
    overlay_path: str
    lease_expires_ns: int
    receipt_hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def verify(cls, value: Mapping[str, Any], *, trusted: Mapping[str, Any] | None = None) -> "GenerationBinding":
        payload = dict(value)
        supplied = payload.pop("receipt_hash", "")
        if payload.get("schema") != SCHEMA or supplied != _digest(payload):
            raise LifecycleError("generation binding receipt mismatch")
        for key, expected in (trusted or {}).items():
            if payload.get(key) != expected:
                raise LifecycleError(f"generation binding trusted-anchor mismatch: {key}")
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
        self._lock_path = lifecycle.attempt / ".generation-broker.lock"
        self.reconcile()

    @contextmanager
    def _process_lock(self):
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+b") as handle:
            if handle.seek(0, 2) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

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
        with self._lock, self._process_lock():
            candidate_id = validate_candidate_id(candidate_id)
            if generation.generation != self.lifecycle.fast_generation:
                raise LifecycleError("stale canonical generation")
            identity = self.registry.identity(identity_key)
            worktree = str(Path(identity.worktree_root or identity.canonical_root).resolve())
            if Path(worktree) != self.lifecycle.base_path:
                raise LifecycleError("cross-worktree generation binding")
            existing = self._bindings.get(candidate_id)
            normalized_files = tuple(sorted(set(map(str, files))))
            files_digest = _digest({"files": normalized_files})
            config_identity = _digest({"mapper_config": identity.mapper_config})
            if existing is not None:
                if (
                    existing.identity_key != identity_key
                    or existing.tree_hash != str(tree_hash)
                    or existing.mapper_generation != generation.generation
                    or existing.files_digest != files_digest
                    or existing.config_identity != config_identity
                    or existing.source_commit != generation.source_commit
                    or existing.context_hash != generation.context_hash
                    or existing.plan_hash != generation.plan_hash
                    or existing.generation_receipt_hash != generation.receipt_hash
                    or existing.attempt_id != self.lifecycle.attempt_id
                ):
                    raise LifecycleError("candidate generation fence mismatch")
                return self.inspect(candidate_id)
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
                    "repository_base_sha": identity.base_sha,
                    "tree_hash": str(tree_hash),
                    "files": list(normalized_files),
                    "files_digest": files_digest,
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
                manifest_id = hashlib.sha256(repr(cache_identity).encode()).hexdigest()
                _write_json(self.lifecycle.attempt / "generation-manifests" / f"{manifest_id}.json", base_manifest)
            overlay = self.lifecycle.create_overlay(candidate_id).resolve()
            expiry = int(lease_expires_ns or time.time_ns() + 3_600_000_000_000)
            self.lifecycle.lease(candidate_id, expires_ns=expiry)
            payload = {
                "schema": SCHEMA,
                "repository": identity.repository,
                "repository_base_sha": identity.base_sha,
                "identity_key": identity_key,
                "tree_hash": str(tree_hash),
                "files_digest": files_digest,
                "config_identity": config_identity,
                "mapper_generation": generation.generation,
                "fast_generation": generation.generation,
                "canonical_cache_key": cache_key,
                "source_commit": generation.source_commit,
                "context_hash": generation.context_hash,
                "plan_hash": generation.plan_hash,
                "generation_receipt_hash": generation.receipt_hash,
                "worktree": worktree,
                "task_id": self.lifecycle.task_id,
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
            identity = self.registry.identity(binding.identity_key)
            verified = GenerationBinding.verify(
                binding.to_dict(),
                trusted={
                    "repository": identity.repository,
                    "repository_base_sha": identity.base_sha,
                    "worktree": str(self.lifecycle.base_path),
                    "task_id": self.lifecycle.task_id,
                    "attempt_id": self.lifecycle.attempt_id,
                },
            )
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

    def release(self, candidate_id: str) -> GenerationBinding:
        with self._lock:
            binding = self.inspect(candidate_id)
            self.lifecycle.lease(candidate_id, expires_ns=0)
            payload = binding.to_dict()
            payload.pop("receipt_hash")
            payload["lease_expires_ns"] = 0
            released = GenerationBinding(**payload, receipt_hash=_digest(payload))
            _write_json(Path(released.overlay_path) / "generation-binding.json", released.to_dict())
            self._bindings[candidate_id] = released
            self.registry.release(binding.canonical_cache_key)
            self._record("release", candidate_id=candidate_id)
            return released

    def event(self, event: str, **details: Any) -> dict[str, Any]:
        """Record a Mapper/Fast foreground or background event without repinning."""
        with self._lock:
            self._record(str(event), **details)
            return dict(self._events[-1])

    def reconcile(self) -> dict[str, Any]:
        with self._lock:
            recovered, corrupt, canonical = [], [], []
            for path in sorted((self.lifecycle.attempt / "generation-manifests").glob("*.json")):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    supplied = value.pop("receipt_hash")
                    if supplied != _digest(value):
                        raise LifecycleError("canonical manifest receipt mismatch")
                    files = tuple(value["files"])
                    view = self.registry.build_canonical(
                        value["identity_key"], tree_hash=value["tree_hash"], files=files
                    )
                    if view.cache_key != value["canonical_cache_key"]:
                        raise LifecycleError("canonical manifest cache mismatch")
                    self._canonical[(value["identity_key"], value["tree_hash"], files)] = view.cache_key
                    canonical.append(view.cache_key)
                except (OSError, KeyError, json.JSONDecodeError, LifecycleError):
                    corrupt.append(path.name)
            for path in sorted(self.lifecycle.overlays.glob("*/generation-binding.json")):
                try:
                    binding = GenerationBinding.verify(json.loads(path.read_text(encoding="utf-8")))
                    self._bindings[binding.candidate_id] = binding
                    recovered.append(binding.candidate_id)
                except (OSError, json.JSONDecodeError, LifecycleError):
                    corrupt.append(path.parent.name)
            self._record("reconcile", recovered=len(recovered), corrupt=len(corrupt))
            return {"schema": SCHEMA, "recovered": recovered, "canonical": canonical, "corrupt": corrupt}

    def promote(self, generation: CanonicalGeneration) -> dict[str, Any]:
        with self._lock:
            if not all(generation.to_dict().values()):
                raise LifecycleError("generation promotion parity failed")
            previous = self._promoted_generation
            self._promoted_generation = generation.generation
            self.lifecycle.fast_generation = generation.generation
            self.lifecycle.source_commit = generation.source_commit
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
        journal = self.lifecycle.attempt / "generation-gc-journal.json"
        orphaned_transaction = False
        if journal.exists():
            try:
                orphaned_transaction = json.loads(journal.read_text(encoding="utf-8")).get("state") == "PREPARED"
            except (OSError, json.JSONDecodeError):
                orphaned_transaction = True
        return {
            "schema": SCHEMA,
            "healthy": not result["corrupt"] and not orphaned_transaction,
            "orphaned_transaction": orphaned_transaction,
            **result,
        }

    def gc(self, *, retention_ns: int, now_ns: int | None = None, apply: bool = False) -> dict[str, Any]:
        with self._lock, self._process_lock():
            journal = self.lifecycle.attempt / "generation-gc-journal.json"
            transaction = {"schema": SCHEMA, "state": "PREPARED", "created_ns": time.time_ns()}
            transaction["receipt_hash"] = _digest(transaction)
            _write_json(journal, transaction)
            result = self.lifecycle.gc(retention_ns=retention_ns, now_ns=now_ns, apply=apply)
            for candidate in result["removed"]:
                self._bindings.pop(candidate, None)
            self._record("gc", removed=list(result["removed"]))
            transaction = {**transaction, "state": "COMMITTED", "removed": list(result["removed"])}
            transaction.pop("receipt_hash")
            transaction["receipt_hash"] = _digest(transaction)
            _write_json(journal, transaction)
            return result
