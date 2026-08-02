"""Deterministic multi-source fan-in and delivery routing (#1037)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from .source_contract import (
    DemandEnvelope,
    SourceIdentity,
    SourcePage,
    SourceStatus,
    digest,
)

FAN_IN_SCHEMA = "simplicio.source-fan-in/v1"
GOVERNED_FIELDS = ("title", "body", "state")


def _revision_key(envelope: DemandEnvelope) -> tuple[str, str, str]:
    """Return the provider-independent ordering key for an envelope revision."""

    revision = envelope.revision
    return revision.updated_at, revision.tie_breaker, revision.token


def _alias(envelope: DemandEnvelope) -> str:
    source = envelope.identity.source
    return f"{source.provider}|{source.tenant}|{source.project}|{source.repository}|{envelope.identity.external_id}"


def _relation_alias(relation: Mapping[str, Any], envelope: DemandEnvelope) -> str | None:
    if relation.get("type") not in {"explicit_link", "mirror", "alias"}:
        return None
    provider = str(relation.get("provider") or envelope.identity.source.provider)
    external_id = str(relation.get("external_id") or relation.get("id") or "")
    if not external_id:
        return None
    tenant = str(relation.get("tenant") or envelope.identity.source.tenant)
    project = str(relation.get("project") or envelope.identity.source.project)
    repository = str(relation.get("repository") or "")
    return f"{provider}|{tenant}|{project}|{repository}|{external_id}"


@dataclass
class DemandRecord:
    demand_id: str
    envelopes: dict[str, DemandEnvelope] = field(default_factory=dict)
    aliases: set[str] = field(default_factory=set)
    conflict_fields: set[str] = field(default_factory=set)
    delivery_provider: str = ""
    delivery_external_id: str = ""

    @property
    def latest(self) -> DemandEnvelope:
        return max(self.envelopes.values(), key=lambda item: (item.revision, item.envelope_id))

    @property
    def conflicted(self) -> bool:
        return bool(self.conflict_fields)

    def as_dict(self) -> dict[str, Any]:
        return {
            "demand_id": self.demand_id,
            "envelopes": [item.as_dict() for item in sorted(self.envelopes.values(), key=lambda item: item.envelope_id)],
            "aliases": sorted(self.aliases),
            "conflict_fields": sorted(self.conflict_fields),
            "delivery": {
                "provider": self.delivery_provider or None,
                "external_id": self.delivery_external_id or None,
            },
        }


class FanInReducer:
    """Merge only exact identities or explicit links; similarity never merges."""

    def __init__(self, *, similarity_threshold: float = 0.92) -> None:
        if not 0.0 < similarity_threshold <= 1.0:
            raise ValueError("similarity_threshold must be bounded")
        self.similarity_threshold = similarity_threshold
        self._records: dict[str, DemandRecord] = {}
        self._aliases: dict[str, str] = {}
        self._source_status: dict[str, str] = {}

    def _suggestions(self, envelope: DemandEnvelope, current_id: str | None) -> list[str]:
        suggestions: list[str] = []
        for demand_id, record in self._records.items():
            if demand_id == current_id or record.conflicted:
                continue
            ratio = SequenceMatcher(None, envelope.title.casefold(), record.latest.title.casefold()).ratio()
            if ratio >= self.similarity_threshold:
                suggestions.append(demand_id)
        return sorted(suggestions)

    def ingest(self, envelope: DemandEnvelope) -> dict[str, Any]:
        aliases = {_alias(envelope)}
        aliases.update(
            alias for alias in (_relation_alias(row, envelope) for row in envelope.relations) if alias
        )
        matches = {self._aliases[alias] for alias in aliases if alias in self._aliases}
        if len(matches) > 1:
            return {
                "schema": FAN_IN_SCHEMA,
                "status": "CONFLICT",
                "reason_code": "alias_maps_to_multiple_demands",
                "suggestions": [],
            }
        demand_id = next(iter(matches), digest({"canonical": envelope.envelope_id}))
        record = self._records.setdefault(demand_id, DemandRecord(demand_id))
        existing = record.envelopes.get(envelope.envelope_id)
        if existing is None:
            if record.envelopes:
                current = record.latest
                for field_name in GOVERNED_FIELDS:
                    if getattr(current, field_name) != getattr(envelope, field_name):
                        record.conflict_fields.add(field_name)
            record.envelopes[envelope.envelope_id] = envelope
            record.aliases.update(aliases)
            for alias in aliases:
                self._aliases[alias] = demand_id
        elif _revision_key(envelope) > _revision_key(existing):
            # A newer revision updates the same source identity; it is not a
            # cross-source conflict and must replace the older payload.
            record.envelopes[envelope.envelope_id] = envelope
            record.aliases.update(aliases)
            for alias in aliases:
                self._aliases[alias] = demand_id
        status = "CONFLICT" if record.conflicted else "ADMITTED"
        return {
            "schema": FAN_IN_SCHEMA,
            "status": status,
            "demand_id": demand_id,
            "envelope_id": envelope.envelope_id,
            "revision_id": envelope.revision_id,
            "provenance_count": len(record.envelopes),
            "suggestions": self._suggestions(envelope, demand_id),
            "conflict_fields": sorted(record.conflict_fields),
        }

    def ingest_page(self, page: SourcePage) -> list[dict[str, Any]]:
        if page.status not in {SourceStatus.OK, SourceStatus.EMPTY}:
            source_key = page.cursor_before.token() if page.cursor_before else "unknown"
            self._source_status[source_key] = page.status.value
            return [{
                "schema": FAN_IN_SCHEMA,
                "status": "SOURCE_UNAVAILABLE",
                "source_status": page.status.value,
                "error_code": page.error_code or page.status.value,
                "retry_after_seconds": page.retry_after_seconds,
            }]
        return [self.ingest(item) for item in page.items]

    def freeze_delivery(self, demand_id: str, target: SourceIdentity, external_id: str) -> dict[str, Any]:
        record = self._records.get(demand_id)
        if record is None:
            return {"schema": FAN_IN_SCHEMA, "status": "BLOCKED", "reason_code": "demand_not_found"}
        if record.conflicted:
            return {"schema": FAN_IN_SCHEMA, "status": "BLOCKED", "reason_code": "governed_conflict", "conflict_fields": sorted(record.conflict_fields)}
        if record.delivery_provider and (
            record.delivery_provider != target.provider or record.delivery_external_id != external_id
        ):
            return {"schema": FAN_IN_SCHEMA, "status": "CONFLICT", "reason_code": "delivery_route_frozen"}
        record.delivery_provider = target.provider
        record.delivery_external_id = str(external_id)
        return {
            "schema": FAN_IN_SCHEMA,
            "status": "FROZEN",
            "demand_id": demand_id,
            "provider": target.provider,
            "external_id": str(external_id),
        }

    def deliver(
        self,
        demand_id: str,
        *,
        provider: str,
        write: Callable[[DemandEnvelope], Mapping[str, Any]],
        requery: Callable[[DemandEnvelope], Mapping[str, Any] | None],
    ) -> dict[str, Any]:
        record = self._records.get(demand_id)
        if record is None:
            return {"schema": FAN_IN_SCHEMA, "status": "BLOCKED", "reason_code": "demand_not_found"}
        if record.conflicted:
            return {"schema": FAN_IN_SCHEMA, "status": "BLOCKED", "reason_code": "governed_conflict"}
        if record.delivery_provider != provider:
            return {"schema": FAN_IN_SCHEMA, "status": "BLOCKED", "reason_code": "delivery_route_not_frozen"}
        envelope = record.latest
        try:
            write_receipt = dict(write(envelope))
        except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
            return {"schema": FAN_IN_SCHEMA, "status": "BLOCKED", "reason_code": "delivery_write_failed", "error": str(exc)}
        try:
            observed = requery(envelope)
        except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
            return {"schema": FAN_IN_SCHEMA, "status": "BLOCKED", "reason_code": "delivery_requery_failed", "error": str(exc)}
        if not observed or observed.get("envelope_id") not in {None, envelope.envelope_id}:
            return {"schema": FAN_IN_SCHEMA, "status": "BLOCKED", "reason_code": "delivery_requery_mismatch", "write_receipt": write_receipt}
        return {
            "schema": FAN_IN_SCHEMA,
            "status": "VERIFIED",
            "demand_id": demand_id,
            "provider": provider,
            "requery_observed": True,
            "write_receipt": write_receipt,
        }

    def inspect(self) -> dict[str, Any]:
        return {
            "schema": FAN_IN_SCHEMA,
            "demands": [record.as_dict() for record in sorted(self._records.values(), key=lambda item: item.demand_id)],
            "source_status": dict(sorted(self._source_status.items())),
        }


__all__ = ["FAN_IN_SCHEMA", "DemandRecord", "FanInReducer"]
