from __future__ import annotations

import contextlib
import json
import sqlite3
import subprocess

import pytest

from simplicio_loop.local_task_queue import LEGACY_SCHEMA, LocalTaskQueue, SCHEMA
from simplicio_loop.local_task_queue_cli import cli_main
from simplicio_loop.remote_queue import QueueConflict, QueueUnavailable


def test_fencing_restart_dependencies_and_verified_completion(tmp_path):
    queue = LocalTaskQueue(tmp_path)
    queue.submit("a")
    queue.submit("b", depends_on=["a"])
    with pytest.raises(QueueConflict, match="dependencies"):
        queue.claim_local("b", "w", idempotency_key="b1")
    lease = queue.claim_local("a", "w", idempotency_key="a1", ttl=30)
    queue.persist_intent(lease, {"effect": "write"})
    queue.record_outcome(lease, "verified_success", receipt={"proof": "ok"})
    restarted = LocalTaskQueue(tmp_path)
    assert restarted.inspect_local("a")["outcome"]["outcome"] == "verified_success"
    second = restarted.claim_local("b", "w2", idempotency_key="b2")
    assert second.fencing_token == 1


def test_stop_prevents_claim_and_requests_active_cancellation(tmp_path):
    queue = LocalTaskQueue(tmp_path)
    queue.submit("a")
    queue.claim_local("a", "w", idempotency_key="a1")
    queue.submit("b")
    queue.stop()
    with pytest.raises(QueueConflict, match="stopped"):
        queue.submit("after-stop")
    with sqlite3.connect(queue.path) as db:
        assert db.execute("SELECT 1 FROM tasks WHERE task_id='after-stop'").fetchone() is None
    with pytest.raises(QueueConflict, match="stopped"):
        queue.claim_local("b", "w", idempotency_key="b1")
    assert queue.task("a")["lease"]["cancel_requested"] == 1
    queue.resume()
    assert queue.claim_local("b", "w", idempotency_key="b1").task_id == "b"


def test_unknown_outcome_requires_reconciliation_and_retry_provenance(tmp_path):
    queue = LocalTaskQueue(tmp_path)
    queue.submit("a")
    lease = queue.claim_local("a", "w", idempotency_key="a1")
    queue.record_outcome(lease, "unknown_outcome")
    with pytest.raises(QueueConflict, match="receipt"):
        queue.reconcile_unknown("a", verified=True)
    with pytest.raises(QueueConflict, match="provenance"):
        queue.reconcile_unknown("a", verified=False)
    queue.reconcile_unknown("a", verified=False, provenance={"idempotent": True})
    retry = queue.claim_local("a", "w2", idempotency_key="a2")
    with pytest.raises(QueueConflict, match="provenance"):
        queue.record_outcome(retry, "retryable_failure")


def test_receipts_history_doctor_and_migration(tmp_path):
    queue = LocalTaskQueue(tmp_path)
    queue.submit("a")
    lease = queue.claim_local("a", "w", idempotency_key="a1")
    receipt = queue.record_outcome(
        lease, "retryable_failure", provenance={"idempotent": True}
    )
    assert receipt["digest"].startswith("sha256:")
    assert len(queue.inspect_local("a")["transitions"]) == 3
    assert queue.doctor_local()["healthy"] is True
    with sqlite3.connect(queue.path) as db:
        original_provenance = db.execute("SELECT provenance FROM local_outcomes WHERE task_id='a'").fetchone()[0]
        db.execute("UPDATE local_outcomes SET provenance='{}' WHERE task_id='a'")
    assert queue.doctor_local()["healthy"] is False
    with sqlite3.connect(queue.path) as db:
        db.execute("UPDATE local_outcomes SET provenance=? WHERE task_id='a'", (original_provenance,))
    assert queue.migrate(dry_run=True)["dry_run"] is True
    migrated = queue.migrate(dry_run=False)
    assert migrated["dry_run"] is False
    with sqlite3.connect(migrated["backup"]) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert queue.status_local()["journal_mode"] in {"wal", "delete"}


