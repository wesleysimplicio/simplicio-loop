"""Unit coverage for runtime-neutral GitHub lifecycle status synchronization."""
from scripts.github_status_sync import (
    GitHubError,
    LIFECYCLE_MARKER,
    extract_state,
    status_label,
    sync_event,
)


class FakeApi:
    def __init__(self):
        self.calls = []

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == "GET":
            return [{"name": "keep-me"}, {"name": "simplicio:status:planned"}]
        if (method == "POST" and path.endswith("/labels") and
                payload.get("name") == status_label("IN_PROGRESS")):
            raise GitHubError(422, "already exists")
        return {}

    def graphql(self, query, variables):
        self.calls.append(("GRAPHQL", query, variables))
        if query.lstrip().startswith("mutation"):
            return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "item-1"}}}
        return {"repository": {"projectV2": {
            "id": "project-1",
            "fields": {"nodes": [{"id": "status-1", "name": "Status",
                                    "options": [{"id": "todo", "name": "Todo"},
                                                 {"id": "progress", "name": "In Progress"}]}]},
            "items": {"nodes": [{"id": "item-1", "content": {
                "number": 415, "repository": {"nameWithOwner": "acme/repo"}}}]},
        }}}


def test_extract_state_requires_canonical_marker_and_accepts_rendered_table():
    body = "| Campo | Valor |\n|---|---|\n| Estado | IN_PROGRESS |\n" + LIFECYCLE_MARKER
    assert extract_state(body) == "IN_PROGRESS"
    assert extract_state("| Estado | IN_PROGRESS |") is None
    assert extract_state("human comment " + LIFECYCLE_MARKER) is None


def test_sync_event_updates_label_and_project_for_any_runtime():
    api = FakeApi()
    body = ("| Estado | IN_PROGRESS |\n| Runtime / device | kiro / host-1 |\n" + LIFECYCLE_MARKER)
    result = sync_event(api, "acme/repo", "issue_comment", {
        "action": "created", "issue": {"number": 415}, "comment": {"body": body},
    }, project_number=7, project_owner="acme")
    assert result == {"status": "synced", "issue": 415, "state": "IN_PROGRESS",
                      "label": "simplicio:status:in_progress", "project_moved": True,
                      "project_number": 7, "project_reason": "moved"}
    assert any(call[0] == "GRAPHQL" and str(call[2].get("option")) == "progress"
               for call in api.calls)


def test_human_comments_and_unhandled_events_are_noops():
    api = FakeApi()
    assert sync_event(api, "acme/repo", "issue_comment", {
        "action": "created", "issue": {"number": 415}, "comment": {"body": "hello"},
    })["status"] == "skipped"
    assert sync_event(api, "acme/repo", "issues", {
        "action": "labeled", "issue": {"number": 415},
    })["status"] == "skipped"


def test_issue_close_and_reopen_are_reconciled_without_comments():
    api = FakeApi()
    closed = sync_event(api, "acme/repo", "issues", {"action": "closed", "issue": {"number": 415}})
    reopened = sync_event(api, "acme/repo", "issues", {"action": "reopened", "issue": {"number": 415}})
    assert closed["state"] == "CLOSED"
    assert reopened["state"] == "IN_PROGRESS"


class AutoProjectApi(FakeApi):
    def graphql(self, query, variables):
        self.calls.append(("GRAPHQL", query, variables))
        if "projectsV2" in query:
            return {"repository": {"projectsV2": {"nodes": [{"number": 9, "title": "repo"}]}}}
        if "addProjectV2ItemById" in query:
            return {"addProjectV2ItemById": {"item": {"id": "item-added"}}}
        if "issue(number" in query:
            return {"repository": {"issue": {"id": "issue-1"}}}
        if query.lstrip().startswith("mutation"):
            return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "item-added"}}}
        return {"repository": {"projectV2": {
            "id": "project-1",
            "fields": {"nodes": [{"id": "status-1", "name": "Status",
                                    "options": [{"id": "progress", "name": "In Progress"}]}]},
            "items": {"nodes": []},
        }}}


def test_sync_event_discovers_repo_project_and_adds_missing_issue():
    api = AutoProjectApi()
    body = "| Estado | IN_PROGRESS |\n" + LIFECYCLE_MARKER
    result = sync_event(api, "acme/repo", "issue_comment", {
        "action": "created", "issue": {"number": 415}, "comment": {"body": body},
    })
    assert result["project_number"] == 9
    assert result["project_moved"] is True
    assert any("addProjectV2ItemById" in call[1] for call in api.calls if call[0] == "GRAPHQL")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
