from __future__ import annotations

import json
import threading

import pytest

from simplicio_loop.github_drain_admission import build_admission_request
from simplicio_loop.github_drain_intake import _digest
from simplicio_loop.hub_governor import ResourceGovernor, ResourceLimits, ResourceRequest
from simplicio_loop.hub_queue_retry import HubRetryQueue, QueueRetryError
from simplicio_loop.hub_scheduler import FairScheduler, ScheduledJob
from simplicio_loop.hub_service import HubService


def _job(run_digest: str = "a" * 64, *, title: str = "one"):
    source = {
        "digest": _digest([1]), "open_issues": [1], "observed_at": "2026-07-19T00:00:00Z",
    }
    plan = {
        "schema": "simplicio.github-drain-plan/v1",
        "waves": [{"index": 1, "issues": [1], "risk_order": ["low"]}],
        "issue_count": 1,
    }
    return {
        "schema": "simplicio.github-drain-job/v1",
        "kind": "github_drain_root",
        "repository": "acme/widgets",
        "run_id": "1" * 32,
        "run_digest": run_digest,
        "request_digest": "2" * 64,
        "checkpoint_digest": "4" * 64,
        "source_digest": source["digest"],
        "plan_digest": _digest(plan),
        "workspace_digest": "5" * 64,
        "issue_count": 1,
        "source": source,
        "canonical_map": {
            "schema": "simplicio.github-drain-map/v1", "status": "ready",
            "mode": "canonical", "repository": "acme/widgets", "cache_key": "canonical-1",
        },
        "items": {"1": {
            "number": 1, "title": title, "url": "https://github.com/acme/widgets/issues/1",
            "labels": [], "source_revision": "r1", "observed_at": "2026-07-19T00:00:00Z",
            "dependencies": [], "external_dependencies_closed": [], "risk": "low",
            "state": "planned",
        }},
        "external_dependencies": {},
        "plan": plan,
        "dispatchable": False,
        "activation_required": True,
        "execution_authorized": False,
    }


def _request(job=None, **overrides):
    values = {"client_id": "client-a", "workspace_id": "workspace-a", "weight": 2, "cost": 3}
    values.update(overrides)
    return build_admission_request(job or _job(), **values)


def _capacity():
    resources = {
        "cpu": 0, "memory_bytes": 0, "disk_bytes": 0, "gpu": 0,
        "processes": 0, "connections": 0, "tokens": 0,
    }
    return {
        "schema": "simplicio.hub-capacity-observation/v1",
        "reservation": False,
        "fresh_snapshot_required_at_activation": True,
        "scheduler": {
            "limits": {
                "max_inflight_per_client": 4, "max_queue_per_client": None,
                "max_queue_per_workspace": None, "max_global_queue": None,
                "quantum": 1, "aging_ticks": 20, "aging_boost": 4,
            },
            "global": {"queued": 0, "global_total": 0, "clients": 0},
            "target_client": {"total": 0, "inflight": 0},
            "target_workspace": {"total": 0},
        },
        "governor": {
            "limits": dict(resources), "used": dict(resources),
            "target_client_used": dict(resources), "draining": False,
            "circuit": {
                "state": "closed", "failures": 0, "threshold": 3,
                "cooldown_seconds": 30.0,
            },
        },
    }


def _admit(queue, request):
    return queue.admit_held(**request, capacity_snapshot=_capacity())


def test_atomic_admit_inserts_exactly_one_held_job_and_receipt(tmp_path):
    queue = HubRetryQueue(str(tmp_path / "hub.db"))
    receipt = _admit(queue, _request())
    rows = queue._db.execute("SELECT task_id,state,payload FROM hub_jobs").fetchall()
    admissions = queue._db.execute("SELECT task_id,receipt FROM hub_admissions").fetchall()
    assert len(rows) == len(admissions) == 1
    assert rows[0]["state"] == "admitted_held"
    assert json.loads(rows[0]["payload"])["dispatchable"] is False
    assert receipt["task_id"] == rows[0]["task_id"] == admissions[0]["task_id"]
    assert receipt["recovery"] == "ADMITTED_NOT_DISPATCHED"
    assert receipt["execution_authorized"] is False
    assert queue.admission(task_id=receipt["task_id"]) == receipt
    assert queue.admission(idempotency_key=receipt["idempotency_key"]) == receipt
    queue.close()


