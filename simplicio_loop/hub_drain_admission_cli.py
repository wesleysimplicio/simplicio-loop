"""CLI client for admitting a final #627 checkpoint as a held Hub job."""

from __future__ import annotations

import argparse
import json
from typing import Optional, Sequence

from .github_drain_admission import (
    DrainAdmissionProjectionError,
    build_admission_request,
    load_and_project_checkpoint,
)
from .github_drain_intake import FAILED_EXIT, INVALID_REQUEST_EXIT, PLANNED_NOT_EXECUTED_EXIT
from .hub_daemon import HubError, HubSocketClient, default_endpoint, default_transport


CLI_SCHEMA = "simplicio.hub-drain-admit-cli/v1"


def _emit(value: dict, exit_code: int) -> int:
    output = dict(value)
    output["exit_code"] = int(exit_code)
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="simplicio-loop hub-drain-admit",
        description="Admit a final #627 checkpoint as held; this never dispatches or executes it.",
    )
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--endpoint", default=default_endpoint())
    parser.add_argument("--transport", choices=("unix", "named-pipe"), default=default_transport())
    parser.add_argument("--client-id", default="hub-drain-cli")
    parser.add_argument("--workspace-id", default="default")
    parser.add_argument("--weight", type=int, default=1)
    parser.add_argument("--cost", type=int, default=1)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        job = load_and_project_checkpoint(args.checkpoint)
        request = build_admission_request(
            job, client_id=args.client_id, workspace_id=args.workspace_id,
            weight=args.weight, cost=args.cost,
        )
    except DrainAdmissionProjectionError as exc:
        return _emit({
            "schema": CLI_SCHEMA, "status": "BLOCKED", "reason_code": exc.reason_code,
            "dispatchable": False, "execution_authorized": False,
        }, INVALID_REQUEST_EXIT)
    try:
        response = HubSocketClient(
            args.endpoint, transport=args.transport,
        ).request("hub-drain-admit-" + job["run_id"], "hub_admit", **request)
    except (OSError, HubError, ValueError, json.JSONDecodeError):
        return _emit({
            "schema": CLI_SCHEMA, "status": "FAILED", "reason_code": "hub_unavailable",
            "dispatchable": False, "execution_authorized": False,
        }, FAILED_EXIT)
    if not response.get("ok") or not isinstance(response.get("admission"), dict):
        return _emit({
            "schema": CLI_SCHEMA, "status": "BLOCKED", "reason_code": "hub_admission_rejected",
            "dispatchable": False, "execution_authorized": False,
        }, FAILED_EXIT)
    receipt = response["admission"]
    return _emit({
        "schema": CLI_SCHEMA,
        "status": "ADMITTED_NOT_DISPATCHED",
        "reason_code": "execution_not_authorized",
        "dispatchable": False,
        "activation_required": True,
        "execution_authorized": False,
        "receipt": receipt,
    }, PLANNED_NOT_EXECUTED_EXIT)


if __name__ == "__main__":
    raise SystemExit(main())
