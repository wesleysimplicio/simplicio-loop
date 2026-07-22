#!/usr/bin/env python3
"""Conformance CLI for external quality providers (validate or migrate v1)."""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from simplicio_loop.quality_matrix_v2 import evaluate_v2, migrate_v1


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("validate", "migrate-v1"))
    parser.add_argument("receipt")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        value = json.loads(Path(args.receipt).read_text(encoding="utf-8"))
        result = evaluate_v2(value) if args.command == "validate" else migrate_v1(value)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ready": False, "reason_code": "quality_matrix_input_invalid", "error": str(exc)}))
        return 2
    rendered = json.dumps(result, sort_keys=True, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if args.command == "migrate-v1" or result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
