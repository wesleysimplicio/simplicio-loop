#!/usr/bin/env python3
"""Packaged CLI: real worker-daemon lifecycle supervisor for issue #286.

Runs as its own OS process. Spawns ``--workers`` copies of
``python -m simplicio_loop.remote_worker_cli serve`` (real ``subprocess.Popen`` children, not
threads), polls each child's liveness every ``--health-interval`` seconds, and restarts
(respawns) any child that has exited -- crashed, killed, or otherwise -- after
``--restart-backoff-seconds``. This is what makes "worker daemon lifecycle/supervisor" real
rather than prose: a ``kill -9`` (POSIX) / ``TerminateProcess`` (Windows) on a managed worker is
detected and recovered from without any manual intervention.

The supervisor writes ``--status-file`` after every health-check tick: each managed worker's
current pid, cumulative restart count, and last observed exit code, so a test or an operator
can assert on real, observed state rather than guessing timing.

Lives inside the ``simplicio_loop`` package (issue #286 step 11) -- unlike the historical
``scripts/remote_worker_supervisor.py``, this module ships in the installed wheel/sdist and
spawns its children via ``-m simplicio_loop.remote_worker_cli`` (a module invocation that works
regardless of whether ``scripts/`` is even present on disk), so ``pip install simplicio-loop``
gets a genuinely runnable supervisor binary (the ``simplicio-remote-worker-supervisor`` console
script). ``scripts/remote_worker_supervisor.py`` is kept as a thin backward-compatible shim over
this module for existing repo-local tooling/tests.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _write_status(status_file: str, payload: Dict[str, Any]) -> None:
    path = Path(status_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


class ManagedWorker:
    """One supervised ``simplicio_loop.remote_worker_cli serve`` child process."""

    def __init__(self, index: int, argv: List[str], *, status_file: str, cwd: Optional[str] = None) -> None:
        self.index = index
        self.argv = argv
        self.status_file = status_file
        self.cwd = cwd
        self.restarts = 0
        self.last_exit_code: Optional[int] = None
        self.proc: subprocess.Popen = self._spawn()

    def _spawn(self) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-m", "simplicio_loop.remote_worker_cli", *self.argv],
            cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            stdin=subprocess.DEVNULL,
        )

    def poll_and_restart(self, *, backoff_seconds: float) -> bool:
        """Return True if a restart happened this tick."""
        rc = self.proc.poll()
        if rc is None:
            return False
        self.last_exit_code = rc
        self.restarts += 1
        if backoff_seconds > 0:
            time.sleep(backoff_seconds)
        self.proc = self._spawn()
        return True

    def terminate(self, *, timeout: float = 5.0) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=timeout)

    def to_status(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "pid": self.proc.pid,
            "alive": self.proc.poll() is None,
            "restarts": self.restarts,
            "last_exit_code": self.last_exit_code,
            "status_file": self.status_file,
        }


class WorkerSupervisor:
    """Owns a fixed-size pool of :class:`ManagedWorker` and keeps it alive."""

    def __init__(self, workers: List[ManagedWorker], *, health_interval: float,
                restart_backoff_seconds: float, status_file: str) -> None:
        self.workers = workers
        self.health_interval = health_interval
        self.restart_backoff_seconds = restart_backoff_seconds
        self.status_file = status_file
        self._stop = False

    def request_stop(self) -> None:
        self._stop = True

    def _write_status(self) -> None:
        _write_status(self.status_file, {
            "pid": os.getpid(), "ts": time.time(),
            "workers": [w.to_status() for w in self.workers],
        })

    def run_forever(self) -> None:
        self._write_status()
        try:
            while not self._stop:
                for worker in self.workers:
                    worker.poll_and_restart(backoff_seconds=self.restart_backoff_seconds)
                self._write_status()
                time.sleep(self.health_interval)
        finally:
            for worker in self.workers:
                worker.terminate()
            self._write_status()


def _build_worker_argv(index: int, args: argparse.Namespace, status_file: str) -> List[str]:
    argv = ["serve"]
    if args.db:
        argv += ["--db", args.db]
    else:
        argv += ["--http", args.http]
        if args.token:
            argv += ["--token", args.token]
    argv += [
        "--agent-id", f"{args.agent_id_prefix}-{index}",
        "--ttl", str(args.ttl),
        "--heartbeat-interval", str(args.heartbeat_interval),
        "--work-seconds", str(args.work_seconds),
        "--poll-interval", str(args.poll_interval),
        "--status-file", status_file,
    ]
    return argv


def build_supervisor(args: argparse.Namespace) -> WorkerSupervisor:
    status_dir = Path(args.status_dir)
    status_dir.mkdir(parents=True, exist_ok=True)
    workers = []
    for i in range(args.workers):
        status_file = str(status_dir / f"worker-{i}.status.json")
        argv = _build_worker_argv(i, args, status_file)
        workers.append(ManagedWorker(i, argv, status_file=status_file, cwd=args.cwd))
    return WorkerSupervisor(
        workers, health_interval=args.health_interval,
        restart_backoff_seconds=args.restart_backoff_seconds,
        status_file=str(status_dir / "supervisor.status.json"),
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None, help="shared SQLite queue file path")
    parser.add_argument("--http", default=None, help="base URL of a real remote-queue-server instance")
    parser.add_argument("--token", default=os.environ.get("SIMPLICIO_QUEUE_TOKEN"))
    parser.add_argument("--agent-id-prefix", default="worker")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--ttl", type=float, default=5.0)
    parser.add_argument("--heartbeat-interval", type=float, default=1.0)
    parser.add_argument("--work-seconds", type=float, default=2.0)
    parser.add_argument("--poll-interval", type=float, default=0.3)
    parser.add_argument("--health-interval", type=float, default=0.3,
                        help="how often the supervisor checks child liveness and writes status")
    parser.add_argument("--restart-backoff-seconds", type=float, default=0.2)
    parser.add_argument("--status-dir", required=True,
                        help="directory for supervisor.status.json + one worker-<i>.status.json each")
    parser.add_argument("--cwd", default=None,
                        help="working directory for spawned worker children (default: inherit)")
    args = parser.parse_args(argv)
    if bool(args.db) == bool(args.http):
        parser.error("exactly one of --db or --http is required")
    if args.workers < 1:
        parser.error("--workers must be at least 1")

    supervisor = build_supervisor(args)

    def _handle_signal(*_a: Any) -> None:
        supervisor.request_stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    try:
        signal.signal(signal.SIGINT, _handle_signal)
    except (ValueError, OSError):  # pragma: no cover
        pass
    # Windows has no SIGTERM delivery to a process it didn't create with a console; a caller
    # there uses CTRL_BREAK_EVENT (which requires CREATE_NEW_PROCESS_GROUP at spawn time) --
    # that arrives as SIGBREAK, so register it too when available.
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signal.signal(sigbreak, _handle_signal)

    supervisor.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
