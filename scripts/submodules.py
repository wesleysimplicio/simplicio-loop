#!/usr/bin/env python3
"""Reproducible simplicio-loop component checkout.

The superproject is the source of truth. This helper deliberately never uses
``git submodule update --remote`` and never follows a branch during a run. A
checkout is materialised only at the gitlink SHA recorded by the superproject
and ``components/submodules.json``.
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
GITMODULES = REPO / ".gitmodules"
PIN_MANIFEST = REPO / "components" / "submodules.json"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SCHEMA = "simplicio.loop-submodules/v1"


class SubmoduleError(RuntimeError):
    """An actionable, user-facing component checkout error."""


def _run(args: list[str], *, check: bool = True, cwd: Path = REPO) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args, cwd=str(cwd), text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False,
        )
    except FileNotFoundError as exc:
        raise SubmoduleError("git is required for submodule operations") from exc
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise SubmoduleError(f"command failed ({result.returncode}): {' '.join(args)}\n{detail}")
    return result


def load_pins(path: Path = PIN_MANIFEST) -> dict[str, dict[str, str]]:
    """Load and validate the committed pins before any mutation."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SubmoduleError(f"pin manifest missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SubmoduleError(f"pin manifest is not valid JSON: {path}: {exc}") from exc
    if document.get("schema") != SCHEMA:
        raise SubmoduleError(f"unsupported pin manifest schema: {document.get('schema')!r}")
    if document.get("policy", {}).get("floating_updates") is not False:
        raise SubmoduleError("pin manifest must set policy.floating_updates=false")
    components = document.get("components")
    expected = {"simplicio-mapper", "simplicio-dev-cli", "simplicio-fast"}
    if not isinstance(components, dict) or set(components) != expected:
        raise SubmoduleError("pin manifest must contain exactly mapper, dev-cli and fast")
    result: dict[str, dict[str, str]] = {}
    paths: set[str] = set()
    for name, item in components.items():
        required = ("path", "url", "ref", "sha")
        if not isinstance(item, dict) or any(not isinstance(item.get(key), str) for key in required):
            raise SubmoduleError(f"component {name} must define string path/url/ref/sha")
        sha = item["sha"].lower()
        if not SHA_RE.fullmatch(sha):
            raise SubmoduleError(f"component {name} has invalid commit SHA: {sha!r}")
        if item["path"] in paths:
            raise SubmoduleError(f"duplicate submodule path: {item['path']}")
        paths.add(item["path"])
        result[name] = {key: item[key] for key in required}
        result[name]["sha"] = sha
    return result


def load_gitmodules(path: Path = GITMODULES) -> dict[str, dict[str, str]]:
    """Parse .gitmodules without trusting arbitrary keys or command output."""
    if not path.is_file():
        raise SubmoduleError(f".gitmodules missing: {path}")
    parser = configparser.ConfigParser()
    parser.optionxform = str
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error as exc:
        raise SubmoduleError(f"invalid .gitmodules: {exc}") from exc
    result: dict[str, dict[str, str]] = {}
    for section in parser.sections():
        prefix = 'submodule "'
        if not section.startswith(prefix) or not section.endswith('"'):
            raise SubmoduleError(f"unsupported .gitmodules section: {section}")
        name = section[len(prefix):-1]
        values = {key: value.strip() for key, value in parser.items(section)}
        if set(values) != {"path", "url"}:
            raise SubmoduleError(f"{section} must contain exactly path and url")
        path_value = Path(values["path"])
        if not values["path"] or path_value.is_absolute() or ".." in path_value.parts:
            raise SubmoduleError(f"unsafe submodule path for {name}: {values['path']!r}")
        if not values["url"]:
            raise SubmoduleError(f"empty submodule URL for {name}")
        result[name] = values
    return result


def _gitlink_shas() -> dict[str, str]:
    result = _run(["git", "ls-tree", "-r", "HEAD"]).stdout
    links: dict[str, str] = {}
    for line in result.splitlines():
        match = re.match(r"^160000 commit ([0-9a-f]{40})\t(.+)$", line)
        if match:
            links[match.group(2)] = match.group(1)
    return links