def test_replay_is_byte_equivalent_and_divergent_inputs_conflict(tmp_path):
    queue = HubRetryQueue(str(tmp_path / "hub.db"))
    request = _request()
    first = _admit(queue, request)
    replay = _admit(queue, request)
    assert HubRetryQueue._canonical_json(replay) == HubRetryQueue._canonical_json(first)
    divergent = [
        _request(_job(title="changed")),
        _request(client_id="client-b"),
        _request(workspace_id="workspace-b"),
        _request(weight=9),
        _request(cost=9),
    ]
    for candidate in divergent:
        with pytest.raises(QueueRetryError, match="different held input"):
            _admit(queue, candidate)
    assert queue.count() == 1
    queue.close()


def test_queue_rejects_tampered_explicit_job_identity_fields(tmp_path):
    queue = HubRetryQueue(str(tmp_path / "hub.db"))
    original = _job()
    mutations = []
    for field in ("checkpoint_digest", "source_digest", "plan_digest", "workspace_digest"):
        candidate = json.loads(json.dumps(original))
        candidate[field] = "not-a-canonical-digest"
        mutations.append(candidate)
    source_mismatch = json.loads(json.dumps(original))
    source_mismatch["source_digest"] = "6" * 64
    mutations.append(source_mismatch)
    plan_mismatch = json.loads(json.dumps(original))
    plan_mismatch["plan_digest"] = "7" * 64
    mutations.append(plan_mismatch)
    count_mismatch = json.loads(json.dumps(original))
    count_mismatch["issue_count"] = 2
    mutations.append(count_mismatch)
    extra_state = json.loads(json.dumps(original))
    extra_state["state"] = "queued"
    mutations.append(extra_state)
    extra_path = json.loads(json.dumps(original))
    extra_path["checkpoint_path"] = "/private/checkpoint.json"
    mutations.append(extra_path)
    item_path = json.loads(json.dumps(original))
    item_path["items"]["1"]["path"] = "/private/worktree"
    mutations.append(item_path)
    url_path = json.loads(json.dumps(original))
    url_path["items"]["1"]["url"] = "/private/worktree/file.txt"
    mutations.append(url_path)
    revision_path = json.loads(json.dumps(original))
    revision_path["items"]["1"]["source_revision"] = "/private/revision"
    mutations.append(revision_path)
    timestamp_payload = json.loads(json.dumps(original))
    timestamp_payload["items"]["1"]["observed_at"] = "/private/observed-at"
    mutations.append(timestamp_payload)
    map_path = json.loads(json.dumps(original))
    map_path["canonical_map"]["root"] = "/private/worktree"
    mutations.append(map_path)
    cache_path = json.loads(json.dumps(original))
    cache_path["canonical_map"]["cache_key"] = "/private/cache-key"
    mutations.append(cache_path)
    source_semantic = json.loads(json.dumps(original))
    source_semantic["source"]["open_issues"] = [2]
    source_semantic["source"]["digest"] = _digest([2])
    source_semantic["source_digest"] = _digest([2])
    mutations.append(source_semantic)
    plan_semantic = json.loads(json.dumps(original))
    plan_semantic["plan"]["waves"][0]["risk_order"] = ["high"]
    plan_semantic["plan_digest"] = _digest(plan_semantic["plan"])
    mutations.append(plan_semantic)
    missing_external = json.loads(json.dumps(original))
    missing_external["items"]["1"]["dependencies"] = [9]
    missing_external["items"]["1"]["external_dependencies_closed"] = [9]
    mutations.append(missing_external)

    for candidate in mutations:
        with pytest.raises(QueueRetryError, match="job projection"):
            _admit(queue, _request(candidate))
    assert queue.count() == 0
    queue.close()


def test_legacy_queued_key_collision_and_reverse_submit_are_blocked(tmp_path):
    queue = HubRetryQueue(str(tmp_path / "hub.db"))
    request = _request()
    queue.submit({"legacy": True}, idempotency_key=request["idempotency_key"])
    with pytest.raises(QueueRetryError, match="non-admission"):
        _admit(queue, request)

    second = _request(_job("b" * 64))
    receipt = _admit(queue, second)
    with pytest.raises(QueueRetryError, match="held admission"):
        queue.submit({"activate": True}, idempotency_key=second["idempotency_key"])
    assert queue.state(receipt["task_id"]) == "admitted_held"
    queue.close()