def test_cancel_reclaim_drain_top_and_retention(tmp_path):
    queue = LocalTaskQueue(tmp_path)
    queue.submit("queued")
    assert [item["task_id"] for item in queue.top()] == ["queued"]
    assert queue.cancel_local("queued")["status"] == "cancelled"
    queue.submit("active")
    queue.claim_local("active", "w", idempotency_key="active", ttl=30)
    assert queue.cancel_local("active")["status"] == "cancelling"
    assert queue.drain(timeout=0)["status"] == "cancelling"
    queue.resume()
    queue.submit("stale")
    queue.claim_local("stale", "w", idempotency_key="stale", ttl=0.01)
    assert queue.reclaim_stale(now=10**30) == ["active", "stale"]
    queue.submit("done")
    lease = queue.claim_local("done", "w", idempotency_key="done")
    queue.record_outcome(lease, "verified_success", receipt={"proof": True})
    with pytest.raises(QueueConflict, match="terminal"):
        queue.cancel_local("done")
    assert queue.task("done")["lease"]["status"] == "completed"
    with pytest.raises(QueueConflict, match="stale"):
        queue.release(lease)
    assert queue.gc_terminal()["eligible"] == ["done"]
    assert queue.gc_terminal(apply=True)["removed"] == ["done"]


def test_json_cli_surface(tmp_path, capsys):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    queue = LocalTaskQueue(tmp_path)
    queue.submit("a")
    repo = str(tmp_path)
    for args in (
        ["--repo", repo, "status"],
        ["--repo", repo, "top", "--limit", "1"],
        ["--repo", repo, "inspect", "a"],
        ["--repo", repo, "cancel", "a"],
        ["--repo", repo, "drain", "--timeout", "0"],
        ["--repo", repo, "resume"],
        ["--repo", repo, "doctor"],
        ["--repo", repo, "reclaim"],
        ["--repo", repo, "gc"],
    ):
        assert cli_main(args) == 0
        assert capsys.readouterr().out.strip().startswith(("{", "["))


def test_cli_terminal_conflict_is_stable_json(tmp_path, capsys):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    queue = LocalTaskQueue(tmp_path)
    queue.submit("done")
    lease = queue.claim_local("done", "w", idempotency_key="done")
    queue.record_outcome(lease, "verified_success", receipt={"proof": True})
    assert cli_main(["--repo", str(tmp_path), "cancel", "done"]) == 3
    value = json.loads(capsys.readouterr().out)
    assert value == {"schema": "simplicio.loop.local-task-queue-error/v1",
                     "status": "error", "code": "conflict",
                     "reason": "terminal outcome is immutable"}


def test_stale_fence_cannot_write_intent_or_terminal_outcome(tmp_path):
    queue = LocalTaskQueue(tmp_path)
    queue.submit("a")
    stale = queue.claim_local("a", "old", idempotency_key="old", ttl=0.01)
    queue.reclaim_stale(now=10**30)
    current = queue.claim_local("a", "new", idempotency_key="new")
    with pytest.raises(QueueConflict, match="stale"):
        queue.persist_intent(stale, {"effect": "write"})
    with pytest.raises(QueueConflict, match="stale"):
        queue.record_outcome(stale, "verified_success", receipt={"proof": "stale"})
    queue.record_outcome(current, "verified_success", receipt={"proof": "current"})
    assert queue.task("a")["lease"]["status"] == "completed"


def test_submit_is_atomic_and_doctor_verifies_transition_digests(tmp_path, monkeypatch):
    queue = LocalTaskQueue(tmp_path)
    queue.submit("a")
    assert queue.doctor_local()["healthy"] is True
    with sqlite3.connect(queue.path) as db:
        db.execute("UPDATE local_transitions SET digest='tampered' WHERE task_id='a'")
    result = queue.doctor_local()
    assert result["healthy"] is False and result["corrupt_transitions"]
    queue.submit("b")
    assert queue.inspect_local("b")["outcome"]["outcome"] == "never_started"


