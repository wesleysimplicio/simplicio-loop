from __future__ import annotations

import json
import subprocess
import threading

import pytest

from simplicio_loop import cli, hub_drain_admission_cli
from simplicio_loop.github_drain_admission import build_admission_request, load_and_project_checkpoint
from simplicio_loop.github_drain_intake import _digest
from simplicio_loop.hub_daemon import (
    HubClient, HubDaemon, HubProtocolError, HubSocketClient, HubSocketServer,
    default_endpoint, default_transport,
)
from simplicio_loop.hub_governor import ResourceRequest
from simplicio_loop.hub_queue_retry import HubRetryQueue, QueueRetryError
from simplicio_loop.hub_scheduler import FairScheduler
from tests.test_github_drain_intake_integration import CanonicalMap, ReadOnlyGitHub, _run


def _checkpoint(tmp_path):
    source = ReadOnlyGitHub("acme/widgets", [
        {"number": 1, "title": "base"},
        {"number": 2, "title": "dependent", "body": "Depends on #1"},
    ])
    _run(tmp_path, source, mapping=CanonicalMap(), checkpoint="final.json")
    return tmp_path / "final.json", source


class _ObservedRLock:
    def __init__(self, lock, target_thread, attempted):
        self._lock = lock
        self._target_thread = target_thread
        self._attempted = attempted

    def __enter__(self):
        if threading.current_thread().name == self._target_thread:
            self._attempted.set()
        self._lock.acquire()
        return self

    def __exit__(self, *_args):
        self._lock.release()


def test_in_process_ipc_admit_lookup_and_execution_planes_stay_empty(tmp_path):
    checkpoint, source = _checkpoint(tmp_path)
    request = build_admission_request(
        load_and_project_checkpoint(checkpoint), client_id="client-a", workspace_id="workspace-a",
    )
    daemon = HubDaemon(str(tmp_path / "hub.lock"), queue_path=str(tmp_path / "hub.db"))
    daemon.start()
    try:
        client = HubClient(daemon, "client-a")
        response = client.request("admit", "hub_admit", **request)
        receipt = response["admission"]
        assert receipt["state"] == "admitted_held"
        assert receipt["recovery"] == "ADMITTED_NOT_DISPATCHED"
        assert str(tmp_path) not in json.dumps(receipt, sort_keys=True)
        assert client.request(
            "lookup-task", "hub_admission", task_id=receipt["task_id"],
        )["admission"] == receipt
        assert client.request(
            "lookup-key", "hub_admission", idempotency_key=receipt["idempotency_key"],
        )["admission"] == receipt
        assert daemon.service.claim("worker", ResourceRequest()) is None
        assert daemon.scheduler.status()["global_total"] == 0
        assert daemon.service.governor.status()["active_leases"] == 0
        assert daemon.service.rehydrate_scheduler() == 0
        assert source.effect_calls == []
        for hostile in (
            {**request, "weight": True},
            {**request, "client_id": "x" * 129},
        ):
            with pytest.raises(HubProtocolError):
                client.request("invalid", "hub_admit", **hostile)
        hostile_jobs = []
        for field in ("state", "checkpoint_path"):
            hostile_job = json.loads(json.dumps(request["job"]))
            hostile_job[field] = "queued" if field == "state" else "/private/checkpoint.json"
            hostile_jobs.append(hostile_job)
        item_path = json.loads(json.dumps(request["job"]))
        item_path["items"]["1"]["path"] = "/private/worktree"
        hostile_jobs.append(item_path)
        url_path = json.loads(json.dumps(request["job"]))
        url_path["items"]["1"]["url"] = "/private/worktree/file.txt"
        hostile_jobs.append(url_path)
        source_tamper = json.loads(json.dumps(request["job"]))
        source_tamper["source"]["open_issues"] = [2]
        source_tamper["source"]["digest"] = _digest([2])
        source_tamper["source_digest"] = _digest([2])
        hostile_jobs.append(source_tamper)
        plan_tamper = json.loads(json.dumps(request["job"]))
        plan_tamper["plan"]["waves"].reverse()
        plan_tamper["plan_digest"] = _digest(plan_tamper["plan"])
        hostile_jobs.append(plan_tamper)
        for index, hostile_job in enumerate(hostile_jobs):
            hostile = build_admission_request(
                hostile_job, client_id="client-a", workspace_id="workspace-a",
            )
            with pytest.raises(HubProtocolError):
                client.request("invalid-job-%d" % index, "hub_admit", **hostile)
        assert daemon.queue.count() == 1
    finally:
        daemon.stop()


