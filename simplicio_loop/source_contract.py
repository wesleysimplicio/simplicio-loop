"""Canonical source intake contract for local and provider adapters.

The contract is deliberately provider-neutral and stdlib-only.  Provider modules may
add fields to ``provider_fields`` and ``raw_provenance``, but identity, revision,
cursor, error and receipt semantics stay the same.  The fixture adapter in this
module is also the hermetic conformance harness used by provider implementations.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

SOURCE_ADAPTER_SCHEMA = "simplicio.source-adapter/v1"
SOURCE_RECEIPT_SCHEMA = "simplicio.source-receipt/v1"
SOURCE_CURSOR_SCHEMA = "simplicio.source-cursor/v1"
DELIVERY_RECEIPT_SCHEMA = "simplicio.source-delivery-receipt/v1"
MAX_PAGE_SIZE = 500


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


_SECRET_KEY = re.compile(
    r"(?:token|secret|password|passwd|authorization|cookie|api[_-]?key|private[_-]?key)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_TOKEN_QUERY = re.compile(r"(?i)([?&](?:token|api[_-]?key|secret|password)=)[^&#\s]+")


def redact(value: Any, *, key: str = "") -> Any:
    """Redact common credentials recursively while preserving useful provenance."""
    if _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(name): redact(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, key=key) for item in value]
    if isinstance(value, str):
        value = _BEARER.sub("Bearer [REDACTED]", value)
        return _TOKEN_QUERY.sub(r"\1[REDACTED]", value)
    return value


class SourceStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    FORBIDDEN = "forbidden"
    RATE_LIMITED = "rate_limited"
    STALE_CURSOR = "stale_cursor"
    MALFORMED = "malformed"
    CONFLICT = "conflict"
    TIMEOUT = "timeout"


RETRYABLE_STATUSES = frozenset(
    {SourceStatus.UNAVAILABLE, SourceStatus.RATE_LIMITED, SourceStatus.TIMEOUT}
)


class SourceContractError(ValueError):
    """A malformed local contract, never a provider-empty result."""


class SourceProviderError(RuntimeError):
    """A classified provider failure that must not be converted to an empty page."""

    def __init__(
        self,
        status: SourceStatus,
        detail: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.status = SourceStatus(status)
        self.retry_after_seconds = retry_after_seconds
        super().__init__(detail)


@dataclass(frozen=True)
class SourceIdentity:
    provider: str
    tenant: str
    project: str
    repository: str = ""

    def __post_init__(self) -> None:
        for name in ("provider", "tenant", "project"):
            value = str(getattr(self, name)).strip()
            if not value:
                raise SourceContractError(f"{name} is required")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "repository", str(self.repository).strip())

    @property
    def source_key(self) -> str:
        return digest({
            "provider": self.provider.lower(),
            "tenant": self.tenant,
            "project": self.project,
            "repository": self.repository,
        })

    def as_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "tenant": self.tenant,
            "project": self.project,
            "repository": self.repository,
            "source_key": self.source_key,
        }


@dataclass(frozen=True)
class ItemIdentity:
    source: SourceIdentity
    external_id: str
    kind: str = "work_item"

    def __post_init__(self) -> None:
        if not str(self.external_id).strip() or not str(self.kind).strip():
            raise SourceContractError("external_id and kind are required")
        object.__setattr__(self, "external_id", str(self.external_id).strip())
        object.__setattr__(self, "kind", str(self.kind).strip())

    @property
    def stable_id(self) -> str:
        return digest({
            "source_key": self.source.source_key,
            "external_id": self.external_id,
            "kind": self.kind,
        })

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.as_dict(),
            "external_id": self.external_id,
            "kind": self.kind,
            "stable_id": self.stable_id,
        }


@dataclass(frozen=True, order=True)
class SourceRevision:
    """Provider-independent ordering key; ``tie_breaker`` prevents lost ties."""

    updated_at: str
    tie_breaker: str
    token: str = ""

    def __post_init__(self) -> None:
        if not str(self.updated_at).strip() or not str(self.tie_breaker).strip():
            raise SourceContractError("updated_at and tie_breaker are required")
        object.__setattr__(self, "updated_at", str(self.updated_at).strip())
        object.__setattr__(self, "tie_breaker", str(self.tie_breaker).strip())
        object.__setattr__(self, "token", str(self.token))

    @property
    def revision_id(self) -> str:
        return digest({
            "updated_at": self.updated_at,
            "tie_breaker": self.tie_breaker,
            "token": self.token,
        })

    def as_dict(self) -> dict[str, str]:
        return {
            "updated_at": self.updated_at,
            "tie_breaker": self.tie_breaker,
            "token": self.token,
            "revision_id": self.revision_id,
        }


@dataclass(frozen=True)
class SourceCursor:
    updated_at: str
    tie_breaker: str
    opaque: str = ""

    def __post_init__(self) -> None:
        if not str(self.updated_at).strip() or not str(self.tie_breaker).strip():
            raise SourceContractError("cursor updated_at and tie_breaker are required")

    def token(self) -> str:
        payload = {
            "schema": SOURCE_CURSOR_SCHEMA,
            "updated_at": str(self.updated_at),
            "tie_breaker": str(self.tie_breaker),
            "opaque": str(self.opaque),
        }
        return base64.urlsafe_b64encode(canonical(payload)).decode("ascii").rstrip("=")

    @classmethod
    def from_token(cls, token: str) -> SourceCursor:
        raw = str(token or "")
        if not raw:
            raise SourceContractError("cursor token is empty")
        try:
            padded = raw + "=" * (-len(raw) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise SourceContractError("cursor token is malformed") from exc
        if payload.get("schema") != SOURCE_CURSOR_SCHEMA:
            raise SourceContractError("cursor schema mismatch")
        return cls(payload["updated_at"], payload["tie_breaker"], payload.get("opaque", ""))

    def as_dict(self) -> dict[str, str]:
        return {
            "schema": SOURCE_CURSOR_SCHEMA,
            "updated_at": str(self.updated_at),
            "tie_breaker": str(self.tie_breaker),
            "opaque": str(self.opaque),
            "token": self.token(),
        }


@dataclass(frozen=True)
class DemandEnvelope:
    identity: ItemIdentity
    revision: SourceRevision
    title: str
    body: str = ""
    state: str = "open"
    provider_fields: Mapping[str, Any] = field(default_factory=dict)
    relations: tuple[Mapping[str, Any], ...] = ()
    raw_provenance: Mapping[str, Any] = field(default_factory=dict)
    observed_at: str = ""

    def __post_init__(self) -> None:
        if not str(self.title).strip():
            raise SourceContractError("envelope title is required")
        object.__setattr__(self, "title", str(self.title))
        object.__setattr__(self, "body", str(self.body))
        object.__setattr__(self, "state", str(self.state))
        object.__setattr__(self, "provider_fields", dict(self.provider_fields))
        object.__setattr__(self, "relations", tuple(dict(row) for row in self.relations))
        object.__setattr__(self, "raw_provenance", dict(self.raw_provenance))
        object.__setattr__(self, "observed_at", self.observed_at or _now_iso())

    @property
    def envelope_id(self) -> str:
        """Stable task identity: revisions must not create a second demand."""
        return self.identity.stable_id

    @property
    def revision_id(self) -> str:
        return self.revision.revision_id

    @property
    def idempotency_key(self) -> str:
        return f"{SOURCE_ADAPTER_SCHEMA}:{self.envelope_id}:{self.revision_id}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SOURCE_ADAPTER_SCHEMA,
            "envelope_id": self.envelope_id,
            "identity": self.identity.as_dict(),
            "revision": self.revision.as_dict(),
            "title": self.title,
            "body": self.body,
            "state": self.state,
            "provider_fields": redact(self.provider_fields),
            "relations": redact(self.relations),
            "raw_provenance": redact(self.raw_provenance),
            "observed_at": self.observed_at,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class SourceCapabilities:
    provider: str
    profile: str
    capabilities: frozenset[str] = frozenset({"read", "changes", "reconcile"})
    transport: str = "fixture"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SOURCE_ADAPTER_SCHEMA,
            "provider": self.provider,
            "profile": self.profile,
            "capabilities": sorted(self.capabilities),
            "transport": self.transport,
        }


@dataclass(frozen=True)
class SourcePage:
    status: SourceStatus
    cursor_before: SourceCursor | None
    items: tuple[DemandEnvelope, ...] = ()
    next_cursor: SourceCursor | None = None
    error_code: str = ""
    detail: str = ""
    retry_after_seconds: float | None = None

    def __post_init__(self) -> None:
        status = SourceStatus(self.status)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "items", tuple(self.items))
        if status != SourceStatus.OK and self.items:
            raise SourceContractError("failed source page cannot contain items")
        if status == SourceStatus.OK and self.error_code:
            raise SourceContractError("successful source page cannot contain error_code")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SOURCE_ADAPTER_SCHEMA,
            "status": self.status.value,
            "cursor_before": self.cursor_before.token() if self.cursor_before else None,
            "next_cursor": self.next_cursor.token() if self.next_cursor else None,
            "items": [item.as_dict() for item in self.items],
            "error_code": self.error_code or None,
            "detail": self.detail or None,
            "retry_after_seconds": self.retry_after_seconds,
        }


@dataclass(frozen=True)
class SourceReceipt:
    operation_id: str
    source_key: str
    status: SourceStatus
    cursor_before: str | None
    cursor_after: str | None
    envelope_ids: tuple[str, ...] = ()
    durable: bool = False
    error_code: str = ""
    observed_at: str = ""
    receipt_hash: str = ""

    def __post_init__(self) -> None:
        if not str(self.operation_id).strip() or not str(self.source_key).strip():
            raise SourceContractError("receipt identity is required")
        object.__setattr__(self, "status", SourceStatus(self.status))
        object.__setattr__(self, "envelope_ids", tuple(sorted(set(self.envelope_ids))))
        object.__setattr__(self, "observed_at", self.observed_at or _now_iso())
        expected = digest(self._body())
        if self.receipt_hash and self.receipt_hash != expected:
            raise SourceContractError("source receipt hash mismatch")
        object.__setattr__(self, "receipt_hash", expected)

    def _body(self) -> dict[str, Any]:
        return {
            "schema": SOURCE_RECEIPT_SCHEMA,
            "operation_id": self.operation_id,
            "source_key": self.source_key,
            "status": self.status.value,
            "cursor_before": self.cursor_before,
            "cursor_after": self.cursor_after,
            "envelope_ids": list(self.envelope_ids),
            "durable": self.durable,
            "error_code": self.error_code,
            "observed_at": self.observed_at,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self._body(), "receipt_hash": self.receipt_hash}

    def verify(self) -> None:
        if self.receipt_hash != digest(self._body()):
            raise SourceContractError("source receipt hash mismatch")


@dataclass(frozen=True)
class DeliveryReceipt:
    operation_id: str
    envelope_id: str
    provider: str
    status: str
    before_revision: str
    after_revision: str
    requery_observed: bool
    detail: str = ""
    receipt_hash: str = ""

    def __post_init__(self) -> None:
        expected = digest(self._body())
        if self.receipt_hash and self.receipt_hash != expected:
            raise SourceContractError("delivery receipt hash mismatch")
        object.__setattr__(self, "receipt_hash", expected)

    def _body(self) -> dict[str, Any]:
        return {
            "schema": DELIVERY_RECEIPT_SCHEMA,
            "operation_id": self.operation_id,
            "envelope_id": self.envelope_id,
            "provider": self.provider,
            "status": self.status,
            "before_revision": self.before_revision,
            "after_revision": self.after_revision,
            "requery_observed": self.requery_observed,
            "detail": self.detail,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self._body(), "receipt_hash": self.receipt_hash}


class SourceAdapterV1(Protocol):
    provider: str
    source: SourceIdentity

    def capabilities(self) -> SourceCapabilities: ...

    def list_changes(self, cursor: SourceCursor | None = None, *, limit: int = 100) -> SourcePage: ...

    def get(self, external_id: str) -> DemandEnvelope: ...

    def reconcile(self, operation_id: str, external_id: str, **kwargs: Any) -> Mapping[str, Any]: ...


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class CursorStore:
    """Crash-safe checkpoint store; a cursor is committed only with a durable receipt."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._values: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if self.path is None or not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SourceContractError("cursor store is malformed") from exc
        if not isinstance(payload, dict):
            raise SourceContractError("cursor store root must be an object")
        return {str(key): dict(value) for key, value in payload.items()}

    def get(self, source_key: str) -> SourceCursor | None:
        with self._lock:
            value = self._values.get(source_key)
            return SourceCursor.from_token(value["cursor"]) if value and value.get("cursor") else None

    def commit(self, source_key: str, cursor: SourceCursor | None, receipt: SourceReceipt) -> None:
        receipt.verify()
        if not receipt.durable:
            raise SourceContractError("cursor commit requires durable receipt")
        if receipt.source_key != source_key:
            raise SourceContractError("receipt source does not match cursor source")
        token = cursor.token() if cursor else None
        if receipt.cursor_after != token:
            raise SourceContractError("receipt cursor does not match commit cursor")
        with self._lock:
            current = self._values.get(source_key)
            if current and current.get("receipt_hash") == receipt.receipt_hash:
                return
            self._values[source_key] = {
                "cursor": token,
                "receipt_hash": receipt.receipt_hash,
                "updated_at": _now_iso(),
            }
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix(self.path.suffix + ".tmp")
                temporary.write_text(
                    json.dumps(self._values, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8",
                )
                temporary.replace(self.path)


class FixtureSourceAdapter:
    """Deterministic provider-neutral adapter used by every offline conformance test."""

    provider = "fixture"

    def __init__(
        self,
        source: SourceIdentity,
        envelopes: Iterable[DemandEnvelope] = (),
        *,
        capabilities: SourceCapabilities | None = None,
    ) -> None:
        self.source = source
        self.provider = source.provider
        self._items = {item.identity.external_id: item for item in envelopes}
        self._capabilities = capabilities or SourceCapabilities(self.provider, "fixture")
        self.injected_status: SourceStatus | None = None
        self.injected_retry_after: float | None = None

    def capabilities(self) -> SourceCapabilities:
        return self._capabilities

    def list_changes(self, cursor: SourceCursor | None = None, *, limit: int = 100) -> SourcePage:
        if limit < 1 or limit > MAX_PAGE_SIZE:
            raise SourceContractError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
        if self.injected_status is not None:
            status = SourceStatus(self.injected_status)
            return SourcePage(
                status, cursor, error_code=status.value,
                detail=f"fixture injected {status.value}",
                retry_after_seconds=self.injected_retry_after,
            )
        ordered = sorted(self._items.values(), key=lambda item: (item.revision, item.envelope_id))
        if cursor is not None:
            boundary = (cursor.updated_at, cursor.tie_breaker)
            ordered = [item for item in ordered if (item.revision.updated_at, item.revision.tie_breaker) > boundary]
        page = tuple(ordered[:limit])
        next_cursor = (
            SourceCursor(page[-1].revision.updated_at, page[-1].revision.tie_breaker)
            if page else cursor
        )
        return SourcePage(SourceStatus.OK if page else SourceStatus.EMPTY, cursor, page, next_cursor)

    def get(self, external_id: str) -> DemandEnvelope:
        try:
            return self._items[str(external_id)]
        except KeyError as exc:
            raise SourceProviderError(SourceStatus.MALFORMED, "fixture item not found") from exc

    def reconcile(self, operation_id: str, external_id: str, **_: Any) -> Mapping[str, Any]:
        item = self.get(external_id)
        return {
            "schema": DELIVERY_RECEIPT_SCHEMA,
            "operation_id": operation_id,
            "status": "OBSERVED",
            "envelope": item.as_dict(),
            "requery_observed": True,
        }

    def ingest(
        self,
        store: CursorStore,
        persist: Callable[[DemandEnvelope], None],
        *,
        limit: int = 100,
        operation_id: str = "fixture-ingest",
    ) -> SourceReceipt:
        before = store.get(self.source.source_key)
        page = self.list_changes(before, limit=limit)
        if page.status not in {SourceStatus.OK, SourceStatus.EMPTY}:
            receipt = SourceReceipt(
                operation_id, self.source.source_key, page.status,
                before.token() if before else None, before.token() if before else None,
                error_code=page.error_code,
                durable=False,
            )
            return receipt
        for item in page.items:
            persist(item)
        after = page.next_cursor
        receipt = SourceReceipt(
            operation_id, self.source.source_key, page.status,
            before.token() if before else None, after.token() if after else None,
            tuple(item.envelope_id for item in page.items), durable=True,
        )
        store.commit(self.source.source_key, after, receipt)
        return receipt


__all__ = [
    "DELIVERY_RECEIPT_SCHEMA",
    "MAX_PAGE_SIZE",
    "RETRYABLE_STATUSES",
    "SOURCE_ADAPTER_SCHEMA",
    "SOURCE_CURSOR_SCHEMA",
    "SOURCE_RECEIPT_SCHEMA",
    "CursorStore",
    "DeliveryReceipt",
    "DemandEnvelope",
    "FixtureSourceAdapter",
    "ItemIdentity",
    "SourceAdapterV1",
    "SourceCapabilities",
    "SourceContractError",
    "SourceCursor",
    "SourceIdentity",
    "SourcePage",
    "SourceProviderError",
    "SourceReceipt",
    "SourceRevision",
    "SourceStatus",
    "canonical",
    "digest",
    "redact",
]
