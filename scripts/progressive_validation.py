#!/usr/bin/env python3
"""Execute a progressive validation request and persist its receipt."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from simplicio_loop.progressive_validation import ProgressiveValidator, request_from_dict


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    request = request_from_dict(json.loads(args.plan.read_text(encoding="utf-8")))
    receipt = ProgressiveValidator(args.cache).run(request)
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(
        json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "status": receipt["status"],
        "required_level": receipt["required_level"],
        "receipt": str(args.receipt),
        "receipt_hash": receipt["receipt_hash"],
    }, sort_keys=True))
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
