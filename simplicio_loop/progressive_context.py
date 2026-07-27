from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

PROGRESSIVE_CONTEXT_SCHEMA = "simplicio.loop.progressive-context/v1"
PACKET_SCHEMA = "simplicio.context-packet/v1"


class ProgressiveContextError(ValueError):
    """Raised when progressive context cannot be trusted or fit its budget."""


class ContextBudgetError(ProgressiveContextError):
    """Raised when new context would exceed the bounded byte budget."""


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProgressiveContextError(f"{name} must be a non-empty string")
    return value.strip()


def _span_key(span: Mapping[str, Any]) -> str:
    symbol = str(span.get("symbol") or "").strip()
    file_name = str(span.get("file") or "").strip()
    start, end = span.get("start_line"), span.get("end_line")
    if not symbol and not file_name:
        raise ProgressiveContextError("Fast span must identify a symbol or file")
    return f"{symbol}|{file_name}|{start}|{end}"


@dataclass(frozen=True)
class ContextHandle:
    handle_id: str
    span_key: str
    content_sha256: str
    content: str
    tokens: int
    bytes: int

    def as_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        value = {"handle_id": self.handle_id, "span_key": self.span_key, "content_sha256": self.content_sha256, "tokens": self.tokens, "bytes": self.bytes}
        if include_content:
            value["content"] = self.content
        return value


@dataclass
class ProgressiveContext:
    task_id: str
    generation: str
    max_bytes: int = 131072
    _handles: dict[str, ContextHandle] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.task_id = _text(self.task_id, "task_id")
        self.generation = _text(self.generation, "generation")
        if isinstance(self.max_bytes, bool) or not isinstance(self.max_bytes, int) or self.max_bytes < 1:
            raise ProgressiveContextError("max_bytes must be a positive integer")

    @property
    def context_bytes(self) -> int:
        return sum(item.bytes for item in self._handles.values())

    @property
    def context_tokens(self) -> int:
        return sum(item.tokens for item in self._handles.values())

    @property
    def handle_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._handles))

    def manifest(self) -> dict[str, Any]:
        result = {
            "schema": PROGRESSIVE_CONTEXT_SCHEMA,
            "task_id": self.task_id,
            "generation": self.generation,
            "handles": [self._handles[key].as_dict() for key in self.handle_ids],
            "context_bytes": self.context_bytes,
            "context_tokens": self.context_tokens,
            "remaining_bytes": self.max_bytes - self.context_bytes,
        }
        result["manifest_digest"] = _digest(result)
        return result

    def observe_packet(self, packet: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(packet, Mapping) or packet.get("schema") != PACKET_SCHEMA:
            raise ProgressiveContextError("unexpected context packet schema")
        if _text(packet.get("generation"), "generation") != self.generation:
            raise ProgressiveContextError("stale context generation")
        spans = packet.get("spans")
        if not isinstance(spans, list):
            raise ProgressiveContextError("context packet spans must be a list")
        pending: dict[str, ContextHandle] = {}
        reused: list[str] = []
        for raw in spans:
            if not isinstance(raw, Mapping):
                raise ProgressiveContextError("context packet span must be a mapping")
            key = _span_key(raw)
            content = raw.get("content")
            if not isinstance(content, str):
                raise ProgressiveContextError("Fast span content must be text")
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            handle_id = "fast:" + hashlib.sha256(key.encode("utf-8")).hexdigest()
            tokens = raw.get("tokens", 0)
            if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
                raise ProgressiveContextError("context span tokens must be non-negative")
            handle = ContextHandle(handle_id, key, digest, content, tokens, len(content.encode("utf-8")))
            existing = self._handles.get(handle_id)
            if existing is not None:
                if existing.content_sha256 != digest:
                    raise ProgressiveContextError("handle content changed without a new generation")
                reused.append(handle_id)
            else:
                pending[handle_id] = handle
        pending_bytes = sum(item.bytes for item in pending.values())
        if self.context_bytes + pending_bytes > self.max_bytes:
            raise ContextBudgetError("context packet exceeds progressive byte budget")
        self._handles.update(pending)
        return {
            "schema": PROGRESSIVE_CONTEXT_SCHEMA,
            "task_id": self.task_id,
            "generation": self.generation,
            "new_handles": sorted(pending),
            "reused_handles": sorted(reused),
            "new_bytes": pending_bytes,
            "new_tokens": sum(item.tokens for item in pending.values()),
            "manifest": self.manifest(),
        }

    def materialize(self, handle_ids: Sequence[str]) -> list[dict[str, Any]]:
        requested = sorted({_text(item, "handle_id") for item in handle_ids})
        missing = [item for item in requested if item not in self._handles]
        if missing:
            raise ProgressiveContextError(f"unknown context handles: {missing}")
        return [self._handles[item].as_dict(include_content=True) for item in requested]

    def retry_delta(self, *, error: str, diff: str = "", evidence: Sequence[str] = (), affected_handles: Sequence[str] = ()) -> dict[str, Any]:
        handles = sorted({_text(item, "handle_id") for item in affected_handles})
        unknown = [item for item in handles if item not in self._handles]
        if unknown:
            raise ProgressiveContextError(f"unknown affected handles: {unknown}")
        result = {
            "schema": "simplicio.loop.failure-delta/v1",
            "task_id": self.task_id,
            "generation": self.generation,
            "error": _text(error, "error"),
            "diff": str(diff),
            "evidence": sorted({_text(item, "evidence") for item in evidence}),
            "affected_handles": handles,
            "reused_handles": list(self.handle_ids),
            "context_bytes": self.context_bytes,
            "context_tokens": self.context_tokens,
        }
        result["delta_digest"] = _digest(result)
        return result
