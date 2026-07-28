"""Deterministic, impact-driven progressive validation.

The controller consumes Mapper-style impact facts and a Dev CLI-style
VerificationPlan without importing either project.  It never invokes a model.
Every execution and cache decision is bound to complete source, tool, config,
and command hashes.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence


SCHEMA = "simplicio.progressive-validation/v1"
RECEIPT_SCHEMA = "simplicio.validation-receipt/v1"
CACHE_SCHEMA = "simplicio.validation-cache/v1"


class ValidationLevel(str, Enum):
    PARSE = "parse"
    FORMAT = "format"
    TARGETED = "targeted"
    IMPACT = "impact"
    MODULE = "module"
    FULL = "full"


LEVEL_ORDER = tuple(ValidationLevel)
LEVEL_INDEX = {level: index for index, level in enumerate(LEVEL_ORDER)}


class Risk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class ValidationCommand:
    level: ValidationLevel
    command: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.command or any(not isinstance(item, str) or not item for item in self.command):
            raise ValueError("validation commands must be non-empty argv strings")


@dataclass(frozen=True)
class ValidationPolicy:
    critical_requires_full: bool = True
    delivery_requires_full: bool = True
    prior_failure_requires_full: bool = True
    high_risk_minimum: ValidationLevel = ValidationLevel.MODULE
    medium_risk_minimum: ValidationLevel = ValidationLevel.IMPACT


@dataclass(frozen=True)
class ValidationRequest:
    source_hash: str
    tool_hash: str
    config_hash: str
    commands: tuple[ValidationCommand, ...]
    impact_level: ValidationLevel = ValidationLevel.TARGETED
    risk: Risk = Risk.LOW
    delivery: bool = False
    prior_failure: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("source_hash", self.source_hash),
            ("tool_hash", self.tool_hash),
            ("config_hash", self.config_hash),
        ):
            if not _is_sha256(value):
                raise ValueError("%s must be a complete sha256 digest" % name)
        levels = [command.level for command in self.commands]
        if len(levels) != len(set(levels)):
            raise ValueError("only one command is allowed per validation level")
        if ValidationLevel.TARGETED not in levels:
            raise ValueError("verification plan must include a targeted command")


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return sha256_bytes(encoded)


def source_tree_hash(root: Path, paths: Iterable[str]) -> str:
    """Hash names and complete bytes for a stable, selected source snapshot."""
    digest = hashlib.sha256()
    normalized = sorted(set(paths))
    for relative in normalized:
        path = root / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if not path.is_file():
            digest.update(b"MISSING")
        else:
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _is_sha256(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    digest = value[7:]
    return len(digest) == 64 and all(char in "0123456789abcdef" for char in digest)


def _required_level(request: ValidationRequest, policy: ValidationPolicy) -> ValidationLevel:
    required = request.impact_level
    if request.risk is Risk.MEDIUM:
        required = max(required, policy.medium_risk_minimum, key=LEVEL_INDEX.get)
    elif request.risk is Risk.HIGH:
        required = max(required, policy.high_risk_minimum, key=LEVEL_INDEX.get)
    elif request.risk is Risk.CRITICAL and policy.critical_requires_full:
        required = ValidationLevel.FULL
    if request.delivery and policy.delivery_requires_full:
        required = ValidationLevel.FULL
    if request.prior_failure and policy.prior_failure_requires_full:
        required = ValidationLevel.FULL
    return required


def selected_commands(
    request: ValidationRequest, policy: ValidationPolicy = ValidationPolicy()
) -> tuple[ValidationCommand, ...]:
    """Return the smallest sufficient ordered prefix available in the plan."""
    required = _required_level(request, policy)
    by_level = {item.level: item for item in request.commands}
    selected = tuple(
        by_level[level]
        for level in LEVEL_ORDER
        if LEVEL_INDEX[level] <= LEVEL_INDEX[required] and level in by_level
    )
    if not selected or selected[-1].level is not required:
        raise ValueError("verification plan has no command for required level %s" % required.value)
    targeted_index = next(
        index for index, item in enumerate(selected) if item.level is ValidationLevel.TARGETED
    )
    if any(
        LEVEL_INDEX[item.level] > LEVEL_INDEX[ValidationLevel.TARGETED]
        for item in selected[:targeted_index]
    ):
        raise ValueError("targeted validation must precede broad validation")
    return selected


class ValidationCache:
    """Small durable cache whose entries fail closed on any input drift."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: Dict[str, Dict[str, Any]] = {}
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schema") != CACHE_SCHEMA:
                raise ValueError("unsupported validation cache")
            self.entries = dict(payload.get("entries", {}))

    @staticmethod
    def key(request: ValidationRequest, item: ValidationCommand) -> str:
        return canonical_hash({
            "source_hash": request.source_hash,
            "tool_hash": request.tool_hash,
            "config_hash": request.config_hash,
            "level": item.level.value,
            "command": list(item.command),
        })

    def lookup(
        self, request: ValidationRequest, item: ValidationCommand
    ) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        key = self.key(request, item)
        entry = self.entries.get(key)
        if entry is None:
            # A same-lane entry proves this is a stale rejection rather than a
            # first execution.  It is never reused under a partial match.
            stale = any(
                candidate.get("level") == item.level.value
                and candidate.get("command") == list(item.command)
                for candidate in self.entries.values()
            )
            return None, "hash_mismatch" if stale else None
        expected = {
            "source_hash": request.source_hash,
            "tool_hash": request.tool_hash,
            "config_hash": request.config_hash,
            "level": item.level.value,
            "command": list(item.command),
        }
        if any(entry.get(name) != value for name, value in expected.items()):
            return None, "entry_mismatch"
        body = dict(entry)
        declared = body.pop("entry_hash", None)
        if declared != canonical_hash(body):
            return None, "entry_tampered"
        return dict(entry), None

    def store(
        self, request: ValidationRequest, item: ValidationCommand, result: Mapping[str, Any]
    ) -> None:
        key = self.key(request, item)
        entry: Dict[str, Any] = {
            "source_hash": request.source_hash,
            "tool_hash": request.tool_hash,
            "config_hash": request.config_hash,
            "level": item.level.value,
            "command": list(item.command),
            "exit_code": int(result["exit_code"]),
            "stdout_hash": str(result["stdout_hash"]),
            "stderr_hash": str(result["stderr_hash"]),
        }
        entry["entry_hash"] = canonical_hash(entry)
        self.entries[key] = entry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema": CACHE_SCHEMA, "entries": self.entries}
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, self.path)


