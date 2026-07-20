from __future__ import annotations

import json
import fcntl

import pytest

from simplicio_loop.github_drain_intake import (
    PLANNER_REVISION,
    PLANNED_NOT_EXECUTED_EXIT,
    DrainCheckpointError,
    GitHubDrainIntake,
)


class ReadOnlyGitHub:
    provider = "github"

    def __init__(self, repo, issues):
        self.repo = repo
        self.issues = {int(item["number"]): dict(item, state=item.get("state", "open")) for item in issues}
        self.list_calls = 0
        self.detail_calls = []
        self.requery_calls = []
        self.effect_calls = []

    def list_ready(self, *, state="open", labels=(), assignee="", milestone=""):
        self.list_calls += 1
        items = [
            {
                "number": number,
                "title": issue["title"],
                "state": issue["state"],
                "labels": list(issue.get("labels", [])),
                "url": "https://github.com/%s/issues/%d" % (self.repo, number),
            }
            for number, issue in sorted(self.issues.items())
            if issue["state"] == state
        ]
        return {
            "schema": "simplicio.github-list-ready/v1",
            "provider": "github",
            "repo": self.repo,
            "items": items,
            "count": len(items),
            "observed_at": "2026-07-19T00:00:%02dZ" % min(self.list_calls, 59),
        }

    def get_details(self, ref):
        self.detail_calls.append(int(ref))
        return self._snapshot(int(ref))

    def requery(self, ref, *, comment_id=None):
        self.requery_calls.append(int(ref))
        value = self._snapshot(int(ref))
        value["requeried_at"] = "2026-07-19T00:01:00Z"
        return value

    def _snapshot(self, number):
        issue = self.issues[number]
        revision = issue.get("revision") or "rev-%d-%s" % (number, issue["state"])
        return {
            "schema": "simplicio.github-source-snapshot/v1",
            "provider": "github",
            "repo": self.repo,
            "issue": str(number),
            "url": "https://github.com/%s/issues/%d" % (self.repo, number),
            "title": issue["title"],
            "body": issue.get("body", ""),
            "labels": list(issue.get("labels", [])),
            "state": issue["state"],
            "source_revision": revision,
            "observed_at": "2026-07-19T00:00:00Z",
        }

    def close_remotely(self, number):
        self.issues[int(number)]["state"] = "closed"
        self.issues[int(number)].pop("revision", None)

    def claim(self, *_args, **_kwargs):
        self.effect_calls.append("claim")
        raise AssertionError("intake must not claim")

    def update_status(self, *_args, **_kwargs):
        self.effect_calls.append("update_status")
        raise AssertionError("intake must not update GitHub")

    def close(self, *_args, **_kwargs):
        self.effect_calls.append("close")
        raise AssertionError("intake must not close GitHub")


class CanonicalMap:
    def __init__(self, receipt=None):
        self.calls = 0
        self.receipt = receipt or {
            "schema": "simplicio.github-drain-map/v1",
            "status": "ready",
            "mode": "canonical",
            "cache_key": "canonical-1",
        }

    def prepare_canonical(self, repository, workspace):
        self.calls += 1
        return dict(self.receipt, repository=repository)


def _run(tmp_path, source, *, mapping=None, checkpoint="intake.json"):
    return GitHubDrainIntake(
        source=source,
        checkpoint=tmp_path / checkpoint,
        workspace=str(tmp_path),
        map_reader=mapping,
    ).run("termine todas as issues do projeto acme/widgets")


def test_read_only_intake_plans_waves_replays_and_never_authorizes_effects(tmp_path):
    source = ReadOnlyGitHub("acme/widgets", [
        {"number": 1, "title": "base"},
        {"number": 2, "title": "[P0] dependent", "body": "Depends on #1"},
    ])
    mapping = CanonicalMap()
    first = _run(tmp_path, source, mapping=mapping)
    replay = _run(tmp_path, source, mapping=mapping)

    assert first["outcome"]["status"] == "PLANNED_NOT_EXECUTED"
    assert first["outcome"]["exit_code"] == PLANNED_NOT_EXECUTED_EXIT
    assert first["execution_authorized"] is False
    assert first["plan"]["waves"] == [
        {"index": 1, "issues": [1], "risk_order": ["low"]},
        {"index": 2, "issues": [2], "risk_order": ["high"]},
    ]
    assert replay["plan"]["issue_count"] == 2
    assert sorted(replay["items"]) == ["1", "2"]
    assert mapping.calls == 1
    assert replay["map"]["overlays"] == {}
    assert replay["metering"] == {
        "measurement_state": "unmeasured", "tokens": None, "cost_usd": None,
    }
    assert replay["planner_revision"] == PLANNER_REVISION
    assert set(replay["digests"]) == {"config", "task", "run"}
    assert replay["run_identity"]["request_digest"] == replay["digests"]["task"]
    assert first["run_identity"] == replay["run_identity"]
    assert source.effect_calls == []


