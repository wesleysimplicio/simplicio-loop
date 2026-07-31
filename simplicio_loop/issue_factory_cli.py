"""Source-aware issue-factory facade for the Loop CLI."""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path


def _azure_main(argv: list[str]) -> int:
    script = Path(__file__).resolve().parent.parent / "scripts" / "az_boards_adapter.py"
    spec = importlib.util.spec_from_file_location("simplicio_azure_adapter", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("Azure adapter is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.VERBS["list_ready"](module._parse(argv))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="issue-factory")
    sub = parser.add_subparsers(dest="command", required=True)
    discover = sub.add_parser("discover", help="discover ready work-items from a source adapter")
    discover.add_argument("--source", required=True, choices=("azure",))
    discover.add_argument("--state", default="New,Active")
    discover.add_argument("--area", default="")
    discover.add_argument("--org", default="")
    discover.add_argument("--project", default="")
    discover.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "discover" and args.source == "azure":
        forwarded = ["--state", args.state]
        if args.area:
            forwarded += ["--area", args.area]
        if args.org:
            forwarded += ["--org", args.org]
        if args.project:
            forwarded += ["--project", args.project]
        if args.dry_run:
            forwarded.append("--dry-run")
        return _azure_main(forwarded)
    parser.error("unsupported source")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