def test_scheduler_first_submit_rolls_back_if_external_held_admission_wins(
    tmp_path, monkeypatch,
):
    checkpoint, _source = _checkpoint(tmp_path)
    request = build_admission_request(
        load_and_project_checkpoint(checkpoint), client_id="held", workspace_id="held-space",
    )
    queue_path = str(tmp_path / "hub.db")
    daemon = HubDaemon(
        str(tmp_path / "hub.lock"), queue_path=queue_path,
        scheduler=FairScheduler(max_global_queue=1),
    )
    daemon.start()
    external = HubRetryQueue(queue_path)
    scheduler_enqueued = threading.Event()
    release_submit = threading.Event()
    errors = []
    thread = None
    original_submit = daemon.queue.submit

    def paused_submit(*args, **kwargs):
        scheduler_enqueued.set()
        assert release_submit.wait(timeout=5)
        return original_submit(*args, **kwargs)

    def legacy_submit():
        try:
            HubClient(daemon, "legacy").request(
                "legacy-race", "submit",
                job_id=request["idempotency_key"], client_id="legacy",
            )
        except Exception as exc:  # noqa: BLE001 - exact race outcome asserted below
            errors.append(exc)

    monkeypatch.setattr(daemon.queue, "submit", paused_submit)
    try:
        thread = threading.Thread(target=legacy_submit)
        thread.start()
        assert scheduler_enqueued.wait(timeout=5)
        receipt = external.admit_held(
            **request,
            capacity_snapshot=daemon.service._capacity_snapshot("held", "held-space"),
        )
        release_submit.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], QueueRetryError)
        assert external.state(receipt["task_id"]) == "admitted_held"
        status = daemon.scheduler.status()
        assert status["global_total"] == status["queued"] == 0

        monkeypatch.setattr(daemon.queue, "submit", original_submit)
        response = HubClient(daemon, "other").request(
            "unrelated", "submit", job_id="unrelated", client_id="other",
        )
        assert response["job"]["state"] == "queued"
        assert daemon.scheduler.status()["global_total"] == 1
    finally:
        release_submit.set()
        if thread is not None:
            thread.join(timeout=5)
        external.close()
        daemon.stop()


def test_held_admission_wins_daemon_race_without_scheduler_ghost(tmp_path, monkeypatch):
    checkpoint, _source = _checkpoint(tmp_path)
    request = build_admission_request(
        load_and_project_checkpoint(checkpoint), client_id="held", workspace_id="held-space",
    )
    daemon = HubDaemon(
        str(tmp_path / "hub.lock"), queue_path=str(tmp_path / "hub.db"),
        scheduler=FairScheduler(max_global_queue=1),
    )
    daemon.start()
    held_inserted = threading.Event()
    release_admission = threading.Event()
    legacy_waiting = threading.Event()
    receipts = []
    admission_errors = []
    legacy_errors = []

    def pause_inside_admission(_task_id):
        held_inserted.set()
        assert release_admission.wait(timeout=5)

    def admit():
        try:
            receipts.append(HubClient(daemon, "held").request(
                "admit-first", "hub_admit", **request,
            )["admission"])
        except Exception as exc:  # noqa: BLE001 - thread outcome asserted below
            admission_errors.append(exc)

    def legacy_submit():
        try:
            HubClient(daemon, "legacy").request(
                "legacy-second", "submit",
                job_id=request["idempotency_key"], client_id="legacy",
            )
        except Exception as exc:  # noqa: BLE001 - thread outcome asserted below
            legacy_errors.append(exc)

    daemon._queue_lock = _ObservedRLock(
        daemon._queue_lock, "legacy-second", legacy_waiting,
    )
    monkeypatch.setattr(daemon.queue, "_after_held_job_insert", pause_inside_admission)
    admit_thread = threading.Thread(target=admit, name="admit-first")
    legacy_thread = threading.Thread(target=legacy_submit, name="legacy-second")
    try:
        admit_thread.start()
        assert held_inserted.wait(timeout=5)
        legacy_thread.start()
        assert legacy_waiting.wait(timeout=5)
        release_admission.set()
        admit_thread.join(timeout=5)
        legacy_thread.join(timeout=5)
        assert not admit_thread.is_alive() and not legacy_thread.is_alive()
        assert admission_errors == [] and len(receipts) == 1
        assert len(legacy_errors) == 1 and isinstance(legacy_errors[0], HubProtocolError)
        assert receipts[0]["state"] == "admitted_held"
        assert daemon.queue.count() == 1
        status = daemon.scheduler.status()
        assert status["global_total"] == status["queued"] == 0

        response = HubClient(daemon, "other").request(
            "unrelated", "submit", job_id="unrelated", client_id="other",
        )
        assert response["job"]["state"] == "queued"
        assert daemon.scheduler.status()["global_total"] == 1
    finally:
        release_admission.set()
        if admit_thread.ident is not None:
            admit_thread.join(timeout=5)
        if legacy_thread.ident is not None:
            legacy_thread.join(timeout=5)
        daemon.stop()