class _PauseSubmitAfterMissingSelect:
    def __init__(self, db, selected, release):
        self._db = db
        self._selected = selected
        self._release = release
        self._paused = False

    def execute(self, sql, params=()):
        result = self._db.execute(sql, params)
        if (
            not self._paused
            and sql.lstrip().startswith("SELECT task_id,state FROM hub_jobs WHERE idempotency_key")
        ):
            self._paused = True
            self._selected.set()
            assert self._release.wait(timeout=5)
        return result

    def __getattr__(self, name):
        return getattr(self._db, name)


def test_submit_losing_race_to_held_admission_rejects_winner(tmp_path):
    path = str(tmp_path / "race.db")
    submit_queue = HubRetryQueue(path)
    admit_queue = HubRetryQueue(path)
    selected = threading.Event()
    release = threading.Event()
    submit_queue._db = _PauseSubmitAfterMissingSelect(submit_queue._db, selected, release)
    request = _request()
    errors = []

    def submit():
        try:
            submit_queue.submit({"legacy": True}, idempotency_key=request["idempotency_key"])
        except Exception as exc:  # noqa: BLE001 - exact race outcome asserted below
            errors.append(exc)

    thread = threading.Thread(target=submit)
    thread.start()
    assert selected.wait(timeout=5)
    receipt = _admit(admit_queue, request)
    release.set()
    thread.join(timeout=5)
    assert len(errors) == 1 and isinstance(errors[0], QueueRetryError)
    assert "held admission" in str(errors[0])
    assert admit_queue.state(receipt["task_id"]) == "admitted_held"
    assert admit_queue.count() == 1
    submit_queue.close()
    admit_queue.close()


def test_fault_after_job_insert_rolls_back_both_rows(tmp_path, monkeypatch):
    queue = HubRetryQueue(str(tmp_path / "hub.db"))
    monkeypatch.setattr(
        queue, "_after_held_job_insert",
        lambda _task_id: (_ for _ in ()).throw(RuntimeError("injected crash")),
    )
    with pytest.raises(RuntimeError, match="injected crash"):
        _admit(queue, _request())
    assert queue._db.execute("SELECT COUNT(*) FROM hub_jobs").fetchone()[0] == 0
    assert queue._db.execute("SELECT COUNT(*) FROM hub_admissions").fetchone()[0] == 0
    queue.close()


def test_lookup_rejects_receipt_extensions_and_noncanonical_timestamp_even_with_new_hash(tmp_path):
    queue = HubRetryQueue(str(tmp_path / "hub.db"))
    receipt = _admit(queue, _request())
    original = dict(receipt)
    variants = []
    extended = dict(original, unexpected="value")
    variants.append(extended)
    bad_time = dict(original, created_at="2026-02-30T00:00:00Z")
    variants.append(bad_time)
    forged_time = dict(original, created_at="2000-01-01T00:00:00Z")
    variants.append(forged_time)

    for variant in variants:
        variant["receipt_hash"] = queue._value_digest({
            key: value for key, value in variant.items() if key != "receipt_hash"
        })
        queue._db.execute(
            "UPDATE hub_admissions SET receipt=? WHERE task_id=?",
            (queue._canonical_json(variant), receipt["task_id"]),
        )
        with pytest.raises(QueueRetryError, match="receipt failed validation"):
            queue.admission(task_id=receipt["task_id"])
    queue.close()


def test_direct_queue_rejects_unsanitized_or_reserving_capacity(tmp_path):
    queue = HubRetryQueue(str(tmp_path / "hub.db"))
    for bad in (
        {**_capacity(), "reservation": True},
        {**_capacity(), "third_party_ids": ["private-client"]},
        {**_capacity(), "scheduler": {**_capacity()["scheduler"], "client_total": {"private": 1}}},
    ):
        with pytest.raises(QueueRetryError, match="capacity snapshot"):
            queue.admit_held(**_request(), capacity_snapshot=bad)
    assert queue.count() == 0
    queue.close()


def test_direct_queue_rejects_bool_unbounded_or_hostile_metadata(tmp_path):
    queue = HubRetryQueue(str(tmp_path / "hub.db"))
    valid = _request()
    invalid = [
        {**valid, "weight": True},
        {**valid, "cost": 1_000_001},
        {**valid, "client_id": "x" * 129},
        {**valid, "workspace_id": "workspace\nprivate"},
    ]
    for request in invalid:
        with pytest.raises(QueueRetryError, match="metadata"):
            queue.admit_held(**request, capacity_snapshot=_capacity())
    assert queue.count() == 0
    queue.close()


