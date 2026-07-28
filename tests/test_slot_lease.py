from __future__ import annotations

import multiprocessing
import random

import pytest

from simplicio_loop.slot_lease import LeaseConflict, LeaseStore, StaleFence


class Clock:
    def __init__(self, value=1_000.0):
        self.value = value

    def __call__(self):
        return self.value


def _race_acquire(path, owner, start, results):
    store = LeaseStore(path)
    start.wait()
    try:
        lease = store.acquire("repo:issue:807", owner, ttl_seconds=30)["lease"]
        results.put(("won", lease["owner_id"], lease["fence"]))
    except LeaseConflict:
        results.put(("lost", owner, None))


def test_two_processes_never_hold_exclusivity_simultaneously(tmp_path):
    path = str(tmp_path / "leases.db")
    ctx = multiprocessing.get_context("spawn")
    start, results = ctx.Event(), ctx.Queue()
    processes = [
        ctx.Process(target=_race_acquire, args=(path, f"worker-{i}", start, results))
        for i in range(6)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    rows = [results.get(timeout=2) for _ in processes]
    assert sum(row[0] == "won" for row in rows) == 1


def test_persistence_expiry_reclaim_and_old_writer_fenced(tmp_path):
    clock = Clock()
    path = tmp_path / "leases.db"
    first = LeaseStore(path, clock=clock)
    lease1 = first.acquire("slot:807", "worker-a", ttl_seconds=10)["lease"]
    first.put("slot:807", "worker-a", lease1["fence"], "result", {"v": 1})

    # New process/store sees persisted ownership. Simulates holder death.
    restarted = LeaseStore(path, clock=clock)
    with pytest.raises(LeaseConflict):
        restarted.acquire("slot:807", "worker-b", ttl_seconds=10)
    clock.value += 11
    stale_receipts = restarted.mark_stale()
    assert stale_receipts[0]["event"] == "marked_stale"
    lease2 = restarted.reclaim("slot:807", "worker-b", ttl_seconds=10)["lease"]
    assert lease2["attempt"] == lease1["attempt"] + 1
    assert lease2["fence"] == lease1["fence"] + 1

    with pytest.raises(StaleFence):
        first.put("slot:807", "worker-a", lease1["fence"], "result", {"v": 2})
    restarted.put("slot:807", "worker-b", lease2["fence"], "result", {"v": 3})
    assert restarted.read("slot:807", "result") == {"v": 3}


def test_heartbeat_extends_bounded_ttl_and_receipt_carries_fence(tmp_path):
    clock = Clock()
    store = LeaseStore(tmp_path / "leases.db", clock=clock, max_ttl_seconds=30)
    acquired = store.acquire("slot:1", "worker", ttl_seconds=10)
    clock.value += 5
    renewed = store.heartbeat(
        "slot:1", "worker", acquired["lease"]["fence"], ttl_seconds=10
    )
    assert renewed["lease"]["expires_at"] == 1_015
    assert renewed["receipt"]["fence"] == acquired["lease"]["fence"]
    assert renewed["receipt"]["receipt_hash"].startswith("sha256:")
    with pytest.raises(ValueError):
        store.heartbeat(
            "slot:1", "worker", acquired["lease"]["fence"], ttl_seconds=31
        )


def test_stale_heartbeat_cannot_resurrect_old_lease(tmp_path):
    clock = Clock()
    store = LeaseStore(tmp_path / "leases.db", clock=clock)
    old = store.acquire("slot:1", "old", ttl_seconds=2)["lease"]
    clock.value += 3
    current = store.reclaim("slot:1", "new", ttl_seconds=10)["lease"]
    with pytest.raises(StaleFence):
        store.heartbeat(
            "slot:1", "old", old["fence"], ttl_seconds=10
        )
    assert store.status("slot:1")["fence"] == current["fence"]


def test_release_requires_current_fence(tmp_path):
    store = LeaseStore(tmp_path / "leases.db")
    lease = store.acquire("slot:1", "owner", ttl_seconds=10)["lease"]
    with pytest.raises(StaleFence):
        store.release("slot:1", "owner", lease["fence"] - 1)
    released = store.release("slot:1", "owner", lease["fence"])
    assert released["lease"]["state"] == "released"


def test_property_reducer_never_decreases_fence_or_allows_two_owners(tmp_path):
    clock, rng = Clock(), random.Random(807)
    store = LeaseStore(tmp_path / "leases.db", clock=clock)
    last_fence = 0
    current = None
    for _ in range(250):
        action = rng.choice(("acquire", "heartbeat", "advance", "release"))
        if action == "advance":
            clock.value += rng.random() * 3
        elif action == "acquire":
            owner = f"worker-{rng.randrange(5)}"
            try:
                result = store.acquire("property", owner, ttl_seconds=2)
                current = result["lease"]
                assert current["fence"] > last_fence
                last_fence = current["fence"]
            except LeaseConflict:
                assert store.status("property")["conflict"]
        elif action == "heartbeat" and current:
            try:
                store.heartbeat(
                    "property", current["owner_id"], current["fence"],
                    ttl_seconds=2,
                )
            except StaleFence:
                current = None
        elif action == "release" and current:
            try:
                store.release(
                    "property", current["owner_id"], current["fence"]
                )
            except StaleFence:
                pass
            current = None
        status = store.status("property")
        if status and status["state"] == "active":
            assert status["fence"] == last_fence


def test_deterministic_receipt_hashes_with_fixed_clock(tmp_path):
    store = LeaseStore(tmp_path / "leases.db", clock=Clock())
    store.acquire("slot:fixture", "worker", ttl_seconds=10)
    receipts = store.receipts("slot:fixture")
    assert receipts == [
        {
            "attempt": 1,
            "event": "acquired",
            "expires_at": 1010.0,
            "fence": 1,
            "observed_at": 1000.0,
            "owner_id": "worker",
            "reason": None,
            "receipt_hash": "sha256:93880017a57367a3ea923fd4324638f536949db38893618e81c12ba8aa483c1c",
            "resource_key": "slot:fixture",
            "schema": "simplicio.capability-lease-receipt/v1",
            "state": "active",
        }
    ]
