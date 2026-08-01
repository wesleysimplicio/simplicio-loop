"""Canonical generation and candidate-overlay broker."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .checkpoint_lifecycle import CheckpointLifecycle, LifecycleError
from .fast_fanout import CanonicalGeneration
from .map_service import MapServiceRegistry


@dataclass(frozen=True)
class GenerationBinding:
    """Receipt binding one canonical generation to one isolated overlay."""

    generation: CanonicalGeneration
    canonical_cache_key: str
    overlay_path: str
    receipt_hash: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["generation"] = self.generation.to_dict()
        return value


class GenerationBroker:
    """Compose map-service generations with checkpoint overlay lifecycle."""

    def __init__(
        self, registry: MapServiceRegistry, lifecycle: CheckpointLifecycle
    ) -> None:
        self.registry = registry
        self.lifecycle = lifecycle

    def bind(
        self,
        identity_key: str,
        *,
        tree_hash: str,
        files: Iterable[str],
        candidate_id: str,
        generation: CanonicalGeneration,
    ) -> GenerationBinding:
        if generation.generation != self.lifecycle.fast_generation:
            raise LifecycleError("stale canonical generation")
        canonical = self.registry.build_canonical(
            identity_key, tree_hash=tree_hash, files=files
        )
        overlay = self.lifecycle.create_overlay(candidate_id).resolve()
        payload = {
            "candidate_id": candidate_id,
            "canonical_cache_key": canonical.cache_key,
            "generation": generation.to_dict(),
            "overlay_path": str(overlay),
        }
        receipt_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return GenerationBinding(
            generation=generation,
            canonical_cache_key=canonical.cache_key,
            overlay_path=str(overlay),
            receipt_hash=receipt_hash,
        )

    def gc(
        self, *, retention_ns: int, now_ns: int | None = None, apply: bool = False
    ) -> dict[str, Any]:
        return self.lifecycle.gc(
            retention_ns=retention_ns, now_ns=now_ns, apply=apply
        )
