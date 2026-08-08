"""Runtime-backed versus standalone semantic convergence parity protocol."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .execution_report import consolidate, new_report, record_task
from .semantic_convergence import ConvergenceController

FIXTURE_SCHEMA = "simplicio.convergence-parity-fixture/v1"
RECEIPT_SCHEMA = "simplicio.convergence-parity/v1"
RUNTIME_DECISION_SCHEMA = "simplicio.loop-policy-decision/v1"
_STANDALONE_RUNTIME_STATES = frozenset(
    {"incompatible", "not_configured", "unavailable"}
)


class FixtureError(ValueError):
    """Raised when a parity fixture cannot express comparable semantics."""


def _public_report(report: dict[str, Any]) -> dict[str, Any]:
    report["consolidated"] = consolidate(report)
    report["wall_ms"] = report["consolidated"]["wall_ms_run"]
    if "wall_ms" not in report["measured_fields"]:
        report["measured_fields"].append("wall_ms")
    return {key: value for key, value in report.items() if not key.startswith("_")}


def _execution_report(
    fixture: Mapping[str, Any],
    repo: Path,
    *,
    profile: str,
    outcome: str,
    loop_decision: Mapping[str, Any] | None,
) -> dict[str, Any]:
    provenance = fixture.get("provenance")
    if not isinstance(provenance, Mapping) or not provenance:
        raise FixtureError("fixture provenance must be a non-empty mapping")
    task = fixture.get("task")
    if not isinstance(task, Mapping):
        raise FixtureError("fixture task must be a mapping")
    task_id = str(task.get("task_id") or "").strip()
    title = str(task.get("title") or "").strip()
    if not task_id or not title:
        raise FixtureError("fixture task_id and title are required")
    report = new_report(
        repo,
        execution_profile=profile,
        loop_decision=dict(loop_decision) if loop_decision is not None else None,
        provenance=dict(provenance),
    )
    record_task(
        report,
        task_id=task_id,
        title=title,
        issue=str(task.get("issue") or "") or None,
        outcome=outcome,
        operators=["semantic-convergence", "simplicio-runtime"]
        if profile == "runtime-backed"
        else ["semantic-convergence"],
    )
    report["status"] = outcome
    return _public_report(report)


def _runtime_unsupported_reason(decision: Any) -> tuple[str, str] | None:
    if not isinstance(decision, Mapping):
        return "runtime_decision_absent", "Runtime activation decision is absent"
    if decision.get("schema") != RUNTIME_DECISION_SCHEMA:
        return (
            "runtime_decision_incompatible",
            "Runtime activation decision schema is incompatible",
        )
    if (
        decision.get("authority") != "runtime"
        or decision.get("host_may_override") is not False
    ):
        return (
            "runtime_authority_invalid",
            "Runtime decision is not authoritative and non-overridable",
        )
    if not str(decision.get("task_fingerprint") or "").strip():
        return "runtime_provenance_absent", "Runtime task fingerprint is absent"
    if decision.get("use_loop") is not True:
        return (
            "runtime_loop_not_activated",
            "Runtime did not activate the loop for this task",
        )
    return None


def _standalone_unsupported_reason(observation: Any) -> tuple[str, str] | None:
    if not isinstance(observation, Mapping):
        return (
            "standalone_runtime_observation_absent",
            "Standalone Runtime observation is absent",
        )
    if observation.get("status") not in _STANDALONE_RUNTIME_STATES:
        return (
            "standalone_runtime_status_invalid",
            "Standalone mode must explicitly record Runtime absence or incompatibility",
        )
    if not str(observation.get("reason_code") or "").strip():
        return (
            "standalone_runtime_reason_absent",
            "Standalone Runtime absence reason is missing",
        )
    if observation.get("effects_attempted") is not False:
        return (
            "standalone_effect_boundary_unsafe",
            "Standalone mode must prove that Runtime effects were not attempted",
        )
    return None


def _unsupported_path(
    fixture: Mapping[str, Any],
    repo: Path,
    *,
    path: str,
    reason: tuple[str, str],
    decision: Mapping[str, Any] | None = None,
    observation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    profile = "runtime-backed" if path == "runtime_backed" else "operator-standalone"
    return {
        "schema": "simplicio.convergence-path-receipt/v1",
        "path": path,
        "status": "UNSUPPORTED",
        "reason_code": reason[0],
        "detail": reason[1],
        "effects_attempted": False,
        "runtime_decision": dict(decision) if decision is not None else None,
        "runtime_observation": dict(observation) if observation is not None else None,
        "semantic_receipts": [],
        "acceptance_evidence": [],
        "execution_report": _execution_report(
            fixture,
            repo,
            profile=profile,
            outcome="UNSUPPORTED",
            loop_decision=decision,
        ),
    }


def _run_path(
    fixture: Mapping[str, Any],
    repo: Path,
    *,
    path: str,
    decision: Mapping[str, Any] | None = None,
    observation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    signals = fixture.get("signals")
    loop_evidence = fixture.get("loop_evidence")
    if (
        not isinstance(signals, Sequence)
        or isinstance(signals, (str, bytes))
        or not signals
    ):
        raise FixtureError("fixture signals must be a non-empty sequence")
    if not isinstance(loop_evidence, Sequence) or isinstance(
        loop_evidence, (str, bytes)
    ):
        raise FixtureError("fixture loop_evidence must be a sequence")
    if len(signals) != len(loop_evidence):
        raise FixtureError("signals and loop_evidence must have the same length")

    controller = ConvergenceController()
    semantic_receipts: list[dict[str, Any]] = []
    acceptance_evidence: list[dict[str, Any]] = []
    for signal, evidence in zip(signals, loop_evidence):
        if not isinstance(signal, Mapping) or not isinstance(evidence, Mapping):
            raise FixtureError("signals and loop_evidence entries must be mappings")
        receipt = controller.step(signal, evidence)
        semantic_receipts.append(receipt)
        acceptance_evidence.append(
            {
                "signal_id": str(signal.get("signal_id") or ""),
                "evidence_id": receipt["evidence_id"],
                "evidence_hash": receipt["evidence_hash"],
                "acceptance_verified": bool(evidence.get("acceptance_verified")),
                "delivery_verified": bool(evidence.get("delivery_verified")),
            }
        )

    status = "VERIFIED" if semantic_receipts[-1]["state"] == "VERIFIED" else "FAILED"
    profile = "runtime-backed" if path == "runtime_backed" else "operator-standalone"
    return {
        "schema": "simplicio.convergence-path-receipt/v1",
        "path": path,
        "status": status,
        "reason_code": None,
        "detail": None,
        "effects_attempted": False,
        "runtime_decision": dict(decision) if decision is not None else None,
        "runtime_observation": dict(observation) if observation is not None else None,
        "semantic_receipts": semantic_receipts,
        "acceptance_evidence": acceptance_evidence,
        "execution_report": _execution_report(
            fixture,
            repo,
            profile=profile,
            outcome=status,
            loop_decision=decision,
        ),
    }


def evaluate_fixture(
    fixture: Mapping[str, Any],
    *,
    repo: Path,
    runtime_decision: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute one fixture through both paths and return an evidence-bearing receipt."""
    if fixture.get("schema") != FIXTURE_SCHEMA:
        raise FixtureError(f"fixture schema must be {FIXTURE_SCHEMA}")
    fixture_id = str(fixture.get("fixture_id") or "").strip()
    if not fixture_id:
        raise FixtureError("fixture_id is required")
    repo = Path(repo).resolve()

    decision = (
        runtime_decision
        if runtime_decision is not None
        else fixture.get("runtime_decision")
    )
    runtime_reason = _runtime_unsupported_reason(decision)
    if runtime_reason is None:
        runtime_path = _run_path(
            fixture, repo, path="runtime_backed", decision=decision
        )
    else:
        runtime_path = _unsupported_path(
            fixture,
            repo,
            path="runtime_backed",
            reason=runtime_reason,
            decision=decision if isinstance(decision, Mapping) else None,
        )

    observation = fixture.get("standalone_runtime")
    standalone_reason = _standalone_unsupported_reason(observation)
    if standalone_reason is None:
        standalone_path = _run_path(
            fixture,
            repo,
            path="standalone",
            observation=observation,
        )
    else:
        standalone_path = _unsupported_path(
            fixture,
            repo,
            path="standalone",
            reason=standalone_reason,
            observation=observation if isinstance(observation, Mapping) else None,
        )

    receipts_equal = (
        runtime_path["semantic_receipts"] == standalone_path["semantic_receipts"]
    )
    evidence_equal = (
        runtime_path["acceptance_evidence"] == standalone_path["acceptance_evidence"]
    )
    supported = (
        runtime_path["status"] != "UNSUPPORTED"
        and standalone_path["status"] != "UNSUPPORTED"
    )
    parity = supported and receipts_equal and evidence_equal
    if not supported:
        status = "UNSUPPORTED"
    elif parity and runtime_path["status"] == standalone_path["status"] == "VERIFIED":
        status = "VERIFIED"
    else:
        status = "FAILED"
    unsupported = [
        {
            "path": path["path"],
            "reason_code": path["reason_code"],
            "detail": path["detail"],
        }
        for path in (runtime_path, standalone_path)
        if path["status"] == "UNSUPPORTED"
    ]
    return {
        "schema": RECEIPT_SCHEMA,
        "fixture_schema": FIXTURE_SCHEMA,
        "fixture_id": fixture_id,
        "provenance": dict(fixture["provenance"]),
        "status": status,
        "parity": parity,
        "comparison": {
            "semantic_receipts_equal": receipts_equal if supported else None,
            "acceptance_evidence_equal": evidence_equal if supported else None,
        },
        "unsupported_environments": unsupported,
        "runtime_backed": runtime_path,
        "standalone": standalone_path,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m simplicio_loop.convergence_parity",
        description="Execute a convergence fixture through Runtime-backed and standalone paths.",
    )
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--runtime-decision", type=Path)
    args = parser.parse_args(argv)
    try:
        fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
        decision = None
        if args.runtime_decision is not None:
            decision = json.loads(args.runtime_decision.read_text(encoding="utf-8"))
        receipt = evaluate_fixture(fixture, repo=args.repo, runtime_decision=decision)
    except (FixtureError, json.JSONDecodeError, OSError) as exc:
        print(
            json.dumps(
                {"schema": RECEIPT_SCHEMA, "status": "INVALID", "error": str(exc)},
                indent=2,
            )
        )
        return 2
    print(json.dumps(receipt, indent=2))
    return (
        0
        if receipt["status"] == "VERIFIED"
        else 2
        if receipt["status"] == "UNSUPPORTED"
        else 1
    )


__all__ = [
    "FIXTURE_SCHEMA",
    "RECEIPT_SCHEMA",
    "RUNTIME_DECISION_SCHEMA",
    "FixtureError",
    "evaluate_fixture",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