def expected_components() -> dict[str, dict[str, str]]:
    pins = load_pins()
    modules = load_gitmodules()
    for name, item in pins.items():
        # Current .gitmodules names are paths; historical names are accepted
        # only as a migration aid, while path and URL remain exact.
        module = modules.get(item["path"]) or modules.get(name)
        if module is None:
            raise SubmoduleError(f"{name} missing from .gitmodules")
        if module["path"] != item["path"] or module["url"] != item["url"]:
            raise SubmoduleError(f".gitmodules drift for {name}: expected {item['path']} / {item['url']}")
    return pins


def _path_status(path: Path, expected_sha: str) -> dict[str, Any]:
    if not path.exists():
        return {"state": "missing", "expected_sha": expected_sha, "observed_sha": None}
    if not path.is_dir():
        return {"state": "invalid_path", "expected_sha": expected_sha, "observed_sha": None}
    result = _run(["git", "rev-parse", "HEAD"], cwd=path, check=False)
    observed = result.stdout.strip().lower() if result.returncode == 0 else None
    if not observed:
        return {"state": "not_repository", "expected_sha": expected_sha, "observed_sha": None}
    state = "ok" if observed == expected_sha else "diverged"
    dirty = _run(["git", "status", "--porcelain"], cwd=path, check=False).stdout.strip()
    if dirty and state == "ok":
        state = "dirty"
    return {"state": state, "expected_sha": expected_sha, "observed_sha": observed, "dirty": bool(dirty)}


def inspect() -> dict[str, Any]:
    pins = expected_components()
    gitlinks = _gitlink_shas()
    components: dict[str, Any] = {}
    for name, item in pins.items():
        committed = gitlinks.get(item["path"])
        committed_state = "ok" if committed == item["sha"] else ("missing" if committed is None else "diverged")
        components[name] = {
            "path": item["path"], "url": item["url"], "ref": item["ref"],
            "expected_sha": item["sha"], "committed_sha": committed,
            "committed_state": committed_state,
            **_path_status(REPO / item["path"], item["sha"]),
        }
    ok = all(value["committed_state"] == "ok" and value["state"] == "ok" for value in components.values())
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "superproject": str(REPO), "floating_updates": False, "ok": ok,
        "components": components,
    }


def verify() -> dict[str, Any]:
    report = inspect()
    failures = []
    for name, value in report["components"].items():
        if value["committed_state"] != "ok":
            failures.append(f"{name}: superproject gitlink is {value['committed_state']} (expected {value['expected_sha']})")
        if value["state"] != "ok":
            observed = value.get("observed_sha") or "none"
            failures.append(f"{name}: checkout is {value['state']} (observed {observed}, expected {value['expected_sha']})")
    report["failures"] = failures
    if failures:
        raise SubmoduleError("submodule verification failed:\n- " + "\n- ".join(failures))
    return report


def update(*, offline: bool = False) -> dict[str, Any]:
    """Materialise exactly the superproject gitlinks; never follow branches."""
    expected_components()
    _run(["git", "submodule", "sync", "--recursive"])
    update_args = ["git", "submodule", "update", "--init", "--recursive", "--checkout"]
    if offline:
        update_args.append("--no-fetch")
    _run(update_args)
    return verify()


def write_run_manifest(output: Path) -> dict[str, Any]:
    report = verify()
    document = {
        "schema": "simplicio.loop-run-components/v1",
        "generated_at": report["generated_at"], "floating_updates": False,
        "components": {
            name: {"path": item["path"], "url": item["url"],
                   "sha": item["expected_sha"], "observed_sha": item["observed_sha"]}
            for name, item in report["components"].items()
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(document, indent=2, sort_keys=True) + "\n"
    output.write_text(encoded, encoding="utf-8")
    document["sha256"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return document


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("bootstrap", "update"):
        command = sub.add_parser(name, help="materialise pinned gitlinks (never floating)")
        command.add_argument("--offline", action="store_true", help="do not fetch from a remote")
    sub.add_parser("status", help="print JSON status without changing checkout")
    sub.add_parser("verify", help="fail if any gitlink or checkout diverges from the pins")
    manifest = sub.add_parser("manifest", help="write a run manifest after verification")
    manifest.add_argument("--output", type=Path, default=REPO / ".simplicio" / "submodules-run.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command in {"bootstrap", "update"}:
            report = update(offline=args.offline)
        elif args.command == "status":
            report = inspect()
        elif args.command == "verify":
            report = verify()
        else:
            report = write_run_manifest(args.output)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except SubmoduleError as exc:
        print(f"SUBMODULE_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
