"""Single installed-stack manifest for Mapper, Fast, Dev CLI and Loop."""

from __future__ import annotations

import argparse
import importlib.metadata as metadata
import json
import sys
from typing import Any

STACK_SCHEMA = "simplicio.loop-stack/v1"
COMPONENTS = (
    ("simplicio-mapper", "understand", "0.26.18"),
    ("simplicio-fast", "search", "2.0.27"),
    ("simplicio-cli", "change,verify", "0.18.9"),
    ("simplicio-loop", "run", "3.42.0"),
)


def _installed_version(distribution: str) -> str | None:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def stack_manifest() -> dict[str, Any]:
    components = []
    drifted = []
    for distribution, role, expected in COMPONENTS:
        installed = _installed_version(distribution)
        status = "ok" if installed == expected else "missing-or-drifted"
        components.append({"distribution": distribution, "role": role, "expected": expected, "installed": installed, "status": status})
        if status != "ok":
            drifted.append(distribution)
    return {"schema": STACK_SCHEMA, "version": 1, "package": "simplicio-loop", "components": components, "healthy": not drifted, "missing_or_drifted": drifted}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect the installed Simplicio Loop stack")
    parser.add_argument("--json", action="store_true", help="emit the machine-readable manifest")
    parser.add_argument("--check", action="store_true", help="exit non-zero when the stack drifts")
    args = parser.parse_args(argv)
    document = stack_manifest()
    if args.json or args.check:
        print(json.dumps(document, ensure_ascii=False, sort_keys=True))
    else:
        print("simplicio-loop stack: " + ("healthy" if document["healthy"] else "drifted"))
        for component in document["components"]:
            print(f"- {component['distribution']}: {component['installed'] or 'missing'} ({component['role']})")
    return 0 if document["healthy"] or not args.check else 1


if __name__ == "__main__":
    sys.exit(main())

