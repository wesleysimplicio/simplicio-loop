#!/usr/bin/env python3
"""Run the stdlib HTTP facade for a simplicio.queue/v1 SQLite backend."""
from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

# The script lives in scripts/, but it imports the top-level ``simplicio_loop`` package.
# When executed as ``python3 scripts/remote_queue_server.py`` Python adds the script's own
# directory (scripts/) to sys.path[0], not the repo root, so the import below fails with a
# bare ModuleNotFoundError. Anchoring the repo root on sys.path here keeps the CLI invokable
# from any working directory and lets genuine argparse/ValueError gates (e.g. the TLS-pair
# requirement) surface with their intended exit codes instead of being masked by an import
# error.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from simplicio_loop.remote_queue import SQLiteRemoteQueue, create_http_queue_server, tls_context_from_files  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=".orchestrator/shared-queue.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default=os.environ.get("SIMPLICIO_QUEUE_TOKEN"))
    parser.add_argument("--tls-certfile", default=os.environ.get("SIMPLICIO_QUEUE_TLS_CERTFILE"))
    parser.add_argument("--tls-keyfile", default=os.environ.get("SIMPLICIO_QUEUE_TLS_KEYFILE"))
    args = parser.parse_args()
    if not args.token:
        parser.error("--token or SIMPLICIO_QUEUE_TOKEN is required")
    if bool(args.tls_certfile) != bool(args.tls_keyfile):
        parser.error("--tls-certfile and --tls-keyfile must be provided together")
    context = (tls_context_from_files(args.tls_certfile, args.tls_keyfile)
               if args.tls_certfile else None)
    try:
        server = create_http_queue_server(SQLiteRemoteQueue(args.db), args.host, args.port,
                                          token=args.token, ssl_context=context)
    except ValueError as exc:
        parser.error(str(exc))
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