def test_legacy_submit_wins_daemon_race_as_one_real_scheduled_row(tmp_path, monkeypatch):
    checkpoint, _source = _checkpoint(tmp_path)
    request = build_admission_request(
        load_and_project_checkpoint(checkpoint), client_id="held", workspace_id="held-space",
    )
    daemon = HubDaemon(
        str(tmp_path / "hub.lock"), queue_path=str(tmp_path / "hub.db"),
        scheduler=FairScheduler(max_global_queue=2),
    )
    daemon.start()
    legacy_checked = threading.Event()
    release_submit = threading.Event()
    admission_waiting = threading.Event()
    submit_results = []
    submit_errors = []
    admission_errors = []
    original_find = daemon.queue.find_task_id
    paused = [False]

    def pause_after_missing(key):
        result = original_find(key)
        if key == request["idempotency_key"] and not paused[0]:
            paused[0] = True
            assert result is None
            legacy_checked.set()
            assert release_submit.wait(timeout=5)
        return result

    def legacy_submit():
        try:
            submit_results.append(HubClient(daemon, "legacy").request(
                "legacy-first", "submit",
                job_id=request["idempotency_key"], client_id="legacy",
            ))
        except Exception as exc:  # noqa: BLE001 - thread outcome asserted below
            submit_errors.append(exc)

    def admit():
        try:
            HubClient(daemon, "held").request("admit-second", "hub_admit", **request)
        except Exception as exc:  # noqa: BLE001 - thread outcome asserted below
            admission_errors.append(exc)

    daemon._queue_lock = _ObservedRLock(
        daemon._queue_lock, "admit-second", admission_waiting,
    )
    monkeypatch.setattr(daemon.queue, "find_task_id", pause_after_missing)
    legacy_thread = threading.Thread(target=legacy_submit, name="legacy-first")
    admit_thread = threading.Thread(target=admit, name="admit-second")
    try:
        legacy_thread.start()
        assert legacy_checked.wait(timeout=5)
        admit_thread.start()
        assert admission_waiting.wait(timeout=5)
        release_submit.set()
        legacy_thread.join(timeout=5)
        admit_thread.join(timeout=5)
        assert not legacy_thread.is_alive() and not admit_thread.is_alive()
        assert submit_errors == [] and len(submit_results) == 1
        assert len(admission_errors) == 1 and isinstance(admission_errors[0], HubProtocolError)
        assert daemon.queue.count() == 1
        assert daemon.queue._db.execute("SELECT COUNT(*) FROM hub_admissions").fetchone()[0] == 0
        status = daemon.scheduler.status()
        assert status["global_total"] == status["queued"] == 1

        monkeypatch.setattr(daemon.queue, "find_task_id", original_find)
        HubClient(daemon, "other").request(
            "unrelated", "submit", job_id="unrelated", client_id="other",
        )
        assert daemon.scheduler.status()["global_total"] == 2
    finally:
        release_submit.set()
        if legacy_thread.ident is not None:
            legacy_thread.join(timeout=5)
        if admit_thread.ident is not None:
            admit_thread.join(timeout=5)
        daemon.stop()


def test_real_unix_socket_admit_lookup_and_restart_replay(tmp_path):
    if default_transport() != "unix":
        return
    checkpoint, _source = _checkpoint(tmp_path)
    job = load_and_project_checkpoint(checkpoint)
    request = build_admission_request(job, client_id="wire-client", workspace_id="wire-workspace")
    lock_path = str(tmp_path / "hub.lock")
    queue_path = str(tmp_path / "hub.db")
    endpoint = default_endpoint(str(tmp_path))

    daemon = HubDaemon(lock_path, queue_path=queue_path)
    daemon.start()
    server = HubSocketServer(daemon, endpoint, "unix")
    try:
        server.start()
    except PermissionError:
        daemon.stop()
        pytest.skip("AF_UNIX is denied by this sandbox")
    try:
        client = HubSocketClient(endpoint, transport="unix")
        first = client.request("wire-admit", "hub_admit", **request)["admission"]
        replay = client.request("wire-replay", "hub_admit", **request)["admission"]
        assert first == replay
    finally:
        server.shutdown()
        daemon.stop()

    restarted = HubDaemon(lock_path, queue_path=queue_path)
    restarted.start()
    server = HubSocketServer(restarted, endpoint, "unix")
    try:
        server.start()
    except PermissionError:
        restarted.stop()
        pytest.skip("AF_UNIX is denied by this sandbox")
    try:
        client = HubSocketClient(endpoint, transport="unix")
        loaded = client.request(
            "wire-lookup", "hub_admission", idempotency_key=first["idempotency_key"],
        )["admission"]
        assert loaded == first
        assert restarted.scheduler.status()["global_total"] == 0
        assert restarted.service.rehydrate_scheduler() == 0
    finally:
        server.shutdown()
        restarted.stop()


