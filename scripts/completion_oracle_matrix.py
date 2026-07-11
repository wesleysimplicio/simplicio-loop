#!/usr/bin/env python3
"""Run the shared completion oracle against every supported runtime adapter."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from simplicio_loop.oracle import evaluate_completion

ADAPTERS = ("cursor", "claude", "codex", "vscode", "antigravity", "hermes")


def evaluate_adapter(adapter: str, loop_dir: str, run_dir: str, response_text: str = "", flow_gap: str = "") -> dict[str, Any]:
    """Evaluate one adapter through the same oracle implementation."""
    if adapter not in ADAPTERS:
        raise ValueError(f"unsupported adapter: {adapter}")
    result = evaluate_completion(loop_dir, run_dir, response_text=response_text, flow_gap=flow_gap)
    return {
        "adapter": adapter,
        "ready": bool(result.get("ready")),
        "verdict": result.get("verdict", "DELIVERY_PENDING"),
        "reason_code": result.get("reason_code", "oracle_incomplete"),
        "tag": result.get("tag", "UNVERIFIED"),
    }


def evaluate_matrix(loop_dir: str, run_dir: str, response_text: str = "", flow_gap: str = "") -> dict[str, Any]:
    rows = [evaluate_adapter(adapter, loop_dir, run_dir, response_text, flow_gap) for adapter in ADAPTERS]
    signatures = {(row["ready"], row["verdict"], row["reason_code"], row["tag"]) for row in rows}
    return {
        "schema": "simplicio.completion-oracle-matrix/v1",
        "adapters": rows,
        "parity": len(signatures) == 1,
        "signature": list(next(iter(signatures))) if len(signatures) == 1 else None,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="completion_oracle_matrix")
    parser.add_argument("--loop-dir", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--response-text", default="")
    parser.add_argument("--flow-gap", default="")
    args = parser.parse_args(argv)
    payload = evaluate_matrix(args.loop_dir, args.run_dir, args.response_text, args.flow_gap)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["parity"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
