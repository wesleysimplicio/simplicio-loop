"""Source-aware issue-factory facade for the Loop CLI."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path


def _azure_main(argv: list[str]) -> int:
    script = Path(__file__).resolve().parent.parent / "scripts" / "az_boards_adapter.py"
    opts = _parse(argv)
    if not script.exists():
        if not opts.get("dry-run"):
            print(json.dumps({"status": "BLOCKED", "reason": "azure_adapter_not_packaged"}))
            return 2
        states = [s.strip() for s in str(opts.get("state", "New,Active")).split(",") if s.strip()]
        where = " OR ".join("[System.State] = '%s'" % s.replace("'", "''") for s in states)
        wiql = ("SELECT [System.Id], [System.Title], [System.State], [System.WorkItemType], "
                "[System.Tags] FROM workitems WHERE (%s) ORDER BY [System.ChangedDate] DESC" % where)
        command = ["boards", "query", "--wiql", wiql, "--output", "json"]
        if opts.get("org"):
            command += ["--organization", str(opts["org"])]
        if opts.get("project"):
            command += ["--project", str(opts["project"])]
        print("az " + " ".join('"%s"' % a.replace('"', '\\"') if " " in a else a for a in command))
        return 0
    spec = importlib.util.spec_from_file_location("simplicio_azure_adapter", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("Azure adapter is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.VERBS["list_ready"](opts)
    return 0


def _parse(argv: list[str]) -> dict[str, str | bool]:
    opts: dict[str, str | bool] = {}
    i = 0
    while i < len(argv):
        token = argv[i]
        if token.startswith("--"):
            key = token[2:]
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                opts[key] = argv[i + 1]
                i += 2
            else:
                opts[key] = True
                i += 1
        else:
            i += 1
    return opts


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