def test_cli_uses_running_daemon_reports_non_execution_and_never_spawns(tmp_path, capsys, monkeypatch):
    if default_transport() != "unix":
        return
    checkpoint, source = _checkpoint(tmp_path)
    endpoint = default_endpoint(str(tmp_path))
    daemon = HubDaemon(str(tmp_path / "hub.lock"), queue_path=str(tmp_path / "hub.db"))
    daemon.start()
    server = HubSocketServer(daemon, endpoint, "unix")
    try:
        server.start()
    except PermissionError:
        daemon.stop()
        pytest.skip("AF_UNIX is denied by this sandbox")
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("subprocess.run")))
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("subprocess.Popen")))
    try:
        code = cli.main([
            "hub-drain-admit", "--checkpoint", str(checkpoint), "--endpoint", endpoint,
            "--transport", "unix", "--client-id", "cli-client", "--workspace-id", "cli-workspace",
        ])
        payload = json.loads(capsys.readouterr().out)
        assert code != 0 and code == 3
        assert payload["exit_code"] == 3
        assert payload["status"] == "ADMITTED_NOT_DISPATCHED"
        assert payload["dispatchable"] is False
        assert payload["activation_required"] is True
        assert payload["execution_authorized"] is False
        assert payload["receipt"]["recovery"] == "ADMITTED_NOT_DISPATCHED"
        assert daemon.queue.count() == 1
        assert daemon.scheduler.status()["global_total"] == 0
        assert source.effect_calls == []
    finally:
        server.shutdown()
        daemon.stop()


def test_cli_success_contract_through_real_daemon_dispatch_without_process_effects(
    tmp_path, capsys, monkeypatch,
):
    checkpoint, source = _checkpoint(tmp_path)
    daemon = HubDaemon(str(tmp_path / "hub.lock"), queue_path=str(tmp_path / "hub.db"))
    daemon.start()
    monkeypatch.setattr(
        hub_drain_admission_cli, "HubSocketClient",
        lambda *_a, **_k: HubClient(daemon, "fallback-client"),
    )
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("subprocess.run")))
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("subprocess.Popen")))
    try:
        code = cli.main([
            "hub-drain-admit", "--checkpoint", str(checkpoint),
            "--client-id", "cli-client", "--workspace-id", "cli-workspace",
        ])
        payload = json.loads(capsys.readouterr().out)
        assert code == 3
        assert payload["exit_code"] == 3
        assert payload["status"] == "ADMITTED_NOT_DISPATCHED"
        assert payload["dispatchable"] is False
        assert payload["execution_authorized"] is False
        assert payload["receipt"]["idempotency_key"].startswith("github-drain-admission/v1:")
        assert daemon.queue.count() == 1
        assert daemon.scheduler.status()["global_total"] == 0
        assert source.effect_calls == []
    finally:
        daemon.stop()


def test_cli_does_not_autostart_missing_daemon_or_create_queue(tmp_path, capsys):
    checkpoint, _source = _checkpoint(tmp_path)
    endpoint = str(tmp_path / "missing.sock")
    code = cli.main([
        "hub-drain-admit", "--checkpoint", str(checkpoint), "--endpoint", endpoint,
        "--transport", "unix",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code != 0
    assert payload["status"] == "FAILED"
    assert payload["reason_code"] == "hub_unavailable"
    assert str(tmp_path) not in json.dumps(payload, sort_keys=True)
    assert not (tmp_path / "hub.lock").exists()
    assert not (tmp_path / "hub.db").exists()
    assert not (tmp_path / "missing.sock").exists()


def test_cli_redacts_hostile_checkpoint_path_from_projection_error(tmp_path, capsys):
    hostile = tmp_path / "private-marker=opaque-value.json"
    code = cli.main(["hub-drain-admit", "--checkpoint", str(hostile)])
    raw = capsys.readouterr().out
    payload = json.loads(raw)
    assert code != 0
    assert payload["status"] == "BLOCKED"
    assert payload["reason_code"] == "checkpoint_invalid"
    assert str(hostile) not in raw
    assert "opaque-value" not in raw


def test_root_help_describes_read_only_plan_and_held_admission(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--help"])
    assert exc_info.value.code == 0
    output = " ".join(capsys.readouterr().out.split())
    assert "read-only PT-BR/EN GitHub drain intake; never executes the plan" in output
    assert "admit a final #627 checkpoint as held; never dispatches or executes it" in output