def test_external_closed_dependency_is_read_only_evidence_but_open_dependency_blocks(tmp_path):
    closed_source = ReadOnlyGitHub("acme/widgets", [
        {"number": 1, "title": "child", "body": "Requires #9"},
        {"number": 9, "title": "old", "state": "closed"},
    ])
    planned = _run(tmp_path, closed_source, checkpoint="closed-dep.json")
    assert planned["outcome"]["status"] == "PLANNED_NOT_EXECUTED"
    assert planned["items"]["1"]["external_dependencies_closed"] == [9]
    assert planned["external_dependencies"]["9"]["state"] == "closed"

    open_source = ReadOnlyGitHub("acme/widgets", [
        {"number": 1, "title": "child", "body": "Requires #9"},
        {"number": 9, "title": "dependency", "state": "open"},
    ])
    # A real all-open listing includes #9, so it is planned in an earlier wave.
    accepted = _run(tmp_path, open_source, checkpoint="open-dep.json")
    assert [wave["issues"] for wave in accepted["plan"]["waves"]] == [[9], [1]]


def test_cycle_and_self_dependency_fail_closed_without_effects(tmp_path):
    cycle = ReadOnlyGitHub("acme/widgets", [
        {"number": 1, "title": "one", "body": "Depends on #2"},
        {"number": 2, "title": "two", "body": "Depends on #1"},
    ])
    result = _run(tmp_path, cycle, checkpoint="cycle.json")
    assert result["outcome"]["status"] == "BLOCKED"
    assert result["outcome"]["reason_code"] == "dependency_cycle"
    assert cycle.effect_calls == []

    self_dep = ReadOnlyGitHub("acme/widgets", [
        {"number": 3, "title": "self", "body": "Blocked by #3"},
    ])
    blocked = _run(tmp_path, self_dep, checkpoint="self.json")
    assert blocked["outcome"]["reason_code"] == "dependency_cycle"


def test_replay_observes_remote_close_without_executing_and_blocks_source_drift(tmp_path):
    source = ReadOnlyGitHub("acme/widgets", [{"number": 7, "title": "seven", "revision": "r1"}])
    first = _run(tmp_path, source, checkpoint="replay.json")
    assert first["plan"]["issue_count"] == 1
    source.close_remotely(7)
    closed = _run(tmp_path, source, checkpoint="replay.json")
    assert closed["items"]["7"]["state"] == "remote_closed"
    assert closed["plan"]["issue_count"] == 0
    assert source.effect_calls == []

    drift = ReadOnlyGitHub("acme/widgets", [{"number": 8, "title": "eight", "revision": "r1"}])
    _run(tmp_path, drift, checkpoint="drift.json")
    drift.issues[8]["revision"] = "r2"
    changed = _run(tmp_path, drift, checkpoint="drift.json")
    assert changed["outcome"]["reason_code"] == "source_revision_changed"


def test_checkpoint_integrity_and_scope_are_verified(tmp_path):
    source = ReadOnlyGitHub("acme/widgets", [{"number": 1, "title": "one"}])
    _run(tmp_path, source)
    path = tmp_path / "intake.json"
    payload = json.loads(path.read_text())
    payload["items"]["1"]["title"] = "tampered"
    path.write_text(json.dumps(payload))
    with pytest.raises(DrainCheckpointError) as integrity:
        _run(tmp_path, source)
    assert integrity.value.reason_code == "checkpoint_integrity_failed"

    other = ReadOnlyGitHub("other/repo", [])
    controller = GitHubDrainIntake(
        source=other, checkpoint=tmp_path / "scope.json", workspace=str(tmp_path)
    )
    controller.run("finish all issues in other/repo")
    with pytest.raises(DrainCheckpointError) as scope:
        GitHubDrainIntake(
            source=source, checkpoint=tmp_path / "scope.json", workspace=str(tmp_path)
        ).run("finish all issues in acme/widgets")
    assert scope.value.reason_code == "checkpoint_scope_mismatch"