def test_migration_failure_restores_original_database(tmp_path, monkeypatch):
    queue = LocalTaskQueue(tmp_path)
    queue.submit("survives")

    def fail_init(**_kwargs):
        raise RuntimeError("migration failed")

    monkeypatch.setattr(queue, "_init_local", fail_init)
    with pytest.raises(RuntimeError, match="migration failed"):
        queue.migrate(dry_run=False)
    restarted = LocalTaskQueue(tmp_path)
    assert restarted.inspect_local("survives")["outcome"]["outcome"] == "never_started"


def test_migration_validation_failure_restores_original_schema(tmp_path, monkeypatch):
    queue = LocalTaskQueue(tmp_path)
    queue.submit("legacy-rollback")
    with sqlite3.connect(queue.path) as db:
        db.execute("UPDATE local_meta SET value=? WHERE key='schema'", (LEGACY_SCHEMA,))
    legacy = LocalTaskQueue(tmp_path, allow_legacy=True)
    monkeypatch.setattr(legacy, "doctor_local", lambda: {"healthy": False, "error": "injected"})
    with pytest.raises(QueueUnavailable, match="post-migration validation failed"):
        legacy.migrate(dry_run=False)
    with sqlite3.connect(legacy.path) as db:
        assert db.execute("SELECT value FROM local_meta WHERE key='schema'").fetchone()[0] == LEGACY_SCHEMA


def test_v1_provenance_migration_and_schema_gate(tmp_path):
    queue = LocalTaskQueue(tmp_path)
    queue.submit("legacy")
    lease = queue.claim_local("legacy", "w", idempotency_key="legacy")
    queue.persist_intent(lease, {"effect": "legacy"})
    queue.record_outcome(lease, "retryable_failure", provenance={"idempotent": True})
    with sqlite3.connect(queue.path) as db:
        db.execute("UPDATE local_meta SET value=? WHERE key='schema'", (LEGACY_SCHEMA,))
        for field in ("intent", "receipt", "provenance"):
            value = json.loads(db.execute(f"SELECT {field} FROM local_outcomes WHERE task_id='legacy'").fetchone()[0])
            value["schema"] = LEGACY_SCHEMA
            value.pop("digest", None)
            from simplicio_loop.local_task_queue import _digest
            value["digest"] = _digest(value)
            db.execute(f"UPDATE local_outcomes SET {field}=? WHERE task_id='legacy'", (json.dumps(value),))
        for seq, raw in db.execute("SELECT seq,payload FROM local_transitions").fetchall():
            value = json.loads(raw)
            value["schema"] = LEGACY_SCHEMA
            db.execute("UPDATE local_transitions SET payload=?,digest=? WHERE seq=?",
                       (json.dumps(value), _digest(value), seq))
    with pytest.raises(QueueUnavailable, match="run `simplicio-loop queue migrate`"):
        LocalTaskQueue(tmp_path)
    legacy = LocalTaskQueue(tmp_path, allow_legacy=True)
    result = legacy.migrate(dry_run=False)
    assert result["from_schema"] == LEGACY_SCHEMA
    assert result["migrated_provenance"] == 1
    restarted = LocalTaskQueue(tmp_path)
    assert restarted.doctor_local()["healthy"] is True
    stored = json.loads(restarted.inspect_local("legacy")["outcome"]["provenance"])
    assert stored["schema"] == SCHEMA and stored["digest"].startswith("sha256:")
    assert restarted.inspect_local("legacy")["transitions"][0]["payload"].find(SCHEMA) >= 0


