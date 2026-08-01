"""JSON operator commands for persisted generation-broker attempts."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .generation_broker import GenerationBinding, _digest, _write_json


def _bindings(attempt: Path) -> list[Path]:
    return sorted((attempt / "overlays").glob("*/generation-binding.json"))


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="simplicio-loop generation-broker")
    parser.add_argument("action", choices=("inspect", "pin", "release", "reconcile", "status", "doctor"))
    parser.add_argument("--attempt-dir", required=True)
    parser.add_argument("--candidate-id")
    parser.add_argument("--expires-ns", type=int)
    args = parser.parse_args(argv)
    attempt = Path(args.attempt_dir).resolve()
    paths = _bindings(attempt)
    corrupt: list[str] = []
    values: dict[str, GenerationBinding] = {}
    for path in paths:
        try:
            binding = GenerationBinding.verify(json.loads(path.read_text(encoding="utf-8")))
            values[binding.candidate_id] = binding
        except (OSError, ValueError, json.JSONDecodeError):
            corrupt.append(path.parent.name)
    if args.action in {"inspect", "pin", "release"}:
        if not args.candidate_id or args.candidate_id not in values:
            parser.error("action requires an existing --candidate-id")
        binding = values[args.candidate_id]
        if args.action in {"pin", "release"}:
            expires = int(args.expires_ns or time.time_ns()) if args.action == "pin" else 0
            payload = binding.to_dict()
            payload.pop("receipt_hash")
            payload["lease_expires_ns"] = expires
            binding = GenerationBinding(**payload, receipt_hash=_digest(payload))
            _write_json(Path(binding.overlay_path) / "generation-binding.json", binding.to_dict())
        result = binding.to_dict()
    else:
        journal = attempt / "generation-gc-journal.json"
        orphaned = False
        if journal.exists():
            try:
                orphaned = json.loads(journal.read_text(encoding="utf-8")).get("state") == "PREPARED"
            except (OSError, json.JSONDecodeError):
                orphaned = True
        result = {
            "action": args.action,
            "bindings": sorted(values),
            "corrupt": corrupt,
            "orphaned_transaction": orphaned,
            "healthy": not corrupt and not orphaned,
        }
    print(json.dumps(result, sort_keys=True))
    return 0 if not corrupt else 2


if __name__ == "__main__":
    raise SystemExit(cli_main())