@pytest.mark.parametrize("tamper", ["created_at", "outcome"])
def test_checkpoint_integrity_binds_metadata_and_outcome(tmp_path, tamper):
    source = ReadOnlyGitHub("acme/widgets", [{"number": 1, "title": "one"}])
    checkpoint = "%s.json" % tamper
    _run(tmp_path, source, checkpoint=checkpoint)
    path = tmp_path / checkpoint
    payload = json.loads(path.read_text())
    if tamper == "created_at":
        payload["created_at"] = "2099-01-01T00:00:00Z"
    else:
        payload["outcome"].update(
            status="COMPLETE", exit_code=0, execution_authorized=True
        )
    path.write_text(json.dumps(payload))

    with pytest.raises(DrainCheckpointError) as excinfo:
        _run(tmp_path, source, checkpoint=checkpoint)
    assert excinfo.value.reason_code == "checkpoint_integrity_failed"


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [("COMPLETE", 0), ("PLANNED_NOT_EXECUTED", True)],
)
def test_checkpoint_rejects_rehashed_complete_or_bool_exit_outcome(
    tmp_path, status, exit_code
):
    from simplicio_loop.github_drain_intake import _digest, _integrity_payload

    source = ReadOnlyGitHub("acme/widgets", [])
    _run(tmp_path, source, checkpoint="outcome-source.json")
    payload = json.loads((tmp_path / "outcome-source.json").read_text())
    payload["outcome"].update(status=status, exit_code=exit_code)
    payload["integrity_hash"] = _digest(_integrity_payload(payload))
    checkpoint = tmp_path / ("outcome-%s.json" % str(exit_code).lower())
    checkpoint.write_text(json.dumps(payload))

    with pytest.raises(DrainCheckpointError) as excinfo:
        GitHubDrainIntake(
            source=source, checkpoint=checkpoint, workspace=str(tmp_path)
        ).run("termine todas as issues do projeto acme/widgets")
    assert excinfo.value.reason_code == "checkpoint_invalid"


def test_checkpoint_request_digest_prevents_synonym_rebinding(tmp_path):
    source = ReadOnlyGitHub("acme/widgets", [])
    checkpoint = tmp_path / "request.json"
    _run(tmp_path, source, checkpoint="request.json")

    with pytest.raises(DrainCheckpointError) as excinfo:
        GitHubDrainIntake(
            source=source, checkpoint=checkpoint, workspace=str(tmp_path)
        ).run("finish all issues in acme/widgets")
    assert excinfo.value.reason_code == "checkpoint_request_mismatch"


@pytest.mark.parametrize(
    "metering",
    [
        {"measurement_state": "measured", "tokens": True, "cost_usd": 1.0},
        {"measurement_state": "measured", "tokens": 10, "cost_usd": None},
        {"measurement_state": "unmeasured", "tokens": 0, "cost_usd": None},
    ],
)
def test_checkpoint_rejects_bool_partial_or_fabricated_metering(tmp_path, metering):
    from simplicio_loop.github_drain_intake import _digest, _integrity_payload

    source = ReadOnlyGitHub("acme/widgets", [])
    _run(tmp_path, source, checkpoint="metering-source.json")
    payload = json.loads((tmp_path / "metering-source.json").read_text())
    payload["metering"] = metering
    payload["integrity_hash"] = _digest(_integrity_payload(payload))
    checkpoint = tmp_path / ("metering-%d.json" % len(list(tmp_path.glob("metering-*.json"))))
    checkpoint.write_text(json.dumps(payload))

    with pytest.raises(DrainCheckpointError) as excinfo:
        GitHubDrainIntake(
            source=source, checkpoint=checkpoint, workspace=str(tmp_path)
        ).run("termine todas as issues do projeto acme/widgets")
    assert excinfo.value.reason_code == "checkpoint_invalid"


