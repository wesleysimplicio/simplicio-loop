"""Standalone daemon process used by test_hub_transport.py integration tests."""

import signal
import sys
import time

from simplicio_loop.hub_daemon import HubAlreadyRunning, HubDaemon, HubSocketServer


def _handle_sigterm(_signum, _frame) -> None:
    raise SystemExit(0)


def main() -> int:
    lock_path, socket_path = sys.argv[1], sys.argv[2]
    signal.signal(signal.SIGTERM, _handle_sigterm)
    daemon = HubDaemon(lock_path)
    try:
        daemon.start()
    except HubAlreadyRunning:
        print("ALREADY_RUNNING", flush=True)
        return 1
    server = HubSocketServer(daemon, socket_path)
    server.start()
    print("READY", flush=True)
    try:
        while True:
            time.sleep(0.1)
    finally:
        server.shutdown()
        daemon.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
