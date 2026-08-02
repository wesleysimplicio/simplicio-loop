from __future__ import annotations

import json

import pytest

from simplicio_loop.cli_impl import main
from simplicio_loop.source_contract import (
    CursorStore,
    DemandEnvelope,
    FixtureSourceAdapter,
    ItemIdentity,
    SourceContractError,
    SourceCursor,
    SourceIdentity,
    SourceRevision,
    SourceStatus,
    redact,
)
from simplicio_loop.source_fan_in import FanInReducer
from simplicio_loop.source_providers import (
    AsanaSourceAdapter,
    AzureDevOpsSourceAdapter,
    JiraSourceAdapter,
    TrelloSourceAdapter,
)


def _envelope(source: SourceIdentity, external_id: str, *, title: str = "Work", revision: str = "1"):
    return DemandEnvelope(
        ItemIdentity(source, external_id),
        SourceRevision("2026-01-01T00:00:00Z", revision),
        title,
        body="body",
    )


def test_revision_changes_preserve_envelope_identity_and_cursor_is_round_trip():
    source = SourceIdentity("local", "tenant-a", "project-a")
    first = _envelope(source, "item-1", revision="1")
    second = DemandEnvelope(first.identity, SourceRevision("2026-01-02T00:00:00Z", "2"), "Work v2")
    assert first.envelope_id == second.envelope_id
    assert first.revision_id != second.revision_id
    assert first.idempotency_key != second.idempotency_key

    cursor = SourceCursor("2026-01-02T00:00:00Z", "2", opaque="continuation")
    assert SourceCursor.from_token(cursor.token()) == cursor


def test_cursor_only_advances_with_durable_receipt_and_recovery_is_idempotent(tmp_path):
    source = SourceIdentity("local", "tenant-a", "project-a")
    adapter = FixtureSourceAdapter(source, [_envelope(source, "item-1")])
    store = CursorStore(tmp_path / "cursor.json")
    persisted = []

    def persist(item):
        persisted.append(item.envelope_id)

    receipt = adapter.ingest(store, persist, operation_id="op-1")
    assert receipt.durable is True
    assert store.get(source.source_key) is not None
    assert persisted == [persisted[0]]

    adapter.injected_status = SourceStatus.RATE_LIMITED
    failed = adapter.ingest(store, persist, operation_id="op-2")
    assert failed.status == SourceStatus.RATE_LIMITED
    assert failed.durable is False
    assert store.get(source.source_key).token() == receipt.cursor_after

    with pytest.raises(SourceContractError, match="durable receipt"):
        store.commit(source.source_key, store.get(source.source_key), failed)


def test_redaction_never_emits_credentials():
    value = redact({
        "authorization": "Bearer secret-token",
        "nested": {"api_key": "abc", "message": "https://example.test?a=1&token=secret"},
        "safe_id": "item-1",
    })
    encoded = json.dumps(value)
    assert "secret-token" not in encoded
    assert "abc" not in encoded
    assert "safe_id" in encoded


@pytest.mark.parametrize(
    "adapter",
    [
        AzureDevOpsSourceAdapter("org", "project", records=[{"id": 1, "rev": 1, "title": "A"}]),
        JiraSourceAdapter("site", "PROJ", records=[{"id": "100", "key": "PROJ-1", "fields": {"summary": "A", "updated": "2026-01-01"}}]),
        AsanaSourceAdapter("workspace", records=[{"gid": "task-1", "name": "A", "modified_at": "2026-01-01"}]),
        TrelloSourceAdapter("workspace", "board", records=[{"id": "card-1", "name": "A", "dateLastActivity": "2026-01-01"}]),
    ],
)
def test_provider_adapters_emit_the_same_page_contract(adapter):
    page = adapter.list_changes(limit=10)
    assert page.status == SourceStatus.OK
    assert len(page.items) == 1
    assert page.items[0].as_dict()["schema"] == "simplicio.source-adapter/v1"
    assert adapter.capabilities().transport == "fixture"
    assert adapter.capabilities().profile.endswith("fixture") or "unsupported" in adapter.capabilities().profile


def test_jira_key_rename_does_not_change_identity():
    adapter = JiraSourceAdapter("site", "PROJ", records=[{
        "id": "100", "key": "PROJ-1", "fields": {"summary": "Rename me", "updated": "2026-01-01"}
    }])
    first = adapter.get("100")
    adapter._items["100"] = DemandEnvelope(
        first.identity,
        SourceRevision("2026-01-02", "2"),
        first.title,
        provider_fields={"key": "PROJ-99"},
    )
    second = adapter.get("100")
    assert first.envelope_id == second.envelope_id
    assert first.provider_fields["key"] != second.provider_fields["key"]


def test_asana_memberships_and_trello_moves_do_not_duplicate_identity():
    asana = AsanaSourceAdapter("workspace", records=[{
        "gid": "task-1", "name": "Shared", "modified_at": "2026-01-01",
        "memberships": [{"gid": "p1", "section": "s1"}, {"gid": "p2", "section": "s2"}],
    }])
    assert len(asana.get("task-1").relations) == 2
    trello = TrelloSourceAdapter("workspace", "board", records=[{
        "id": "card-1", "name": "Moved", "idList": "list-1", "dateLastActivity": "2026-01-01"
    }])
    first = trello.get("card-1")
    trello._items["card-1"] = DemandEnvelope(
        first.identity, SourceRevision("2026-01-02", "2"), first.title,
        provider_fields={"list": "list-2"},
    )
    assert trello.get("card-1").envelope_id == first.envelope_id


