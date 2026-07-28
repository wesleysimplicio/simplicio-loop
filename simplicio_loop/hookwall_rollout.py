"""Atomic Hookwall rollout state; every mode preserves the mutation gate."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

SCHEMA = "simplicio.hookwall-rollout/v1"
MODES = frozenset({"shadow", "canary", "enforced", "rollback"})


def _hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class HookwallRollout:
    """Persist rollout transitions atomically without weakening Hookwall."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def transition(self, mode: str, *, actor: str, reason: str) -> dict[str, Any]:
        if mode not in MODES:
            raise ValueError(f"invalid Hookwall rollout mode: {mode}")
        previous = self.read()
        receipt: dict[str, Any] = {
            "schema": SCHEMA,
            "mode": mode,
            "previous_mode": previous["mode"] if previous else None,
            "mutation_requires_hookwall": True,
            "actor": actor,
            "reason": reason,
            "created_ns": time.time_ns(),
        }
        receipt["receipt_hash"] = _hash(receipt)
        fd, temporary = tempfile.mkstemp(
            dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(receipt, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return receipt

    def read(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        value = json.loads(self.path.read_text(encoding="utf-8"))
        supplied = value.pop("receipt_hash", None)
        if supplied != _hash(value):
            raise ValueError("Hookwall rollout receipt was tampered")
        value["receipt_hash"] = supplied
        if value.get("mode") not in MODES or value.get("mutation_requires_hookwall") is not True:
            raise ValueError("Hookwall rollout state is unsafe")
        return value


__all__ = ["HookwallRollout", "MODES", "SCHEMA"]
