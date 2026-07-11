#!/usr/bin/env python3
"""Run the stdlib HTTP facade for a simplicio.queue/v1 SQLite backend."""
from __future__ import annotations

import argparse
import os
import signal

from simplicio_loop.remote_queue import SQLiteRemoteQueue, create_http_queue_server


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=".orchestrator/shared-queue.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default=os.environ.get("SIMPLICIO_QUEUE_TOKEN"))
    args = parser.parse_args()
    if not args.token:
        parser.error("--token or SIMPLICIO_QUEUE_TOKEN is required")
    server = create_http_queue_server(SQLiteRemoteQueue(args.db), args.host, args.port, token=args.token)
    signal.signal(signal.SIGTERM, lambda *_: server.shutdown())
    print("simplicio queue listening on %s:%d" % (args.host, server.server_port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