@pytest.mark.parametrize("target", ["intent", "transition"])
def test_v1_migration_rejects_tamper_before_rehash_and_preserves_legacy(tmp_path, target):
    queue = LocalTaskQueue(tmp_path)
    queue.submit("legacy-tamper")
    lease = queue.claim_local("legacy-tamper", "w", idempotency_key="legacy-tamper")
    queue.persist_intent(lease, {"effect": "original"})
    with contextlib.closing(sqlite3.connect(queue.path)) as db, db:
        db.execute("UPDATE local_meta SET value=? WHERE key='schema'", (LEGACY_SCHEMA,))
        if target == "intent":
            value = json.loads(db.execute(
                "SELECT intent FROM local_outcomes WHERE task_id='legacy-tamper'"
            ).fetchone()[0])
            value["schema"] = LEGACY_SCHEMA
            value["effect"] = "tampered"
            db.execute("UPDATE local_outcomes SET intent=? WHERE task_id='legacy-tamper'",
                       (json.dumps(value),))
        else:
            seq, raw = db.execute(
                "SELECT seq,payload FROM local_transitions ORDER BY seq LIMIT 1"
            ).fetchone()
            value = json.loads(raw)
            value["schema"] = LEGACY_SCHEMA
            value["payload"]["tampered"] = True
            db.execute("UPDATE local_transitions SET payload=? WHERE seq=?",
                       (json.dumps(value), seq))

    legacy = LocalTaskQueue(tmp_path, allow_legacy=True)
    with pytest.raises(QueueUnavailable, match="invalid legacy .* digest"):
        legacy.migrate(dry_run=False)
    with contextlib.closing(sqlite3.connect(legacy.path)) as db:
        assert db.execute("SELECT value FROM local_meta WHERE key='schema'").fetchone()[0] == LEGACY_SCHEMA


@pytest.mark.parametrize("field", ["intent", "receipt"])
def test_v1_migration_rejects_unversioned_effect_envelopes(tmp_path, field):
    queue = LocalTaskQueue(tmp_path)
    queue.submit("legacy-unversioned")
    lease = queue.claim_local("legacy-unversioned", "w", idempotency_key="legacy-unversioned")
    queue.persist_intent(lease, {"effect": "original"})
    if field == "receipt":
        queue.record_outcome(lease, "verified_success", receipt={"proof": "ok"})
    with contextlib.closing(sqlite3.connect(queue.path)) as db, db:
        db.execute("UPDATE local_meta SET value=? WHERE key='schema'", (LEGACY_SCHEMA,))
        from simplicio_loop.local_task_queue import _digest
        for envelope_field in ("intent", "receipt"):
            raw = db.execute(
                f"SELECT {envelope_field} FROM local_outcomes WHERE task_id='legacy-unversioned'"
            ).fetchone()[0]
            if raw:
                envelope = json.loads(raw)
                envelope["schema"] = LEGACY_SCHEMA
                envelope.pop("digest", None)
                envelope["digest"] = _digest(envelope)
                db.execute(
                    f"UPDATE local_outcomes SET {envelope_field}=? WHERE task_id='legacy-unversioned'",
                    (json.dumps(envelope),),
                )
        value = json.loads(db.execute(
            f"SELECT {field} FROM local_outcomes WHERE task_id='legacy-unversioned'"
        ).fetchone()[0])
        value.pop("schema", None)
        value.pop("digest", None)
        db.execute(f"UPDATE local_outcomes SET {field}=? WHERE task_id='legacy-unversioned'",
                   (json.dumps(value),))

    legacy = LocalTaskQueue(tmp_path, allow_legacy=True)
    with pytest.raises(QueueUnavailable, match=f"invalid legacy {field} digest"):
        legacy.migrate(dry_run=False)


def test_v1_migration_is_exposed_through_json_cli(tmp_path, capsys):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    queue = LocalTaskQueue(tmp_path)
    queue.submit("legacy-cli")
    with sqlite3.connect(queue.path) as db:
        db.execute("UPDATE local_meta SET value=? WHERE key='schema'", (LEGACY_SCHEMA,))
        db.execute("UPDATE local_outcomes SET provenance=? WHERE task_id='legacy-cli'",
                   (json.dumps({"key": "legacy"}),))
    assert cli_main(["--repo", str(tmp_path), "migrate"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert cli_main(["--repo", str(tmp_path), "migrate", "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["from_schema"] == LEGACY_SCHEMA
    assert applied["migrated_provenance"] == 1


def test_cli_rejects_non_root_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    child = tmp_path / "child"
    child.mkdir()
    assert cli_main(["--repo", str(child), "status"]) == 2


def test_benchmark_thresholds_are_enforced(monkeypatch, capsys):
    from bench import benchmark_local_task_queue_889 as benchmark

    assert benchmark.main(["--max-enqueue-us", "0", "--max-claim-us", "0"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["thresholds"] == {"claim": False, "enqueue": False}
