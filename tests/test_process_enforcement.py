"""Real tests for the #516 supervisor observability/enforcement slice.

These tests spawn actual OS subprocesses (never mocked) to prove: a process started outside the
supervisor is flagged by the detector, a process started through the supervisor is not, the
default (opt-in-off) enforcement mode never signals anything, an opt-in-on enforcement pass can
terminate a real flagged process, and the circuit breaker trips on repeated spawn failures while
still completing subsequent work via the standalone fallback path.

Safety note for this shared, multi-agent host: ``enforce(..., enabled=True)`` is only ever
called here with a list *filtered down to a canary pid this test itself spawned* -- never with
the raw, unfiltered output of a host-wide ``detect_unsupervised`` scan, which could otherwise
match and signal unrelated legitimate processes belonging to other concurrent agents/worktrees.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time

from simplicio_loop.process_enforcement import (
    CircuitBreaker,
    ProcessRegistry,
    SupervisedProcessAdapter,
    append_event,
    detect_unsupervised,
    enforce,
    enforcement_enabled,
    is_simplicio_cmdline,
    read_events,
    run_guarded,
)
from simplicio_loop.process_supervisor import ProcessSpec

MARKER = "simplicio-mapper"  # matches SIMPLICIO_SIGNATURES, harmless extra argv token


def _wait_visible(pid: int, *, timeout: float = 3.0) -> None:
    """Block until /proc/<pid>/cmdline is readable and populated, so a scan right after spawn
    does not race the kernel's own process-table/exec bookkeeping."""
    deadline = time.monotonic() + timeout
    cmdline_path = "/proc/%d/cmdline" % pid
    while time.monotonic() < deadline:
        try:
            with open(cmdline_path, "rb") as handle:
                if handle.read():
                    return
        except OSError:
            pass
        time.sleep(0.02)


def _spawn_marker_canary(sleep_seconds: float = 2.5) -> subprocess.Popen:
    """A real subprocess that bypasses the supervisor entirely, but whose cmdline still matches
    the ecosystem signature (via the extra ``MARKER`` positional argv token, which ``python -c``
    ignores at runtime -- it only affects what ``sys.argv`` would be inside the script)."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(%r)" % sleep_seconds, MARKER],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    )
    _wait_visible(proc.pid)
    return proc


def _wait_reaped(proc: subprocess.Popen, *, timeout: float = 3.0) -> bool:
    """True once this test's own child ``proc`` has actually exited, reaping it along the way
    so it is not left as a zombie waiting on a ``wait()`` this test never calls."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.05)
    return False


def test_signature_matching() -> None:
    assert is_simplicio_cmdline(["python3", "-m", "simplicio_loop.cli", "status"])
    assert is_simplicio_cmdline(["simplicio-mapper", "scan"])
    assert not is_simplicio_cmdline(["python3", "-c", "print(1)"])
    assert not is_simplicio_cmdline(["bash", "-c", "ls"])


def test_detector_flags_unsupervised_but_not_supervised(tmp_path) -> None:
    registry = ProcessRegistry(tmp_path / "registry.json")

    # 1) A process launched OUTSIDE the supervisor: no registry entry at all.
    outside = _spawn_marker_canary()
    try:
        flagged_pids = {record.pid for record in detect_unsupervised(registry)}
        assert outside.pid in flagged_pids, "unsupervised marker process must be flagged"
    finally:
        outside.terminate()
        outside.wait(timeout=5)

    # 2) The same kind of process, but launched PROPERLY through the supervisor: while it is
    #    still running, its pid must be registered and therefore NOT flagged.
    adapter = SupervisedProcessAdapter(registry=registry)
    spec = ProcessSpec((sys.executable, "-c", "import time; time.sleep(1.2)", MARKER))

    async def scenario() -> None:
        task = asyncio.ensure_future(adapter.run(spec))
        # Give the child a moment to spawn and register before we scan.
        deadline = time.monotonic() + 2.0
        supervised_pid = None
        while time.monotonic() < deadline and not registry.active():
            await asyncio.sleep(0.02)
        active = registry.active()
        assert active, "supervised process should have registered a pid before completing"
        supervised_pid = next(iter(active))
        flagged_pids = {record.pid for record in detect_unsupervised(registry)}
        assert supervised_pid not in flagged_pids, "supervised process must not be flagged"
        await task

    asyncio.run(scenario())


def test_enforcement_default_off_observes_only_and_kills_nothing(tmp_path) -> None:
    assert enforcement_enabled() is False, "enforcement must default to opt-in OFF"
    canary = _spawn_marker_canary()
    try:
        registry = ProcessRegistry(tmp_path / "registry.json")
        flagged = [r for r in detect_unsupervised(registry) if r.pid == canary.pid]
        assert flagged, "the canary should be detected as flagged"
        actions = enforce(flagged, enabled=False)
        assert all(action["action"] == "observed_only" for action in actions)
        # Real proof nothing was killed: the process is still alive.
        assert canary.poll() is None, "enforcement-off must never kill a flagged process"
    finally:
        canary.terminate()
        canary.wait(timeout=5)


def test_enforcement_opt_in_terminates_a_flagged_process(tmp_path) -> None:
    canary = _spawn_marker_canary(sleep_seconds=10.0)
    try:
        registry = ProcessRegistry(tmp_path / "registry.json")
        # Scope strictly to the canary this test owns -- never act on the unfiltered host-wide
        # scan on this shared, multi-agent machine.
        flagged = [r for r in detect_unsupervised(registry) if r.pid == canary.pid]
        assert flagged
        actions = enforce(flagged, enabled=True)
        assert actions[0]["action"] == "signaled"
        assert _wait_reaped(canary), "opt-in enforcement must actually terminate the process"
    finally:
        if canary.poll() is None:
            canary.terminate()
        canary.wait(timeout=5)