Executor = Callable[[Sequence[str]], Mapping[str, Any]]


def subprocess_executor(command: Sequence[str]) -> Mapping[str, Any]:
    started = time.monotonic_ns()
    process = subprocess.run(
        list(command), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )
    return {
        "exit_code": process.returncode,
        "duration_ns": time.monotonic_ns() - started,
        "stdout_hash": sha256_bytes(process.stdout),
        "stderr_hash": sha256_bytes(process.stderr),
    }


def tool_versions() -> Dict[str, str]:
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
    }


class ProgressiveValidator:
    def __init__(
        self,
        cache_path: Path,
        *,
        policy: ValidationPolicy = ValidationPolicy(),
        executor: Executor = subprocess_executor,
    ) -> None:
        self.cache = ValidationCache(cache_path)
        self.policy = policy
        self.executor = executor

    def run(self, request: ValidationRequest) -> Dict[str, Any]:
        selected = selected_commands(request, self.policy)
        started = time.monotonic_ns()
        rows: list[Dict[str, Any]] = []
        stale_rejections = 0
        incremental_duration = 0
        final_duration: Optional[int] = None

        for item in selected:
            cached, stale_reason = self.cache.lookup(request, item)
            if cached is not None and cached["exit_code"] == 0:
                result: Dict[str, Any] = {
                    "level": item.level.value,
                    "command": list(item.command),
                    "exit_code": 0,
                    "duration_ns": 0,
                    "stdout_hash": cached["stdout_hash"],
                    "stderr_hash": cached["stderr_hash"],
                    "cache": "hit",
                    "stale_rejection": None,
                }
            else:
                if stale_reason:
                    stale_rejections += 1
                observed = dict(self.executor(item.command))
                result = {
                    "level": item.level.value,
                    "command": list(item.command),
                    "exit_code": int(observed["exit_code"]),
                    "duration_ns": int(observed["duration_ns"]),
                    "stdout_hash": str(observed["stdout_hash"]),
                    "stderr_hash": str(observed["stderr_hash"]),
                    "cache": "miss",
                    "stale_rejection": stale_reason,
                }
                if result["exit_code"] == 0:
                    self.cache.store(request, item, result)
            rows.append(result)
            if item.level is ValidationLevel.FULL:
                final_duration = result["duration_ns"]
            else:
                incremental_duration += result["duration_ns"]
            if result["exit_code"] != 0:
                break

        status = "passed" if len(rows) == len(selected) and all(
            row["exit_code"] == 0 for row in rows
        ) else "failed"
        blocked_at = next(
            (row["level"] for row in rows if row["exit_code"] != 0), None
        )
        receipt: Dict[str, Any] = {
            "schema": RECEIPT_SCHEMA,
            "status": status,
            "required_level": selected[-1].level.value,
            "blocked_at": blocked_at,
            "promotion": {
                "risk": request.risk.value,
                "delivery": request.delivery,
                "prior_failure": request.prior_failure,
            },
            "hashes": {
                "source": request.source_hash,
                "tool": request.tool_hash,
                "config": request.config_hash,
            },
            "tool_versions": tool_versions(),
            "commands": rows,
            "metrics": {
                "incremental_duration_ns": incremental_duration,
                "final_duration_ns": final_duration,
                "final_duration_reason": (
                    None if final_duration is not None else "full_lane_not_required_or_not_reached"
                ),
                "total_duration_ns": time.monotonic_ns() - started,
                "executed_lanes": sum(row["cache"] == "miss" for row in rows),
                "cache_hits": sum(row["cache"] == "hit" for row in rows),
                "stale_rejections": stale_rejections,
            },
            "llm_invoked": False,
        }
        receipt["receipt_hash"] = canonical_hash(receipt)
        return receipt


def request_from_dict(payload: Mapping[str, Any]) -> ValidationRequest:
    commands = tuple(
        ValidationCommand(ValidationLevel(str(item["level"])), tuple(item["command"]))
        for item in payload["commands"]
    )
    return ValidationRequest(
        source_hash=str(payload["source_hash"]),
        tool_hash=str(payload["tool_hash"]),
        config_hash=str(payload["config_hash"]),
        commands=commands,
        impact_level=ValidationLevel(str(payload.get("impact_level", "targeted"))),
        risk=Risk(str(payload.get("risk", "low"))),
        delivery=bool(payload.get("delivery", False)),
        prior_failure=bool(payload.get("prior_failure", False)),
    )


def receipt_hash_valid(receipt: Mapping[str, Any]) -> bool:
    body = dict(receipt)
    declared = body.pop("receipt_hash", None)
    return declared == canonical_hash(body)