def test_provider_delivery_requires_capability_revision_and_requery():
    adapter = JiraSourceAdapter("site", "PROJ", records=[{
        "id": "100", "key": "PROJ-1", "fields": {"summary": "A", "updated": "2026-01-01"}
    }], allow_writes=True)
    item = adapter.get("100")
    blocked = adapter.deliver("100", {"title": "B"}, operation_id="op-1", expected_revision="stale")
    assert blocked.status == "CONFLICT"
    verified = adapter.deliver("100", {"title": "B"}, operation_id="op-2", expected_revision=item.revision_id)
    assert verified.status == "VERIFIED"
    assert verified.requery_observed is True


def test_fan_in_merges_explicit_aliases_but_not_similarity_and_freezes_delivery():
    github = SourceIdentity("github", "org", "repo")
    jira = SourceIdentity("jira-cloud", "site", "PROJ")
    first = _envelope(github, "42", title="Ship parser")
    linked = DemandEnvelope(
        ItemIdentity(jira, "100"),
        SourceRevision("2026-01-01T00:00:01Z", "1"),
        "Ship parser",
        body="body",
        relations=({"type": "explicit_link", "provider": "github", "tenant": "org", "project": "repo", "external_id": "42"},),
    )
    similar = _envelope(jira, "101", title="Ship parser now")
    reducer = FanInReducer(similarity_threshold=0.8)
    first_result = reducer.ingest(first)
    linked_result = reducer.ingest(linked)
    similar_result = reducer.ingest(similar)
    assert first_result["status"] == "ADMITTED"
    assert linked_result["demand_id"] == first_result["demand_id"]
    assert linked_result["provenance_count"] == 2
    assert similar_result["demand_id"] != first_result["demand_id"]
    assert first_result["demand_id"] in similar_result["suggestions"]

    frozen = reducer.freeze_delivery(first_result["demand_id"], github, "42")
    assert frozen["status"] == "FROZEN"
    assert reducer.freeze_delivery(first_result["demand_id"], jira, "100")["status"] == "CONFLICT"
    assert reducer.deliver(
        first_result["demand_id"], provider="github",
        write=lambda item: {"ok": True},
        requery=lambda item: {"envelope_id": item.envelope_id},
    )["status"] == "VERIFIED"


def test_fan_in_governed_conflict_and_partial_source_are_fail_closed():
    source = SourceIdentity("local", "tenant", "project")
    reducer = FanInReducer()
    first = _envelope(source, "1", title="One")
    second = DemandEnvelope(
        ItemIdentity(source, "2"), SourceRevision("2026-01-01", "2"), "Two",
        relations=({"type": "explicit_link", "provider": "local", "tenant": "tenant", "project": "project", "external_id": "1"},),
    )
    assert reducer.ingest(first)["status"] == "ADMITTED"
    conflict = reducer.ingest(second)
    assert conflict["status"] == "CONFLICT"
    page = FixtureSourceAdapter(source).list_changes()
    assert reducer.ingest_page(page) == []
    page = type(page)(SourceStatus.RATE_LIMITED, None, error_code="429", retry_after_seconds=3)
    result = reducer.ingest_page(page)
    assert result[0]["status"] == "SOURCE_UNAVAILABLE"
    assert result[0]["source_status"] == "rate_limited"


def test_fan_in_revision_updates_are_ordered_and_stale_replay_cannot_regress():
    source = SourceIdentity("github", "org", "repo")
    first = _envelope(source, "42", title="Old title", revision="1")
    second = DemandEnvelope(
        first.identity,
        SourceRevision("2026-01-02T00:00:00Z", "2"),
        "New title",
        body="new body",
    )
    reducer = FanInReducer()

    assert reducer.ingest(first)["status"] == "ADMITTED"
    updated = reducer.ingest(second)
    assert updated["status"] == "ADMITTED"
    assert updated["conflict_fields"] == []

    stale = reducer.ingest(first)
    assert stale["status"] == "ADMITTED"
    stored = reducer.inspect()["demands"][0]["envelopes"]
    assert len(stored) == 1
    assert stored[0]["title"] == "New title"
    assert stored[0]["revision"]["tie_breaker"] == "2"



def test_source_and_resource_doctors_are_read_only(tmp_path, capsys):
    assert main(["doctor", "source", "--provider", "jira-cloud", "--json"]) == 0
    source = json.loads(capsys.readouterr().out)
    assert source["status"] == "READY"
    assert source["real_provider_auth"] == "UNVERIFIED"
    assert source["effects_attempted"] is False

    assert main(["doctor", "resource", "--root", str(tmp_path), "--json"]) == 2
    missing = json.loads(capsys.readouterr().out)
    assert missing["reason_code"] == "RESOURCE_FABRIC_NOT_STARTED"
    assert not (tmp_path / "resource-fabric.sqlite").exists()

    state = tmp_path / "resource-fabric.json"
    state.write_text(json.dumps({"schema": "simplicio.resource-fabric/v1", "draining": False}), encoding="utf-8")
    assert main(["doctor", "resource", "--root", str(tmp_path), "--json"]) == 0
    observed = json.loads(capsys.readouterr().out)
    assert observed["status"] == "OBSERVED"