@pytest.mark.parametrize(
    "field", ["planner_revision", "planner_config", "config", "task", "run"]
)
def test_checkpoint_rejects_rehashed_planner_and_digest_tampering(tmp_path, field):
    from simplicio_loop.github_drain_intake import _digest, _integrity_payload

    source = ReadOnlyGitHub("acme/widgets", [])
    source_path = tmp_path / "identity-source.json"
    if not source_path.exists():
        _run(tmp_path, source, checkpoint=source_path.name)
    payload = json.loads(source_path.read_text())
    if field == "planner_revision":
        payload[field] = "untrusted-planner/999"
    elif field == "planner_config":
        payload[field]["mode"] = "execute"
    else:
        payload["digests"][field] = "0" * 64
    payload["integrity_hash"] = _digest(_integrity_payload(payload))
    checkpoint = tmp_path / ("identity-%s.json" % field)
    checkpoint.write_text(json.dumps(payload))

    with pytest.raises(DrainCheckpointError) as excinfo:
        GitHubDrainIntake(
            source=source, checkpoint=checkpoint, workspace=str(tmp_path)
        ).run("termine todas as issues do projeto acme/widgets")
    assert excinfo.value.reason_code == "checkpoint_identity_invalid"


def test_fault_injected_pull_request_summary_and_snapshot_are_excluded(tmp_path):
    summary_source = ReadOnlyGitHub("acme/widgets", [{"number": 1, "title": "not an issue"}])
    original_listing = summary_source.list_ready

    def pr_listing(**kwargs):
        listing = original_listing(**kwargs)
        listing["items"][0]["pull_request"] = {
            "url": "https://api.github.test/repos/acme/widgets/pulls/1"
        }
        return listing

    summary_source.list_ready = pr_listing
    summary_result = _run(tmp_path, summary_source, checkpoint="pr-summary.json")
    assert summary_result["outcome"]["reason_code"] == "github_pull_request_excluded"

    snapshot_source = ReadOnlyGitHub("acme/widgets", [{"number": 2, "title": "also a PR"}])
    original_details = snapshot_source.get_details

    def pr_snapshot(ref):
        snapshot = original_details(ref)
        snapshot["type"] = "pull_request"
        return snapshot

    snapshot_source.get_details = pr_snapshot
    snapshot_result = _run(tmp_path, snapshot_source, checkpoint="pr-snapshot.json")
    assert snapshot_result["outcome"]["reason_code"] == "github_pull_request_excluded"


def test_no_map_is_honestly_unsupported_and_invalid_map_or_listing_blocks(tmp_path):
    source = ReadOnlyGitHub("acme/widgets", [])
    result = _run(tmp_path, source, mapping=None, checkpoint="no-map.json")
    assert result["map"]["canonical"]["status"] == "unsupported"
    assert result["outcome"]["status"] == "PLANNED_NOT_EXECUTED"

    invalid_map = _run(
        tmp_path, ReadOnlyGitHub("acme/widgets", []),
        mapping=CanonicalMap({"status": "ready", "mode": "overlay"}),
        checkpoint="bad-map.json",
    )
    assert invalid_map["outcome"]["reason_code"] == "canonical_map_invalid"

    bad_source = ReadOnlyGitHub("acme/widgets", [])
    bad_source.repo = "other/repo"
    blocked = _run(tmp_path, bad_source, checkpoint="bad-listing.json")
    assert blocked["outcome"]["reason_code"] == "github_listing_scope_mismatch"


@pytest.mark.parametrize(
    "kind",
    [
        "not_mapping", "count", "invalid_id", "non_open",
    ],
)
def test_malformed_listings_fail_closed(tmp_path, kind):
    source = ReadOnlyGitHub("acme/widgets", [])
    original = source.list_ready

    def malformed(**kwargs):
        value = original(**kwargs)
        if kind == "not_mapping":
            return None
        if kind == "count":
            value["count"] = 1
        elif kind == "invalid_id":
            value["items"].append({"number": 0, "state": "open"})
            value["count"] = 1
        elif kind == "non_open":
            value["items"].append({"number": 1, "state": "closed"})
            value["count"] = 1
        return value

    source.list_ready = malformed
    result = _run(tmp_path, source, checkpoint="bad-%s.json" % kind)
    assert result["outcome"]["reason_code"] == "github_listing_invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "other"),
        ("repo", "other/repo"),
        ("issue", "99"),
        ("state", "unknown"),
        ("source_revision", ""),
    ],
)
def test_malformed_issue_snapshots_fail_closed(tmp_path, field, value):
    source = ReadOnlyGitHub("acme/widgets", [{"number": 1, "title": "one"}])
    original = source.get_details

    def malformed(ref):
        snapshot = original(ref)
        snapshot[field] = value
        return snapshot

    source.get_details = malformed
    result = _run(tmp_path, source, checkpoint="snapshot-%s.json" % field)
    assert result["outcome"]["reason_code"] == "github_snapshot_invalid"


