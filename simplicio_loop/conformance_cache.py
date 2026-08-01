"""Content-addressed, fail-closed cache for backend conformance receipts."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "simplicio.loop-conformance-cache/v1"


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ConformanceCache:
    """Persist only hash-addressed receipts; malformed cache data is a miss."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    @staticmethod
    def key(*, provider: Mapping[str, Any], corpus_digest: str,
            policy_digest: str, schema: str) -> str:
        identity = {
            "provider": dict(provider), "corpus_digest": corpus_digest,
            "policy_digest": policy_digest, "schema": schema,
        }
        return _hash(identity)

    def get(self, key: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            rows = payload.get("entries")
            entry = rows.get(key) if isinstance(rows, Mapping) else None
            if payload.get("schema") != SCHEMA or not isinstance(entry, Mapping):
                return None
            if entry.get("key") != key or entry.get("passed") is not True:
                return None
            receipt = entry.get("receipt")
            return dict(receipt) if isinstance(receipt, Mapping) else None
        except (OSError, ValueError, TypeError):
            return None

    def put(self, key: str, receipt: Mapping[str, Any]) -> None:
        payload: dict[str, Any] = {"schema": SCHEMA, "entries": {}}
        try:
            existing = json.loads(self.path.read_text(encoding="utf-8"))
            if existing.get("schema") == SCHEMA and isinstance(existing.get("entries"), Mapping):
                payload["entries"] = dict(existing["entries"])
        except (OSError, ValueError, TypeError):
            pass
        payload["entries"][key] = {"key": key, "passed": receipt.get("passed") is True,
                                    "receipt": dict(receipt)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)

    def invalidate(self, key: str) -> bool:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            entries = payload.get("entries")
            if payload.get("schema") != SCHEMA or not isinstance(entries, dict) or key not in entries:
                return False
            del entries[key]
            self.path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            return True
        except (OSError, ValueError, TypeError):
            return False


__all__ = ["SCHEMA", "ConformanceCache"]
