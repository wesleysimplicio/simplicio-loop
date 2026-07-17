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
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from simplicio_loop import stage_agents as sa  # noqa: E402


def main() -> int:
    input_path, output_path, receipt_path = sys.argv[1], sys.argv[2], sys.argv[3]
    stage_context = json.loads(open(input_path, encoding="utf-8").read())

    output = {
        "summary": "echo-agent fixture completed",
        "role_id": stage_context["role_id"],
        "stage_id": stage_context["stage_id"],
    }
    receipt = sa.make_stage_receipt(
        receipt_id=f"receipt-{stage_context['attempt_id']}",
        agent_instance_id=stage_context["agent_instance_id"],
        role_id=stage_context["role_id"],
        stage_id=stage_context["stage_id"],
        run_id=stage_context["run_id"],
        task_id=stage_context["task_id"],
        attempt_id=stage_context["attempt_id"],
        attempt_ordinal=stage_context.get("attempt_ordinal", 1),
        fence=stage_context["fence"],
        plan_revision=stage_context["plan_revision"],
        context_hash=stage_context["context_hash"],
        manifest_hash=stage_context["manifest_hash"],
        verdict="pass",
        evidence_refs=["echo-agent-fixture"],
        reason_code="echo_agent_fixture_completed",
        input_payload=stage_context,
        output_payload=output,
        next_stage_recommendation="proceed",
    )
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(output, fh)
    with open(receipt_path, "w", encoding="utf-8") as fh:
        json.dump(receipt, fh)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
