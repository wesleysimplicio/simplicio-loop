#!/usr/bin/env python3
"""Trivial fixture agent for CommandAgentAdapter integration tests (issue #424).

Reads the stage-context JSON written by the coordinator, and writes back a
minimal stage_output + ``simplicio.stage-receipt/v1`` so the coordinator's
collect() has real files to read from a real (non-mocked) subprocess. Exits 0
on success.

Usage: echo_agent.py <input_path> <output_path> <receipt_path>
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    input_path, output_path, receipt_path = sys.argv[1], sys.argv[2], sys.argv[3]
    stage_context = json.loads(open(input_path, encoding="utf-8").read())

    output = {
        "summary": "echo-agent fixture completed",
        "role_id": stage_context["role_id"],
        "stage_id": stage_context["stage_id"],
    }
    receipt = {
        "schema": "simplicio.stage-receipt/v1",
        "receipt_id": f"receipt-{stage_context['attempt_id']}",
        "agent_instance_id": stage_context["agent_instance_id"],
        "role_id": stage_context["role_id"],
        "stage_id": stage_context["stage_id"],
        "run_id": stage_context["run_id"],
        "task_id": stage_context["task_id"],
        "attempt_id": stage_context["attempt_id"],
        "fence": stage_context["fence"],
        "plan_revision": stage_context["plan_revision"],
        "created_at": "2026-07-16T00:00:00Z",
        "verdict": "pass",
        "evidence_refs": ["echo-agent-fixture"],
        "accepted": True,
    }
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(output, fh)
    with open(receipt_path, "w", encoding="utf-8") as fh:
        json.dump(receipt, fh)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
