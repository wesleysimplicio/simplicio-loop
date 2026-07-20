from __future__ import annotations

import json

import pytest

from simplicio_loop.github_drain_admission import (
    DrainAdmissionProjectionError,
    admission_input_digest,
    build_admission_request,
    load_and_project_checkpoint,
    project_checkpoint,
    validate_projected_job,
)
from simplicio_loop.github_drain_intake import _digest, _integrity_payload
from tests.test_github_drain_intake_integration import CanonicalMap, ReadOnlyGitHub, _run


def _planned_checkpoint(tmp_path):
    source = ReadOnlyGitHub("acme/widgets", [
        {"number": 1, "title": "base"},
        {"number": 2, "title": "dependent", "body": "Depends on #1"},
    ])
    state = _run(tmp_path, source, mapping=CanonicalMap(), checkpoint="final.json")
    assert state["outcome"]["status"] == "PLANNED_NOT_EXECUTED"
    return tmp_path / "final.json", state, source


def _rehash(value):
    value["integrity_hash"] = _digest(_integrity_payload(value))
    return value


def test_final_627_checkpoint_projects_one_non_dispatchable_root_without_paths(tmp_path):
    path, checkpoint, source = _planned_checkpoint(tmp_path)
    calls_before = (source.list_calls, list(source.detail_calls), list(source.requery_calls))
    job = project_checkpoint(checkpoint)
    loaded = load_and_project_checkpoint(path)
    assert loaded == job
    assert job["schema"] == "simplicio.github-drain-job/v1"
    assert job["kind"] == "github_drain_root"
    assert job["dispatchable"] is False
    assert job["activation_required"] is True
    assert job["execution_authorized"] is False
    assert job["run_digest"] == checkpoint["digests"]["run"]
    assert job["checkpoint_digest"] == checkpoint["integrity_hash"]
    assert job["source_digest"] == checkpoint["source_observation"]["digest"]
    assert job["plan_digest"] == _digest(job["plan"])
    assert job["workspace_digest"] == _digest(checkpoint["workspace"])
    assert job["issue_count"] == 2
    assert [wave["issues"] for wave in job["plan"]["waves"]] == [[1], [2]]
    assert str(tmp_path) not in json.dumps(job, sort_keys=True)
    assert "root" not in job["canonical_map"]
    assert calls_before == (source.list_calls, source.detail_calls, source.requery_calls)
    assert source.effect_calls == []


def test_identity_is_derived_and_input_digest_covers_job_and_all_metadata(tmp_path):
    _path, checkpoint, _source = _planned_checkpoint(tmp_path)
    job = project_checkpoint(checkpoint)
    request = build_admission_request(
        job, client_id="client-a", workspace_id="workspace-a", weight=2, cost=3,
    )
    assert request["idempotency_key"] == "github-drain-admission/v1:" + checkpoint["digests"]["run"]
    assert request["input_digest"] == admission_input_digest(
        job, client_id="client-a", workspace_id="workspace-a", weight=2, cost=3,
    )
    variants = [
        build_admission_request(job, client_id="client-b", workspace_id="workspace-a", weight=2, cost=3),
        build_admission_request(job, client_id="client-a", workspace_id="workspace-b", weight=2, cost=3),
        build_admission_request(job, client_id="client-a", workspace_id="workspace-a", weight=4, cost=3),
        build_admission_request(job, client_id="client-a", workspace_id="workspace-a", weight=2, cost=4),
    ]
    assert all(value["idempotency_key"] == request["idempotency_key"] for value in variants)
    assert all(value["input_digest"] != request["input_digest"] for value in variants)
    for invalid in (
        {"client_id": "client", "workspace_id": "workspace", "weight": True, "cost": 1},
        {"client_id": "x" * 129, "workspace_id": "workspace", "weight": 1, "cost": 1},
        {"client_id": "client", "workspace_id": "workspace", "weight": 1, "cost": 1_000_001},
    ):
        with pytest.raises(DrainAdmissionProjectionError) as excinfo:
            build_admission_request(job, **invalid)
        assert excinfo.value.reason_code == "admission_metadata_invalid"


def test_closed_external_dependency_evidence_is_preserved_and_bound(tmp_path):
    source = ReadOnlyGitHub("acme/widgets", [
        {"number": 1, "title": "child", "body": "Requires #9"},
        {"number": 9, "title": "closed base", "state": "closed"},
    ])
    checkpoint = _run(
        tmp_path, source, mapping=CanonicalMap(), checkpoint="external-final.json",
    )
    job = project_checkpoint(checkpoint)
    assert job["items"]["1"]["external_dependencies_closed"] == [9]
    assert job["external_dependencies"]["9"]["state"] == "closed"
    tampered = json.loads(json.dumps(job))
    tampered["external_dependencies"] = {}
    with pytest.raises(DrainAdmissionProjectionError) as excinfo:
        validate_projected_job(tampered)
    assert excinfo.value.reason_code == "job_items_invalid"


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda value: value["outcome"].update(status="COMPLETE", exit_code=0), "checkpoint_not_final"),
        (lambda value: value.update(execution_authorized=True), "checkpoint_not_final"),
        (lambda value: value["digests"].update(run="0" * 64), "checkpoint_identity_invalid"),
        (lambda value: value["map"]["canonical"].update(status="unsupported"), "canonical_map_invalid"),
        (lambda value: value["source_observation"].update(digest="0" * 64), "checkpoint_source_invalid"),
        (lambda value: value["plan"]["waves"].reverse(), "checkpoint_plan_invalid"),
        (lambda value: value["source_observation"].update(path="/private/source"), "job_source_invalid"),
        (lambda value: value["items"]["1"].update(path="/private/worktree"), "job_items_invalid"),
        (
            lambda value: value["items"]["1"].update(
                dependencies=[99], external_dependencies_closed=[99],
            ),
            "job_items_invalid",
        ),
    ],
)
def test_rehashed_semantic_tampering_fails_closed(tmp_path, mutation, reason):
    _path, checkpoint, _source = _planned_checkpoint(tmp_path)
    value = json.loads(json.dumps(checkpoint))
    mutation(value)
    _rehash(value)
    with pytest.raises(DrainAdmissionProjectionError) as excinfo:
        project_checkpoint(value)
    assert excinfo.value.reason_code == reason


def test_raw_integrity_tamper_and_empty_final_plan_are_rejected(tmp_path):
    _path, checkpoint, _source = _planned_checkpoint(tmp_path)
    tampered = json.loads(json.dumps(checkpoint))
    tampered["items"]["1"]["title"] = "changed without hash"
    with pytest.raises(DrainAdmissionProjectionError) as integrity:
        project_checkpoint(tampered)
    assert integrity.value.reason_code == "checkpoint_integrity_failed"

    empty = json.loads(json.dumps(checkpoint))
    empty["items"] = {}
    empty["external_dependencies"] = {}
    empty["plan"] = {"schema": "simplicio.github-drain-plan/v1", "waves": [], "issue_count": 0}
    empty["source_observation"]["open_issues"] = []
    empty["source_observation"]["digest"] = _digest([])
    _rehash(empty)
    with pytest.raises(DrainAdmissionProjectionError) as excinfo:
        project_checkpoint(empty)
    assert excinfo.value.reason_code == "empty_plan"
