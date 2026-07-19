"""Packaged CLI surface for the #512/#513 map service standalone/Hub-backed slice.

Wires `status` / `verify` / `gc` to a real running HubDaemon over its actual socket
transport when `--hub-socket` is given (mirroring process_enforcement_cli.py's
pattern); with no socket, or an unreachable one, reports that honestly rather than
fabricating data - there is no standalone in-process map state to fall back to here
(unlike process_enforcement's on-disk registry), since map state is intentionally
Hub-only and in-memory (see the restart test in test_hub_daemon_map_ipc.py).
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, Optional


def _print(payload: Dict[str, Any]) -> int:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if payload.get("reachable", True) else 1


def _client(hub_socket: str):
    from .hub_daemon import HubSocketClient, default_transport
    return HubSocketClient(hub_socket, transport=default_transport())


def cmd_status(args: argparse.Namespace) -> int:
    from .hub_daemon import HubError
    try:
        response = _client(args.hub_socket).request("map-cli-status", "map_status")
    except (HubError, OSError, ConnectionError) as exc:
        return _print({"schema": "simplicio.map-cli-status/v1", "reachable": False, "error": str(exc)})
    return _print({"schema": "simplicio.map-cli-status/v1", "reachable": True, "status": response.get("status")})


def cmd_verify(args: argparse.Namespace) -> int:
    """Real-diagnosis check: reachable Hub + watcher quotas within bounds."""
    from .hub_daemon import HubError
    try:
        response = _client(args.hub_socket).request("map-cli-verify", "map_status")
    except (HubError, OSError, ConnectionError) as exc:
        return _print({"schema": "simplicio.map-cli-verify/v1", "reachable": False, "healthy": False, "error": str(exc)})
    status = response.get("status") or {}
    healthy = status.get("watchers", 0) <= status.get("max_watchers", 0) and \
        status.get("pending", 0) <= status.get("max_pending", 0)
    return _print({
        "schema": "simplicio.map-cli-verify/v1", "reachable": True, "healthy": healthy, "status": status,
    })


def cmd_gc(args: argparse.Namespace) -> int:
    from .hub_daemon import HubError
    try:
        response = _client(args.hub_socket).request("map-cli-gc", "map_gc")
    except (HubError, OSError, ConnectionError) as exc:
        return _print({"schema": "simplicio.map-cli-gc/v1", "reachable": False, "error": str(exc)})
    return _print({"schema": "simplicio.map-cli-gc/v1", "reachable": True, "removed": response.get("removed", [])})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hub-socket", required=True,
        help="path to a running HubDaemon's socket endpoint - map state is Hub-only "
             "and in-memory (#512/#513), there is no standalone fallback to report on",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="real watcher/registry status from a running Hub")
    sub.add_parser("verify", help="reachable + watcher quotas within bounds")
    sub.add_parser("gc", help="reclaim invalidated, unreferenced views")
    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {"status": cmd_status, "verify": cmd_verify, "gc": cmd_gc}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
