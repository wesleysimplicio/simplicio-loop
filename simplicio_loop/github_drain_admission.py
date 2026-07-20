"""Pure fail-closed projection of a final #627 drain checkpoint into one held Hub job."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Tuple, Union

from .github_drain_intake import (
    INTAKE_SCHEMA,
    INTENT_SCHEMA,
    MAP_SCHEMA,
    PLAN_SCHEMA,
    PLANNED_NOT_EXECUTED_EXIT,
    PLANNER_REVISION,
    _digest,
    _integrity_payload,
    _planner_config,
    _run_digest,
    plan_issue_waves,
)


JOB_SCHEMA = "simplicio.github-drain-job/v1"
IDEMPOTENCY_PREFIX = "github-drain-admission/v1:"
MAX_ADMISSION_ID_LENGTH = 128
MAX_ADMISSION_WEIGHT_OR_COST = 1_000_000
MAX_JOB_TEXT_LENGTH = 4096

_JOB_KEYS = {
    "schema", "kind", "repository", "run_id", "run_digest", "request_digest",
    "checkpoint_digest", "source_digest", "plan_digest", "workspace_digest",
    "issue_count", "source", "canonical_map", "items", "external_dependencies",
    "plan", "dispatchable", "activation_required", "execution_authorized",
}
_ITEM_KEYS = {
    "number", "title", "url", "labels", "source_revision", "observed_at",
    "dependencies", "external_dependencies_closed", "risk", "state",
}
_CANONICAL_MAP_REQUIRED = {"schema", "status", "mode", "repository", "cache_key"}
_CANONICAL_MAP_ALLOWED = _CANONICAL_MAP_REQUIRED | {
    "tree_hash", "trace_id", "files", "source",
}
_REPOSITORY = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?"
)


class DrainAdmissionProjectionError(RuntimeError):
    def __init__(self, message: str, *, reason_code: str = "drain_admission_invalid") -> None:
        self.reason_code = reason_code
        super().__init__(message)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _is_digest(value: Any, length: int = 64) -> bool:
    return (
        isinstance(value, str) and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _safe_text(value: Any, *, maximum: int = MAX_JOB_TEXT_LENGTH, empty: bool = False) -> bool:
    return (
        isinstance(value, str) and (empty or bool(value)) and value == value.strip()
        and len(value) <= maximum
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _positive_sorted_ids(value: Any) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(item, int) and not isinstance(item, bool) and item >= 1 for item in value)
        and value == sorted(value) and len(value) == len(set(value))
    )


def _identifier(value: Any, *, maximum: int = 512) -> bool:
    return (
        _safe_text(value, maximum=maximum)
        and re.fullmatch(r"[A-Za-z0-9_.:@+=-]+", value) is not None
    )


def _utc_timestamp(value: Any) -> bool:
    try:
        return (
            isinstance(value, str) and len(value) == 20
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value) is not None
            and time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
            ) == value
        )
    except (TypeError, ValueError, OverflowError):
        return False


def _github_issue_url(value: Any, repository: str, number: int) -> bool:
    return (
        isinstance(value, str) and len(value) <= 2048
        and re.fullmatch(
            r"https://github\.com/%s/issues/%d" % (re.escape(repository), number),
            value,
            flags=re.IGNORECASE,
        ) is not None
    )


def validate_projected_job(job: Mapping[str, Any]) -> None:
    """Validate the complete durable job shape at every trust boundary without I/O."""
    if not isinstance(job, Mapping) or set(job) != _JOB_KEYS:
        raise DrainAdmissionProjectionError(
            "projected job fields are invalid", reason_code="job_projection_invalid"
        )
    repository = job.get("repository")
    digests = (
        job.get("run_digest"), job.get("request_digest"), job.get("checkpoint_digest"),
        job.get("source_digest"), job.get("plan_digest"), job.get("workspace_digest"),
    )
    issue_count = job.get("issue_count")
    if (
        job.get("schema") != JOB_SCHEMA or job.get("kind") != "github_drain_root"
        or not isinstance(repository, str) or len(repository) > 140
        or _REPOSITORY.fullmatch(repository) is None
        or not _is_digest(job.get("run_id"), 32) or any(not _is_digest(value) for value in digests)
        or not isinstance(issue_count, int) or isinstance(issue_count, bool) or issue_count < 1
        or job.get("dispatchable") is not False or job.get("activation_required") is not True
        or job.get("execution_authorized") is not False
    ):
        raise DrainAdmissionProjectionError(
            "projected job identity/flags are invalid", reason_code="job_projection_invalid"
        )

    source = job.get("source")
    if not isinstance(source, Mapping) or set(source) != {"observed_at", "open_issues", "digest"}:
        raise DrainAdmissionProjectionError(
            "projected source is invalid", reason_code="job_source_invalid"
        )
    open_issues = source.get("open_issues")
    if (
        not _utc_timestamp(source.get("observed_at"))
        or not _positive_sorted_ids(open_issues)
        or source.get("digest") != _digest(open_issues)
        or job.get("source_digest") != source.get("digest")
    ):
        raise DrainAdmissionProjectionError(
            "projected source digest is invalid", reason_code="job_source_invalid"
        )

    canonical = job.get("canonical_map")
    if (
        not isinstance(canonical, Mapping)
        or not _CANONICAL_MAP_REQUIRED <= set(canonical) <= _CANONICAL_MAP_ALLOWED
        or canonical.get("schema") != MAP_SCHEMA or canonical.get("status") != "ready"
        or canonical.get("mode") != "canonical"
        or str(canonical.get("repository") or "").lower() != repository.lower()
        or not _identifier(canonical.get("cache_key"), maximum=512)
        or ("tree_hash" in canonical and not _is_digest(canonical.get("tree_hash")))
        or ("trace_id" in canonical and not _identifier(canonical.get("trace_id"), maximum=256))
        or (
            "files" in canonical
            and (
                not isinstance(canonical.get("files"), int)
                or isinstance(canonical.get("files"), bool) or canonical.get("files") < 0
            )
        )
        or (
            "source" in canonical
            and (
                not _safe_text(canonical.get("source"), maximum=128)
                or re.fullmatch(r"[A-Za-z0-9_.:-]+", canonical.get("source")) is None
            )
        )
    ):
        raise DrainAdmissionProjectionError(
            "projected canonical map is invalid", reason_code="job_map_invalid"
        )

    items = job.get("items")
    if not isinstance(items, Mapping) or not items:
        raise DrainAdmissionProjectionError(
            "projected items are invalid", reason_code="job_items_invalid"
        )
    normalized: Dict[str, Dict[str, Any]] = {}
    for raw_key, raw_item in items.items():
        key = str(raw_key)
        if (
            not isinstance(raw_key, str) or not isinstance(raw_item, Mapping)
            or set(raw_item) != _ITEM_KEYS
        ):
            raise DrainAdmissionProjectionError(
                "projected item fields are invalid", reason_code="job_items_invalid"
            )
        number = raw_item.get("number")
        labels = raw_item.get("labels")
        dependencies = raw_item.get("dependencies")
        external_closed = raw_item.get("external_dependencies_closed")
        if (
            not isinstance(number, int) or isinstance(number, bool) or number < 1 or key != str(number)
            or not _safe_text(raw_item.get("title"), maximum=MAX_JOB_TEXT_LENGTH, empty=True)
            or not _github_issue_url(raw_item.get("url"), repository, number)
            or not isinstance(labels, list) or len(labels) > 100
            or any(not _safe_text(label, maximum=256, empty=True) for label in labels)
            or not _identifier(raw_item.get("source_revision"), maximum=512)
            or not _utc_timestamp(raw_item.get("observed_at"))
            or not _positive_sorted_ids(dependencies) or number in dependencies
            or not _positive_sorted_ids(external_closed)
            or any(dependency not in dependencies for dependency in external_closed)
            or raw_item.get("risk") not in {"high", "medium", "low"}
            or raw_item.get("state") not in {"planned", "remote_closed"}
        ):
            raise DrainAdmissionProjectionError(
                "projected item values are invalid", reason_code="job_items_invalid"
            )
        normalized[key] = dict(raw_item)

    external = job.get("external_dependencies")
    if not isinstance(external, Mapping):
        raise DrainAdmissionProjectionError(
            "projected external dependencies are invalid", reason_code="job_items_invalid"
        )
    for raw_key, evidence in external.items():
        key = str(raw_key)
        if (
            not isinstance(raw_key, str) or not key.isdigit()
            or key != str(int(key)) or int(key) < 1
            or not isinstance(evidence, Mapping)
            or set(evidence) != {"state", "source_revision", "observed_at"}
            or evidence.get("state") != "closed"
            or not _identifier(evidence.get("source_revision"), maximum=512)
            or not _utc_timestamp(evidence.get("observed_at"))
        ):
            raise DrainAdmissionProjectionError(
                "projected external dependency evidence is invalid",
                reason_code="job_items_invalid",
            )
    referenced_external = {
        str(dependency)
        for item in normalized.values()
        for dependency in item["external_dependencies_closed"]
    }
    if referenced_external != {str(key) for key in external}:
        raise DrainAdmissionProjectionError(
            "projected external dependency evidence is incomplete",
            reason_code="job_items_invalid",
        )
    planned = sorted(
        int(key) for key, item in normalized.items() if item.get("state") == "planned"
    )
    try:
        rebuilt = plan_issue_waves(normalized)
    except Exception as exc:
        raise DrainAdmissionProjectionError(
            "projected plan cannot be reconstructed", reason_code="job_plan_invalid"
        ) from exc
    if (
        planned != open_issues or job.get("plan") != rebuilt or rebuilt.get("schema") != PLAN_SCHEMA
        or rebuilt.get("issue_count") != issue_count or not rebuilt.get("waves")
        or job.get("plan_digest") != _digest(rebuilt)
    ):
        raise DrainAdmissionProjectionError(
            "projected plan/digests are invalid", reason_code="job_plan_invalid"
        )


def admission_input_digest(
    job: Mapping[str, Any], *, client_id: str, workspace_id: str, weight: int, cost: int
) -> str:
    return _sha256({
        "job": dict(job), "client_id": str(client_id), "workspace_id": str(workspace_id),
        "weight": int(weight), "cost": int(cost),
    })


def admission_idempotency_key(job: Mapping[str, Any]) -> str:
    run_digest = str(job.get("run_digest") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", run_digest):
        raise DrainAdmissionProjectionError(
            "job run digest is invalid", reason_code="job_identity_invalid"
        )
    return IDEMPOTENCY_PREFIX + run_digest


def validate_admission_metadata(
    *, client_id: Any, workspace_id: Any, weight: Any, cost: Any,
) -> None:
    identities = (client_id, workspace_id)
    if any(
        not isinstance(value, str) or not value or value != value.strip()
        or len(value) > MAX_ADMISSION_ID_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        for value in identities
    ):
        raise DrainAdmissionProjectionError(
            "client/workspace identity is invalid", reason_code="admission_metadata_invalid"
        )
    if any(
        not isinstance(value, int) or isinstance(value, bool)
        or value < 1 or value > MAX_ADMISSION_WEIGHT_OR_COST
        for value in (weight, cost)
    ):
        raise DrainAdmissionProjectionError(
            "weight/cost is invalid", reason_code="admission_metadata_invalid"
        )


def build_admission_request(
    job: Mapping[str, Any], *, client_id: str, workspace_id: str = "default",
    weight: int = 1, cost: int = 1,
) -> Dict[str, Any]:
    validate_admission_metadata(
        client_id=client_id, workspace_id=workspace_id, weight=weight, cost=cost,
    )
    projected = dict(job)
    return {
        "job": projected,
        "idempotency_key": admission_idempotency_key(projected),
        "input_digest": admission_input_digest(
            projected, client_id=client_id, workspace_id=workspace_id, weight=weight, cost=cost,
        ),
        "client_id": client_id,
        "workspace_id": workspace_id,
        "weight": int(weight),
        "cost": int(cost),
    }


def _identity(checkpoint: Mapping[str, Any]) -> Tuple[Mapping[str, Any], Mapping[str, Any]]:
    intent = checkpoint.get("intent")
    run_identity = checkpoint.get("run_identity")
    digests = checkpoint.get("digests")
    created_at = checkpoint.get("created_at")
    workspace = checkpoint.get("workspace")
    if (
        checkpoint.get("planner_revision") != PLANNER_REVISION
        or checkpoint.get("planner_config") != _planner_config()
        or not isinstance(intent, Mapping) or intent.get("schema") != INTENT_SCHEMA
        or not isinstance(run_identity, Mapping) or set(run_identity) != {"run_id", "request_digest"}
        or not isinstance(digests, Mapping) or set(digests) != {"config", "task", "run"}
        or not isinstance(created_at, str) or not created_at
        or not isinstance(workspace, str) or not workspace
    ):
        raise DrainAdmissionProjectionError(
            "checkpoint identity is invalid", reason_code="checkpoint_identity_invalid"
        )
    run_id = run_identity.get("run_id")
    task_digest = _digest(intent)
    config_digest = _digest(_planner_config())
    if (
        not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id)
        or not isinstance(intent.get("repository"), str) or not intent.get("repository")
        or run_identity.get("request_digest") != task_digest
        or digests.get("task") != task_digest or digests.get("config") != config_digest
        or digests.get("run") != _run_digest(
            run_id=run_id, task_digest=task_digest, config_digest=config_digest,
            workspace=workspace, created_at=created_at,
        )
    ):
        raise DrainAdmissionProjectionError(
            "checkpoint digests are invalid", reason_code="checkpoint_identity_invalid"
        )
    return intent, digests


def _validated_items(checkpoint: Mapping[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    items = checkpoint.get("items")
    source = checkpoint.get("source_observation")
    if not isinstance(items, Mapping) or not isinstance(source, Mapping):
        raise DrainAdmissionProjectionError(
            "checkpoint source is invalid", reason_code="checkpoint_source_invalid"
        )
    open_issues = source.get("open_issues")
    if (
        not isinstance(source.get("observed_at"), str) or not source.get("observed_at")
        or not isinstance(open_issues, list)
        or any(not isinstance(number, int) or isinstance(number, bool) or number < 1 for number in open_issues)
        or len(open_issues) != len(set(open_issues)) or open_issues != sorted(open_issues)
        or source.get("digest") != _digest(sorted(open_issues))
    ):
        raise DrainAdmissionProjectionError(
            "checkpoint source observation is invalid", reason_code="checkpoint_source_invalid"
        )
    normalized: Dict[str, Any] = {}
    planned = []
    for raw_key, raw_item in items.items():
        key = str(raw_key)
        if (
            not key.isdigit() or int(key) < 1 or not isinstance(raw_item, Mapping)
            or str(raw_item.get("number") or "") != key
            or raw_item.get("state") not in {"planned", "remote_closed"}
            or not str(raw_item.get("source_revision") or "")
        ):
            raise DrainAdmissionProjectionError(
                "checkpoint item identity/source is invalid", reason_code="checkpoint_source_invalid"
            )
        normalized[key] = dict(raw_item)
        if raw_item.get("state") == "planned":
            planned.append(int(key))
    if sorted(planned) != sorted(open_issues):
        raise DrainAdmissionProjectionError(
            "source observation does not match planned items", reason_code="checkpoint_source_invalid"
        )
    external = checkpoint.get("external_dependencies")
    if not isinstance(external, Mapping) or any(
        not isinstance(value, Mapping) or value.get("state") != "closed" for value in external.values()
    ):
        raise DrainAdmissionProjectionError(
            "external dependency source is invalid", reason_code="checkpoint_source_invalid"
        )
    try:
        rebuilt = plan_issue_waves(normalized)
    except Exception as exc:
        raise DrainAdmissionProjectionError(
            "checkpoint waves cannot be reconstructed", reason_code="checkpoint_plan_invalid"
        ) from exc
    if checkpoint.get("plan") != rebuilt or rebuilt.get("schema") != PLAN_SCHEMA:
        raise DrainAdmissionProjectionError(
            "checkpoint plan is not canonical", reason_code="checkpoint_plan_invalid"
        )
    if rebuilt.get("issue_count", 0) < 1 or not rebuilt.get("waves"):
        raise DrainAdmissionProjectionError("empty drain plans cannot be admitted", reason_code="empty_plan")
    return normalized, rebuilt


def project_checkpoint(checkpoint: Mapping[str, Any]) -> Dict[str, Any]:
    """Pure mapping-to-mapping validation and projection; performs no I/O or Hub action."""
    if not isinstance(checkpoint, Mapping) or checkpoint.get("schema") != INTAKE_SCHEMA:
        raise DrainAdmissionProjectionError("checkpoint schema is invalid", reason_code="checkpoint_invalid")
    if checkpoint.get("integrity_hash") != _digest(_integrity_payload(checkpoint)):
        raise DrainAdmissionProjectionError(
            "checkpoint integrity is invalid", reason_code="checkpoint_integrity_failed"
        )
    outcome = checkpoint.get("outcome")
    if (
        checkpoint.get("execution_authorized") is not False
        or checkpoint.get("metering") != {
            "measurement_state": "unmeasured", "tokens": None, "cost_usd": None,
        }
        or not isinstance(outcome, Mapping)
        or outcome.get("status") != "PLANNED_NOT_EXECUTED"
        or outcome.get("exit_code") != PLANNED_NOT_EXECUTED_EXIT
        or isinstance(outcome.get("exit_code"), bool)
        or outcome.get("execution_authorized") is not False
    ):
        raise DrainAdmissionProjectionError(
            "checkpoint is not final PLANNED_NOT_EXECUTED", reason_code="checkpoint_not_final"
        )
    intent, digests = _identity(checkpoint)
    map_value = checkpoint.get("map")
    canonical = map_value.get("canonical") if isinstance(map_value, Mapping) else None
    if (
        not isinstance(canonical, Mapping) or canonical.get("schema") != MAP_SCHEMA
        or canonical.get("status") != "ready" or canonical.get("mode") != "canonical"
        or str(canonical.get("repository") or "").lower() != str(intent["repository"]).lower()
        or not isinstance(canonical.get("cache_key"), str) or not canonical.get("cache_key")
        or not isinstance(map_value.get("overlays"), Mapping) or map_value.get("overlays")
    ):
        raise DrainAdmissionProjectionError(
            "canonical map is invalid", reason_code="canonical_map_invalid"
        )
    items, plan = _validated_items(checkpoint)
    # Deliberately omit canonical.root/workspace/checkpoint path: no absolute path is durable.
    canonical_projection = {
        key: value for key, value in canonical.items()
        if key in {"schema", "status", "mode", "repository", "tree_hash", "cache_key", "trace_id", "files", "source"}
    }
    job = {
        "schema": JOB_SCHEMA,
        "kind": "github_drain_root",
        "repository": intent["repository"],
        "run_id": checkpoint["run_identity"]["run_id"],
        "run_digest": digests["run"],
        "request_digest": digests["task"],
        "checkpoint_digest": checkpoint["integrity_hash"],
        "source_digest": checkpoint["source_observation"]["digest"],
        "plan_digest": _digest(plan),
        "workspace_digest": _digest(checkpoint["workspace"]),
        "issue_count": plan["issue_count"],
        "source": dict(checkpoint["source_observation"]),
        "canonical_map": canonical_projection,
        "items": items,
        "external_dependencies": {
            str(key): dict(value) for key, value in checkpoint["external_dependencies"].items()
        },
        "plan": plan,
        "dispatchable": False,
        "activation_required": True,
        "execution_authorized": False,
    }
    validate_projected_job(job)
    return job


def load_and_project_checkpoint(path: Union[str, Path]) -> Dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DrainAdmissionProjectionError(
            "checkpoint is not valid JSON", reason_code="checkpoint_invalid"
        ) from exc
    return project_checkpoint(value)


__all__ = [
    "IDEMPOTENCY_PREFIX", "JOB_SCHEMA", "DrainAdmissionProjectionError",
    "admission_idempotency_key", "admission_input_digest", "build_admission_request",
    "load_and_project_checkpoint", "project_checkpoint", "validate_admission_metadata",
    "validate_projected_job",
]
