"""Offline/reference adapters for the SourceAdapter v1 provider matrix.

These classes normalize sanitized fixture records and expose the same cursor,
identity and delivery contract.  A network transport can be injected by a
provider integration later; the default is intentionally fixture-only so tests
never require credentials or turn an unavailable provider into an empty page.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from .source_contract import (
    DeliveryReceipt,
    DemandEnvelope,
    FixtureSourceAdapter,
    ItemIdentity,
    SourceCapabilities,
    SourceContractError,
    SourceIdentity,
    SourceRevision,
    digest,
)


def _value(record: Mapping[str, Any], *names: str, default: Any = "") -> Any:
    for name in names:
        if name in record and record[name] is not None:
            return record[name]
    return default


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        values = [_text(item) for item in value.values()]
        return " ".join(item for item in values if item)
    if isinstance(value, (list, tuple)):
        return " ".join(item for item in (_text(row) for row in value) if item)
    return str(value)


def _timestamp(record: Mapping[str, Any], *names: str) -> str:
    value = _value(record, *names, default="1970-01-01T00:00:00Z")
    return str(value)


def _safe_provenance(provider: str, external_id: str, record: Mapping[str, Any], revision: str) -> dict[str, Any]:
    """Keep IDs/hashes only; raw actor/contact/token fields never enter receipts."""
    return {
        "provider": provider,
        "external_id": external_id,
        "revision": revision,
        "raw_payload_hash": digest(record),
    }


class _ProviderFixtureAdapter(FixtureSourceAdapter):
    provider = "provider"

    def __init__(
        self,
        source: SourceIdentity,
        records: Iterable[DemandEnvelope],
        *,
        profile: str,
        allow_writes: bool = False,
    ) -> None:
        capabilities = {"read", "changes", "reconcile"}
        if allow_writes:
            capabilities.update({"status", "comments", "write"})
        super().__init__(
            source,
            records,
            capabilities=SourceCapabilities(
                source.provider, profile, frozenset(capabilities), "fixture"
            ),
        )
        self._allow_writes = allow_writes
        self._delivery_counter = 0

    def deliver(
        self,
        external_id: str,
        patch: Mapping[str, Any],
        *,
        operation_id: str,
        expected_revision: str,
    ) -> DeliveryReceipt:
        before = self.get(external_id)
        if not self._allow_writes:
            return DeliveryReceipt(
                operation_id, before.envelope_id, self.provider, "BLOCKED",
                before.revision_id, before.revision_id, False,
                "write capability is not negotiated",
            )
        if not expected_revision or expected_revision != before.revision_id:
            return DeliveryReceipt(
                operation_id, before.envelope_id, self.provider, "CONFLICT",
                before.revision_id, before.revision_id, True,
                "expected revision is stale or missing",
            )
        self._delivery_counter += 1
        title = str(patch.get("title", before.title))
        body = str(patch.get("body", before.body))
        state = str(patch.get("state", before.state))
        fields = dict(before.provider_fields)
        fields.update(dict(patch.get("provider_fields") or {}))
        revision = SourceRevision(
            before.revision.updated_at,
            f"{before.revision.tie_breaker}:delivery:{self._delivery_counter:08d}",
            token=f"delivery-{self._delivery_counter}",
        )
        updated = replace(
            before,
            title=title,
            body=body,
            state=state,
            provider_fields=fields,
            revision=revision,
        )
        self._items[str(external_id)] = updated
        observed = self.get(external_id)
        verified = (
            observed.envelope_id == before.envelope_id
            and observed.title == title
            and observed.body == body
            and observed.state == state
        )
        return DeliveryReceipt(
            operation_id, before.envelope_id, self.provider,
            "VERIFIED" if verified else "BLOCKED",
            before.revision_id, observed.revision_id, True,
            "" if verified else "post-write re-query mismatch",
        )


class AzureDevOpsSourceAdapter(_ProviderFixtureAdapter):
    """Azure work-item/PR normalization; continuation is represented by SourceCursor."""

    provider = "azure-devops"

    def __init__(
        self,
        organization: str,
        project: str,
        repository: str = "",
        records: Iterable[Mapping[str, Any]] = (),
        *,
        allow_writes: bool = False,
    ) -> None:
        source = SourceIdentity(self.provider, organization, project, repository)
        envelopes = tuple(self._normalize(source, record) for record in records)
        super().__init__(source, envelopes, profile="azure-devops-fixture", allow_writes=allow_writes)

    @classmethod
    def _normalize(cls, source: SourceIdentity, record: Mapping[str, Any]) -> DemandEnvelope:
        fields = record.get("fields") if isinstance(record.get("fields"), Mapping) else record
        external_id = str(_value(record, "id", "work_item_id", "pullRequestId"))
        if not external_id:
            raise SourceContractError("Azure record has no stable id")
        kind = "pull_request" if "pullRequestId" in record or record.get("kind") == "pull_request" else "work_item"
        revision = str(_value(record, "rev", "revision", "etag", default="1"))
        relations = tuple(
            {"type": "explicit_link", "provider": source.provider, "external_id": str(_value(link, "id", "url"))}
            for link in (record.get("relations") or ())
            if isinstance(link, Mapping) and _value(link, "id", "url")
        )
        return DemandEnvelope(
            ItemIdentity(source, external_id, kind),
            SourceRevision(_timestamp(record, "changedDate", "updatedDate", "updated_at"), revision),
            _text(_value(fields, "System.Title", "title", default="Azure work item")),
            _text(_value(fields, "System.Description", "description", "body")),
            _text(_value(fields, "System.State", "state", default="open")),
            provider_fields={
                "organization": source.tenant,
                "project": source.project,
                "repository": source.repository,
                "kind": kind,
                "tags": _value(fields, "System.Tags", "tags", default=""),
            },
            relations=relations,
            raw_provenance=_safe_provenance(source.provider, external_id, record, revision),
        )


class JiraSourceAdapter(_ProviderFixtureAdapter):
    """Jira Cloud fixture adapter; key is mutable, issue ID is the identity."""

    provider = "jira-cloud"

    def __init__(
        self,
        site: str,
        project: str,
        records: Iterable[Mapping[str, Any]] = (),
        *,
        allow_writes: bool = False,
        server_dc: bool = False,
    ) -> None:
        source = SourceIdentity(self.provider, site, project)
        profile = "jira-server-dc-unsupported" if server_dc else "jira-cloud-fixture"
        envelopes = tuple(self._normalize(source, record) for record in records)
        super().__init__(source, envelopes, profile=profile, allow_writes=allow_writes and not server_dc)

    @classmethod
    def _normalize(cls, source: SourceIdentity, record: Mapping[str, Any]) -> DemandEnvelope:
        fields = record.get("fields") if isinstance(record.get("fields"), Mapping) else record
        external_id = str(_value(record, "id", "issue_id"))
        if not external_id:
            raise SourceContractError("Jira record has no stable issue id")
        key = str(_value(record, "key", "issue_key", default=external_id))
        updated = _timestamp(fields, "updated", "updated_at")
        revision = str(_value(record, "version", "revision", default=updated))
        custom = fields.get("custom_fields") if isinstance(fields.get("custom_fields"), Mapping) else {}
        normalized_custom = {
            str(name): {"value": value, "reason": None} if value is not None else {"value": None, "reason": "not_present"}
            for name, value in custom.items()
        }
        return DemandEnvelope(
            ItemIdentity(source, external_id, "issue"),
            SourceRevision(updated, revision),
            _text(_value(fields, "summary", "title", default="Jira issue")),
            _text(_value(fields, "description", "body")),
            _text(_value(fields.get("status") if isinstance(fields.get("status"), Mapping) else fields, "name", "status", default="open")),
            provider_fields={
                "key": key,
                "project": source.project,
                "custom_fields": normalized_custom,
                "cloud": True,
            },
            raw_provenance=_safe_provenance(source.provider, external_id, record, revision),
        )


class AsanaSourceAdapter(_ProviderFixtureAdapter):
    """Asana task normalization; project memberships are relations, not identity."""

    provider = "asana"

    def __init__(
        self,
        workspace: str,
        project: str = "workspace",
        records: Iterable[Mapping[str, Any]] = (),
        *,
        allow_writes: bool = False,
    ) -> None:
        source = SourceIdentity(self.provider, workspace, project)
        envelopes = tuple(self._normalize(source, record) for record in records)
        super().__init__(source, envelopes, profile="asana-fixture", allow_writes=allow_writes)

    @classmethod
    def _normalize(cls, source: SourceIdentity, record: Mapping[str, Any]) -> DemandEnvelope:
        external_id = str(_value(record, "gid", "id", "task_id"))
        if not external_id:
            raise SourceContractError("Asana record has no stable task gid")
        memberships = record.get("memberships") or record.get("projects") or ()
        relations = tuple(
            {"type": "membership", "project": str(_value(row, "gid", "id", "project")), "section": str(_value(row, "section", "section_gid"))}
            for row in memberships if isinstance(row, Mapping)
        )
        revision = str(_value(record, "modified_at", "revision", default="1"))
        return DemandEnvelope(
            ItemIdentity(source, external_id, "task"),
            SourceRevision(_timestamp(record, "modified_at", "updated_at"), revision),
            _text(_value(record, "name", "title", default="Asana task")),
            _text(_value(record, "notes", "body")),
            "completed" if bool(record.get("completed")) else "open",
            provider_fields={
                "workspace": source.tenant,
                "projects": [dict(row) for row in memberships if isinstance(row, Mapping)],
                "sections": [row.get("section") for row in relations],
                "custom_fields": dict(record.get("custom_fields") or {}),
            },
            relations=relations,
            raw_provenance=_safe_provenance(source.provider, external_id, record, revision),
        )


class TrelloSourceAdapter(_ProviderFixtureAdapter):
    """Trello card normalization; list/board moves never change card identity."""

    provider = "trello"

    def __init__(
        self,
        workspace: str,
        board: str,
        records: Iterable[Mapping[str, Any]] = (),
        *,
        allow_writes: bool = False,
    ) -> None:
        source = SourceIdentity(self.provider, workspace, board)
        envelopes = tuple(self._normalize(source, record) for record in records)
        super().__init__(source, envelopes, profile="trello-fixture", allow_writes=allow_writes)

    @classmethod
    def _normalize(cls, source: SourceIdentity, record: Mapping[str, Any]) -> DemandEnvelope:
        external_id = str(_value(record, "id", "card_id"))
        if not external_id:
            raise SourceContractError("Trello record has no stable card id")
        revision = str(_value(record, "lastActionId", "revision", "dateLastActivity", default="1"))
        actions = record.get("actions") or ()
        relations = tuple(
            {"type": "action", "action_id": str(_value(action, "id", "action_id")), "list": str(_value(action, "listAfter", "list_id"))}
            for action in actions if isinstance(action, Mapping) and _value(action, "id", "action_id")
        )
        return DemandEnvelope(
            ItemIdentity(source, external_id, "card"),
            SourceRevision(_timestamp(record, "dateLastActivity", "updated_at"), revision),
            _text(_value(record, "name", "title", default="Trello card")),
            _text(_value(record, "desc", "description", "body")),
            "archived" if bool(record.get("closed") or record.get("archived")) else "open",
            provider_fields={
                "board": source.project,
                "list": str(_value(record, "idList", "list_id")),
                "labels": list(record.get("labels") or ()),
                "checklists": list(record.get("checklists") or ()),
            },
            relations=relations,
            raw_provenance=_safe_provenance(source.provider, external_id, record, revision),
        )


__all__ = [
    "AsanaSourceAdapter",
    "AzureDevOpsSourceAdapter",
    "JiraSourceAdapter",
    "TrelloSourceAdapter",
]
