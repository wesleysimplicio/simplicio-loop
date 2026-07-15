"""Real supervisor-restarts-a-crashed-daemon proof for issue #286.

``scripts/remote_worker_supervisor.py`` is spawned as its own real OS process
(``subprocess.Popen``), which in turn spawns a real ``scripts/remote_worker_daemon.py serve``
child. This test:

  1. confirms the supervised worker is genuinely healthy before any kill (it claims and
     completes a real task against a shared SQLite queue),
  2. hard-kills the worker's OS process directly by PID (not via the supervisor, and not via
     the test's own Popen handle -- the supervisor's child, found only through the status file
     it wrote, exactly like an operator killing a stray process out-of-band),
  3. asserts the supervisor detects the exit and spawns a genuinely new process (a different
     PID) without any test intervention,
  4. proves the *new* worker is actually healthy -- not merely alive -- by completing a second
     real task,
  5. tears the supervisor down and confirms it stops its own children cleanly.

No thread stands in for a process anywhere in this chain; every kill and every restart is a
real OS-level event.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

_IS_WINDOWS = sys.platform == "win32"

REPO_ROOT = Path(__file__).resolve().parent.parent
SUPERVISOR = REPO_ROOT / "scripts" / "remote_worker_supervisor.py"


def _read_json(path: Path, *, timeout: float = 10.0, predicate=None) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        if path.exists():
            try:
                last = json.loads(path.read_text(encoding="utf-8"))
                if predicate is None or predicate(last):
                    return last
            except (json.JSONDecodeError, OSError):
                pass
        time.sleep(0.05)
    raise TimeoutError(f"{path} never satisfied condition; last seen: {last}")


def _wait_task_status(queue, task_id: str, status: str, *, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = queue.task(task_id)
        if last["status"] == status:
            return last
        time.sleep(0.1)
    raise TimeoutError(f"task {task_id} never reached {status!r}; last seen: {last}")


def _hard_kill(pid: int) -> None:
    sig = getattr(signal, "SIGKILL", None) or signal.SIGTERM
    os.kill(pid, sig)


def test_supervisor_restarts_a_hard_killed_worker_and_stays_healthy(tmp_path):
    from simplicio_loop.remote_queue import SQLiteRemoteQueue

    db = tmp_path / "shared-queue.db"
    status_dir = tmp_path / "status"
    queue = SQLiteRemoteQueue(str(db))
    queue.enqueue("SUPERVISOR-T1", {"goal": "prove pre-kill health"})

    extra_kwargs = {}
    if _IS_WINDOWS:
        # Required so a later CTRL_BREAK_EVENT targets only this process's group (its own
        # console group), letting the supervisor's SIGBREAK handler run its graceful shutdown
        # instead of the blunt TerminateProcess that Popen.terminate() would issue on Windows.
        extra_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [sys.executable, str(SUPERVISOR), "--db", str(db), "--workers", "1",
         "--agent-id-prefix", "supervised", "--ttl", "3", "--heartbeat-interval", "0.3",
         "--work-seconds", "0.5", "--poll-interval", "0.2", "--health-interval", "0.2",
         "--restart-backoff-seconds", "0.1", "--status-dir", str(status_dir)],
        cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        stdin=subprocess.DEVNULL, **extra_kwargs,
    )
    worker_status_path = status_dir / "worker-0.status.json"
    supervisor_status_path = status_dir / "supervisor.status.json"
    try:
        # 1) The worker is genuinely healthy before any kill: it discovers, claims, and
        # completes the pre-enqueued task on its own.
        _wait_task_status(queue, "SUPERVISOR-T1", "completed", timeout=15.0)
        worker_status = _read_json(worker_status_path, timeout=10.0)
        pid_before = worker_status["pid"]
        assert pid_before != proc.pid  # a real, distinct child process, not the supervisor itself

        supervisor_status = _read_json(
            supervisor_status_path, timeout=10.0,
            predicate=lambda s: s.get("workers") and s["workers"][0]["pid"] == pid_before,
        )
        assert supervisor_status["workers"][0]["restarts"] == 0

        # 2) Hard-kill the worker process directly by PID -- a real crash, no graceful stop,
        # discovered only through the status file (as an operator would find it).
        _hard_kill(pid_before)

        # 3) The supervisor notices the exit and spawns a genuinely new process.
        restarted_status = _read_json(
            worker_status_path, timeout=15.0,
            predicate=lambda s: s["pid"] != pid_before,
        )
        pid_after = restarted_status["pid"]
        assert pid_after != pid_before

        supervisor_status_after = _read_json(
            supervisor_status_path, timeout=15.0,
            predicate=lambda s: s.get("workers") and s["workers"][0]["restarts"] >= 1,
        )
        assert supervisor_status_after["workers"][0]["pid"] == pid_after
        assert supervisor_status_after["workers"][0]["last_exit_code"] is not None

        # 4) The *new* worker is genuinely healthy, not merely alive: it completes a second
        # real task end-to-end.
        queue.enqueue("SUPERVISOR-T2", {"goal": "prove post-restart health"})
        _wait_task_status(queue, "SUPERVISOR-T2", "completed", timeout=15.0)
    finally:
        if proc.poll() is None:
            if _IS_WINDOWS:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

    assert proc.returncode == 0, proc.stderr.read()

    final_supervisor_status = json.loads(supervisor_status_path.read_text(encoding="utf-8"))
    assert final_supervisor_status["workers"][0]["alive"] is False


if __name__ == "__main__":
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_remote_worker_supervisor")