def test_two_connections_concurrent_same_and_divergent_inputs(tmp_path):
    path = str(tmp_path / "hub.db")
    first = HubRetryQueue(path)
    second = HubRetryQueue(path)
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def run(queue, request):
        try:
            barrier.wait(timeout=5)
            results.append(_admit(queue, request))
        except Exception as exc:  # noqa: BLE001 - race result is asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=(first, _request())),
        threading.Thread(target=run, args=(second, _request())),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert errors == []
    assert len(results) == 2 and results[0] == results[1]
    assert first.count() == 1
    first.close()
    second.close()

    divergent_path = str(tmp_path / "divergent.db")
    first = HubRetryQueue(divergent_path)
    second = HubRetryQueue(divergent_path)
    barrier = threading.Barrier(2)
    results = []
    errors = []
    threads = [
        threading.Thread(target=run, args=(first, _request())),
        threading.Thread(target=run, args=(second, _request(_job(title="different")))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(results) == 1
    assert len(errors) == 1 and isinstance(errors[0], QueueRetryError)
    assert first.count() == 1
    first.close()
    second.close()


def test_restart_replay_unique_tasks_and_all_claim_paths_ignore_held(tmp_path):
    path = str(tmp_path / "hub.db")
    queue = HubRetryQueue(path)
    first = _admit(queue, _request())
    second = _admit(queue, _request(_job("b" * 64)))
    assert first["task_id"] != second["task_id"]
    assert queue.claim("worker") is None
    assert queue.claim_specific(first["task_id"], "worker") is None
    assert queue.list_queued_scheduling_metadata() == []
    scheduler = FairScheduler()
    queue.sync_fair_scheduler(scheduler)
    assert scheduler.status()["global_total"] == 0
    with pytest.raises(QueueRetryError, match="immutable"):
        queue.update_payload(first["task_id"], {"dispatchable": True})
    queue.close()

    restarted = HubRetryQueue(path)
    assert restarted.admission(task_id=first["task_id"]) == first
    assert restarted.claim("worker") is None
    assert restarted.list_queued_scheduling_metadata() == []
    restarted.close()


def test_service_admission_observes_capacity_without_mutating_execution_planes(tmp_path, monkeypatch):
    queue = HubRetryQueue(str(tmp_path / "hub.db"))
    scheduler = FairScheduler(max_global_queue=8, max_queue_per_client=4, max_queue_per_workspace=6)
    scheduler.enqueue(ScheduledJob("other-task", "other-client", workspace_id="other-workspace"))
    governor = ResourceGovernor(ResourceLimits(cpu=8, memory_bytes=1024))
    governor.admit("other-client", "other-task", ResourceRequest(cpu=2), queue="other-workspace")
    governor._circuit.reason = "/hostile/private/path?note=opaque-value"
    service = HubService(queue, scheduler, governor)
    scheduler_before = scheduler.status()
    governor_before = governor.status()

    monkeypatch.setattr(service, "submit", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("submit")))
    monkeypatch.setattr(scheduler, "enqueue", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("enqueue")))
    monkeypatch.setattr(governor, "admit", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("admit")))
    request = _request()
    receipt = service.admit_held(**request)

    assert scheduler.status() == scheduler_before
    assert governor.status() == governor_before
    assert service.rehydrate_scheduler() == 0
    snapshot = receipt["capacity_snapshot"]
    assert snapshot["reservation"] is False
    assert snapshot["fresh_snapshot_required_at_activation"] is True
    assert snapshot["scheduler"]["limits"]["max_global_queue"] == 8
    assert snapshot["scheduler"]["global"]["global_total"] == 1
    assert snapshot["scheduler"]["target_client"] == {"total": 0, "inflight": 0}
    assert snapshot["scheduler"]["target_workspace"] == {"total": 0}
    assert snapshot["governor"]["used"]["cpu"] == 2
    assert snapshot["governor"]["target_client_used"]["cpu"] == 0
    assert "other-client" not in json.dumps(snapshot, sort_keys=True)
    assert "other-workspace" not in json.dumps(snapshot, sort_keys=True)
    assert "hostile" not in json.dumps(snapshot, sort_keys=True)
    assert "opaque-value" not in json.dumps(snapshot, sort_keys=True)
    assert service.admission(task_id=receipt["task_id"]) == receipt
    queue.close()
