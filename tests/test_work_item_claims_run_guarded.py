"""Prove #183's callout that a long-running mutation had no heartbeat/fence check while
running is fixed: ``AttemptCoordinator.run_guarded`` kills the child process and raises
the instant the lease is no longer active, instead of letting a stale worker keep
mutating the checkout.

Deterministic and offline: no real network queue, no sleeping on wall-clock timeouts
beyond a small, bounded heartbeat interval used purely to keep the test fast.
"""
import sys
import time

import pytest

from simplicio_loop.remote_queue import SQLiteRemoteQueue
from simplicio_loop.work_item_claims import AttemptCoordinator, LeaseLostDuringExecution


IDENTITY = {
    "agent_id": "codex@device-a", "runtime": "codex", "device_id": "device-a",
    "session_id": "session-a", "capabilities": ["claim", "heartbeat", "fencing", "receipts", "events"],
}


def _long_sleep_argv(seconds: float):
    # Cross-platform: a short-lived python subprocess instead of a shell `sleep`.
    return [sys.executable, "-c", "import time; time.sleep(%r)" % seconds]


def test_run_guarded_returns_normally_when_lease_stays_current(tmp_path):
    queue = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    coordinator = AttemptCoordinator(queue, run_id="run-guard-1")
    attempt = coordinator.claim(work_item_id="WI-G1", identity=IDENTITY, goal="mutate safely", ttl=60.0)
    result = coordinator.run_guarded(
        attempt, [sys.executable, "-c", "print('ok')"], cwd=tmp_path,
        timeout=10.0, heartbeat_interval=0.2, ttl=60.0,
    )
    assert result.returncode == 0
    assert "ok" in result.stdout


def test_run_guarded_kills_subprocess_and_raises_on_lease_loss(tmp_path):
    import threading

    queue = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    coordinator = AttemptCoordinator(queue, run_id="run-guard-2")
    # Lease is valid when execution starts (so the pre-flight assert_active passes and the
    # subprocess actually launches); it is then stolen by another identity from a background
    # thread WHILE the guarded subprocess is running, simulating a second worker winning the
    # fence after this one's lease lapsed mid-mutation.
    attempt = coordinator.claim(work_item_id="WI-G2", identity=IDENTITY, goal="mutate", ttl=60.0)

    def _steal_lease_soon():
        time.sleep(0.25)
        # Release this worker's lease (e.g. the coordinator process crashed/handed off) and
        # let a second identity win the fence — the classic "stale worker keeps mutating"
        # scenario the DoD gap named.
        queue.release(attempt.lease, reason="handoff")
        other = dict(IDENTITY, agent_id="claude@device-b", runtime="claude", device_id="device-b",
                     session_id="session-b")
        AttemptCoordinator(queue, run_id="run-guard-2").claim(work_item_id="WI-G2", identity=other, goal="mutate")

    thief = threading.Thread(target=_steal_lease_soon, daemon=True)
    thief.start()

    before = time.time()
    with pytest.raises(LeaseLostDuringExecution) as exc_info:
        coordinator.run_guarded(
            attempt, _long_sleep_argv(5.0), cwd=tmp_path,
            timeout=10.0, heartbeat_interval=0.1, ttl=60.0,
        )
    elapsed = time.time() - before
    thief.join(timeout=2)
    # The subprocess (would-be 5s sleep) must be killed well before its natural exit —
    # proves the mutation was actually terminated, not merely detected after the fact.
    assert elapsed < 4.0
    assert exc_info.value.work_item_id == "WI-G2"


def test_run_guarded_checks_lease_before_starting(tmp_path):
    queue = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    coordinator = AttemptCoordinator(queue, run_id="run-guard-3")
    attempt = coordinator.claim(work_item_id="WI-G3", identity=IDENTITY, goal="mutate", ttl=0.01)
    time.sleep(0.05)
    other = dict(IDENTITY, agent_id="claude@device-b", runtime="claude", device_id="device-b",
                 session_id="session-b")
    AttemptCoordinator(queue, run_id="run-guard-3").claim(work_item_id="WI-G3", identity=other, goal="mutate")
    from simplicio_loop.remote_queue import QueueConflict
    with pytest.raises(QueueConflict):
        coordinator.run_guarded(attempt, [sys.executable, "-c", "print('should not run')"], cwd=tmp_path)


def test_run_guarded_timeout_still_raises_timeout_expired(tmp_path):
    queue = SQLiteRemoteQueue(str(tmp_path / "queue.db"))
    coordinator = AttemptCoordinator(queue, run_id="run-guard-4")
    attempt = coordinator.claim(work_item_id="WI-G4", identity=IDENTITY, goal="mutate", ttl=60.0)
    import subprocess
    with pytest.raises(subprocess.TimeoutExpired):
        coordinator.run_guarded(
            attempt, _long_sleep_argv(5.0), cwd=tmp_path,
            timeout=0.3, heartbeat_interval=0.1, ttl=60.0,
        )