def test_listing_snapshot_disagreement_and_incomplete_replay_block(tmp_path):
    source = ReadOnlyGitHub("acme/widgets", [{"number": 1, "title": "one"}])
    original_details = source.get_details

    def closed_details(ref):
        snapshot = original_details(ref)
        snapshot["state"] = "closed"
        return snapshot

    source.get_details = closed_details
    mismatch = _run(tmp_path, source, checkpoint="state-mismatch.json")
    assert mismatch["outcome"]["reason_code"] == "github_snapshot_state_mismatch"

    replay = ReadOnlyGitHub("acme/widgets", [{"number": 2, "title": "two", "revision": "same"}])
    _run(tmp_path, replay, checkpoint="incomplete.json")
    original_listing = replay.list_ready

    def omitted(**kwargs):
        listing = original_listing(**kwargs)
        listing["items"] = []
        listing["count"] = 0
        return listing

    replay.list_ready = omitted
    blocked = _run(tmp_path, replay, checkpoint="incomplete.json")
    assert blocked["outcome"]["reason_code"] == "github_listing_incomplete"


def test_hidden_open_external_dependency_and_unexpected_adapter_error_fail(tmp_path):
    source = ReadOnlyGitHub("acme/widgets", [
        {"number": 1, "title": "one", "body": "Requires #9"},
        {"number": 9, "title": "hidden"},
    ])
    original_listing = source.list_ready

    def hide_dependency(**kwargs):
        listing = original_listing(**kwargs)
        listing["items"] = [item for item in listing["items"] if item["number"] != 9]
        listing["count"] = len(listing["items"])
        return listing

    source.list_ready = hide_dependency
    blocked = _run(tmp_path, source, checkpoint="hidden.json")
    assert blocked["outcome"]["reason_code"] == "dependency_unresolved"

    class ExplodingMap:
        def prepare_canonical(self, repository, workspace):
            raise RuntimeError("boom")

    failed = _run(
        tmp_path, ReadOnlyGitHub("acme/widgets", []),
        mapping=ExplodingMap(), checkpoint="explode.json",
    )
    assert failed["outcome"]["status"] == "FAILED"
    assert failed["outcome"]["exit_code"] == 4


def test_checkpoint_schema_workspace_items_and_lock_fail_closed(tmp_path):
    source = ReadOnlyGitHub("acme/widgets", [])
    cases = [
        ({"schema": "wrong"}, "checkpoint_invalid"),
        ({"schema": "not-json"}, "checkpoint_invalid"),
    ]
    for index, (payload, reason) in enumerate(cases):
        path = tmp_path / ("manual-%d.json" % index)
        if index == 1:
            path.write_text("not json")
        else:
            path.write_text(json.dumps(payload))
        with pytest.raises(DrainCheckpointError) as excinfo:
            GitHubDrainIntake(source=source, checkpoint=path, workspace=str(tmp_path)).run(
                "finish all issues in acme/widgets"
            )
        assert excinfo.value.reason_code == reason

    valid_path = tmp_path / "valid.json"
    _run(tmp_path, source, checkpoint="valid.json")
    payload = json.loads(valid_path.read_text())

    def rewritten(name, mutate):
        value = json.loads(json.dumps(payload))
        mutate(value)
        from simplicio_loop.github_drain_intake import _digest, _integrity_payload
        value["integrity_hash"] = _digest(_integrity_payload(value))
        path = tmp_path / name
        path.write_text(json.dumps(value))
        return path

    wrong_workspace = rewritten("workspace.json", lambda value: value.update(workspace="/tmp/other"))
    with pytest.raises(DrainCheckpointError) as excinfo:
        GitHubDrainIntake(source=source, checkpoint=wrong_workspace, workspace=str(tmp_path)).run(
            "finish all issues in acme/widgets"
        )
    assert excinfo.value.reason_code == "checkpoint_workspace_mismatch"

    bad_items = rewritten("items.json", lambda value: value.update(items=[]))
    with pytest.raises(DrainCheckpointError) as excinfo:
        GitHubDrainIntake(source=source, checkpoint=bad_items, workspace=str(tmp_path)).run(
            "termine todas as issues do projeto acme/widgets"
        )
    assert excinfo.value.reason_code == "checkpoint_invalid"

    lock_path = valid_path.with_suffix(".json.lock")
    with lock_path.open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(DrainCheckpointError) as excinfo:
            _run(tmp_path, source, checkpoint="valid.json")
    assert excinfo.value.reason_code == "checkpoint_locked"