def test_circuit_breaker_trips_and_standalone_fallback_still_completes_work() -> None:
    async def scenario() -> None:
        breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=100.0)
        bad_spec = ProcessSpec(("simplicio-no-such-executable-516",))
        good_spec = ProcessSpec((sys.executable, "-c", "print('ok')"))

        first = await run_guarded(bad_spec, breaker=breaker)
        assert first["mode"] == "supervised"
        assert first["result"].error_code == "executable_not_found"
        assert breaker.state == "closed", "one failure must not trip a threshold-of-2 breaker"

        second = await run_guarded(bad_spec, breaker=breaker)
        assert second["mode"] == "supervised"
        assert breaker.state == "open", "two consecutive spawn failures must trip the breaker"

        third = await run_guarded(good_spec, breaker=breaker)
        assert third["mode"] == "standalone_fallback"
        assert third["result"].returncode == 0
        assert third["result"].stdout.strip() == "ok"

    asyncio.run(scenario())


def test_circuit_breaker_persists_across_load_save(tmp_path) -> None:
    path = tmp_path / "breaker.json"
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=100.0)
    breaker.record_failure("spawn_error")
    assert breaker.state == "open"
    breaker.save(path)

    reloaded = CircuitBreaker.load(path)
    assert reloaded.state == "open"
    assert reloaded.trip_reason == "spawn_error"


def test_registry_prunes_dead_pids(tmp_path) -> None:
    registry = ProcessRegistry(tmp_path / "registry.json")
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    registry.register(proc.pid, lease_id="lease-1", spec_hash="hash-1", argv=["x"])
    # The pid has already exited; prune_dead (called by active()) must drop it rather than
    # report a stale/possibly-reused pid as still supervised.
    assert proc.pid not in registry.active()


def test_events_log_round_trips(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    append_event("detection_scan", {"flagged_count": 0}, path=path)
    append_event("cancel", {"pid": 4242, "ok": True}, path=path)
    events = read_events(path=path, limit=10)
    assert [event["kind"] for event in events] == ["detection_scan", "cancel"]


def test_cli_status_top_queue_cancel_drain_reports(tmp_path) -> None:
    registry_path = tmp_path / "registry.json"
    module = [sys.executable, "-m", "simplicio_loop.process_enforcement_cli"]

    def run_cli(*args: str) -> dict:
        completed = subprocess.run(
            module + ["--registry", str(registry_path)] + list(args),
            capture_output=True, text=True, timeout=15, check=True,
        )
        return json.loads(completed.stdout)

    status = run_cli("status")
    assert status["schema"] == "simplicio.supervisor-status/v1"
    assert status["enforcement_enabled"] is False
    assert status["active_supervised_count"] == 0

    empty_top = run_cli("top")
    assert empty_top["processes"] == []

    empty_queue = run_cli("queue")
    assert empty_queue["in_flight"] == 0
    assert "note" in empty_queue

    # Register a real disposable process directly (simulating a supervised lease) so top/queue
    # /cancel have something real to operate on.
    canary = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    )
    try:
        registry = ProcessRegistry(registry_path)
        registry.register(canary.pid, lease_id="cli-lease", spec_hash="h", argv=["sleep"])

        top = run_cli("top")
        assert len(top["processes"]) == 1
        assert top["processes"][0]["pid"] == canary.pid
        assert top["processes"][0]["lease_id"] == "cli-lease"

        queue = run_cli("queue")
        assert queue["in_flight"] == 1

        cancelled = run_cli("cancel", "--lease-id", "cli-lease")
        assert cancelled["ok"] is True
        assert cancelled["pid"] == canary.pid
        assert _wait_reaped(canary), "CLI cancel must actually terminate the process"
    finally:
        if canary.poll() is None:
            canary.terminate()
        canary.wait(timeout=5)

    # Drain: registry should self-prune the now-dead pid and report drained quickly.
    drained = run_cli("drain", "--timeout", "2", "--poll-interval", "0.1")
    assert drained["drained"] is True
    assert drained["remaining"] == 0

    reports = run_cli("reports", "--limit", "10")
    assert reports["schema"] == "simplicio.supervisor-reports/v1"
    kinds = [event["kind"] for event in reports["events"]]
    assert "cancel" in kinds


def test_cli_drain_force_kills_remaining_after_timeout(tmp_path) -> None:
    registry_path = tmp_path / "registry.json"
    module = [sys.executable, "-m", "simplicio_loop.process_enforcement_cli"]
    canary = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    )
    try:
        registry = ProcessRegistry(registry_path)
        registry.register(canary.pid, lease_id="force-lease", spec_hash="h", argv=["sleep"])

        completed = subprocess.run(
            module + ["--registry", str(registry_path), "drain",
                      "--timeout", "0.3", "--poll-interval", "0.1", "--force"],
            capture_output=True, text=True, timeout=15, check=True,
        )
        result = json.loads(completed.stdout)
        assert result["drained"] is False
        assert result["remaining"] == 1
        assert result["forced"] is True
        assert _wait_reaped(canary, timeout=5), "--force must actually terminate the leftover process"
    finally:
        if canary.poll() is None:
            canary.terminate()
        canary.wait(timeout=5)
