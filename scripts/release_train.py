#!/usr/bin/env python3
"""Deterministic composition and promotion primitives for release train #558.

The release train is deliberately manifest-driven.  This module can compose a set of
already-published component manifests, prove their compatibility, and move an immutable
composition through canary/stable/rollback state.  It does not publish packages or mutate
other repositories; those effects belong to an authenticated adapter around these pure,
fail-closed decisions.

The state file is written with ``os.replace`` after an fsync.  A failed validation therefore
cannot partially promote a composition, and a failed rollback cannot destroy the last known
stable release.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    from .component_release import (
        ECOSYSTEM_SCHEMA,
        SEMVER_RE,
        validate_component_release,
        validate_ecosystem_release,
    )
except ImportError:  # pragma: no cover - direct ``python scripts/release_train.py`` entrypoint
    from component_release import (  # type: ignore
        ECOSYSTEM_SCHEMA,
        SEMVER_RE,
        validate_component_release,
        validate_ecosystem_release,
    )


TRAIN_SCHEMA = "simplicio.release-train/v1"
STATE_SCHEMA = "simplicio.release-train-state/v1"
PROMOTION_SCHEMA = "simplicio.release-train-promotion/v1"
CHANNELS = ("canary", "stable")
GREEN_VALUES = {"green", "passed", "pass", "ok", "success", True}
_CONSTRAINT_RE = re.compile(r"(==|!=|>=|<=|~=|>|<)\s*([0-9A-Za-z.\-+]+)")


class ReleaseTrainError(ValueError):
    """A release train decision is invalid and must not create an effect."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _version_tuple(value: Any) -> Optional[Tuple[int, int, int]]:
    if not isinstance(value, str):
        return None
    match = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?", value.strip().lstrip("v"))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2) or 0), int(match.group(3) or 0)


def _constraint_satisfied(version: str, operator: str, target: str) -> Optional[bool]:
    installed = _version_tuple(version)
    wanted = _version_tuple(target)
    if installed is None or wanted is None:
        return None
    if operator == "==":
        return installed == wanted
    if operator == "!=":
        return installed != wanted
    if operator == ">=":
        return installed >= wanted
    if operator == "<=":
        return installed <= wanted
    if operator == ">":
        return installed > wanted
    if operator == "<":
        return installed < wanted
    if operator == "~=":
        return installed >= wanted and installed[:2] == wanted[:2]
    return None


def satisfies_range(version: str, expression: str) -> Optional[bool]:
    """Evaluate the small PEP 440 range subset used by release manifests.

    ``None`` means the range is not safely comparable.  Callers treat that as blocked,
    never as compatible.
    """
    if not isinstance(expression, str) or not expression.strip():
        return None
    constraints = _CONSTRAINT_RE.findall(expression)
    if not constraints:
        return None
    results = [_constraint_satisfied(version, op, target) for op, target in constraints]
    if any(result is False for result in results):
        return False
    if any(result is None for result in results):
        return None
    return True


def _artifact_digest(artifacts: Sequence[Mapping[str, Any]]) -> Tuple[str, List[str]]:
    rows = []
    for artifact in artifacts:
        digest = artifact.get("digest")
        if not isinstance(digest, str) or not digest.strip():
            raise ReleaseTrainError("component artifact digest must be non-empty")
        rows.append({
            "registry": artifact.get("registry"),
            "os": artifact.get("os"),
            "arch": artifact.get("arch"),
            "digest": digest,
            "size": artifact.get("size"),
        })
    rows.sort(key=lambda row: (str(row.get("registry")), str(row.get("os")),
                               str(row.get("arch")), str(row["digest"])))
    digests = [row["digest"] for row in rows]
    return (digests[0] if len(digests) == 1 else _hash(rows), digests)


def _manifest_channel(manifest: Mapping[str, Any]) -> str:
    channel = manifest.get("channel", "stable")
    if channel not in CHANNELS:
        raise ReleaseTrainError(f"invalid component channel: {channel!r}")
    return str(channel)


def _compatibility_map(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    value = manifest.get("compatibility", {})
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ReleaseTrainError("component compatibility must be an object")
    return value


def _green(value: Any) -> bool:
    if isinstance(value, Mapping):
        if "status" in value:
            return _green(value["status"])
        if "ok" in value:
            return _green(value["ok"])
        return bool(value) and all(_green(item) for item in value.values())
    if isinstance(value, list):
        return bool(value) and all(_green(item) for item in value)
    if isinstance(value, str):
        return value.strip().lower() in GREEN_VALUES
    return value in GREEN_VALUES


def validate_evidence(evidence: Any) -> List[str]:
    """Validate the evidence envelope used to gate composition and promotion."""
    errors: List[str] = []
    if not isinstance(evidence, Mapping) or not evidence:
        return ["evidence must be a non-empty object"]
    required = ("conformance", "installation", "e2e")
    for name in required:
        if name not in evidence:
            errors.append(f"evidence.{name} is required")
        elif not _green(evidence[name]):
            errors.append(f"evidence.{name} is not green")
    for name, value in evidence.items():
        if not isinstance(name, str) or not name.strip():
            errors.append("evidence keys must be non-empty strings")
        if not _green(value):
            errors.append(f"evidence.{name} is not green")
    return sorted(set(errors))


def _component_errors(manifest: Any) -> List[str]:
    errors = validate_component_release(manifest)
    if errors:
        return errors
    assert isinstance(manifest, Mapping)
    if not SEMVER_RE.match(str(manifest["version"])):
        errors.append("version is not semantic")
    if manifest.get("breaking_change") and not manifest.get("migrations"):
        errors.append("breaking component release requires migrations")
    try:
        _manifest_channel(manifest)
        _artifact_digest(manifest["artifacts"])
        _compatibility_map(manifest)
    except (KeyError, TypeError, ReleaseTrainError) as exc:
        errors.append(str(exc))
    return sorted(set(errors))


def _load_manifests(directory: Path) -> List[Dict[str, Any]]:
    if not directory.is_dir():
        raise ReleaseTrainError(f"manifest directory does not exist: {directory}")
    manifests: List[Dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReleaseTrainError(f"cannot read manifest {path.name}: {exc}") from exc
        if not isinstance(data, dict):
            raise ReleaseTrainError(f"manifest {path.name} must be an object")
        errors = _component_errors(data)
        if errors:
            raise ReleaseTrainError(f"manifest {path.name}: {'; '.join(errors)}")
        manifests.append(data)
    if not manifests:
        raise ReleaseTrainError(f"no JSON component manifests found in {directory}")
    return manifests


def select_latest_compatible(
    manifests: Sequence[Mapping[str, Any]], *, required_components: Optional[Iterable[str]] = None,
    max_attempts: int = 4096,
) -> List[Mapping[str, Any]]:
    """Choose the newest compatible manifest for every component.

    Candidate order is newest-first, while backtracking handles a latest release that is
    incompatible with another candidate.  The bounded search prevents a registry or bot
    from turning composition into an unbounded combinatorial operation.
    """
    groups: Dict[str, List[Mapping[str, Any]]] = {}
    for manifest in manifests:
        errors = _component_errors(manifest)
        if errors:
            raise ReleaseTrainError(f"{manifest.get('component', '<unknown>')}: {'; '.join(errors)}")
        name = str(manifest["component"])
        groups.setdefault(name, []).append(manifest)
    required = set(required_components or groups)
    missing = sorted(required - set(groups))
    if missing:
        raise ReleaseTrainError(f"required component missing: {missing}")
    names = sorted(required)
    for name in names:
        groups[name].sort(
            key=lambda item: (_version_tuple(item["version"]) or (-1, -1, -1),
                              str(item.get("commit", ""))),
            reverse=True,
        )

    selected: Dict[str, Mapping[str, Any]] = {}
    attempts = 0

    def search(index: int) -> bool:
        nonlocal attempts
        if attempts >= max_attempts:
            return False
        if index == len(names):
            attempts += 1
            return not _check_compatibility(list(selected.values()))
        name = names[index]
        for candidate in groups[name]:
            selected[name] = candidate
            # Check constraints whose dependency is already selected.  Constraints to
            # later candidates are evaluated at the leaf.
            partial_errors = []
            for owner in selected.values():
                for dependency, expression in _compatibility_map(owner).items():
                    if dependency in selected:
                        result = satisfies_range(str(selected[dependency]["version"]), str(expression))
                        if result is not True:
                            partial_errors.append(f"{owner['component']}->{dependency}")
            if not partial_errors and search(index + 1):
                return True
            selected.pop(name, None)
        return False

    if not search(0):
        reason = "search limit exceeded" if attempts >= max_attempts else "no compatible composition"
        raise ReleaseTrainError(f"latest compatible selection blocked: {reason}")
    return [selected[name] for name in names]


def _check_compatibility(manifests: Sequence[Mapping[str, Any]]) -> List[str]:
    versions = {str(item["component"]): str(item["version"]) for item in manifests}
    errors: List[str] = []
    for manifest in manifests:
        component = str(manifest["component"])
        for dependency, expression in _compatibility_map(manifest).items():
            if dependency not in versions:
                errors.append(f"{component} requires missing component {dependency}")
                continue
            result = satisfies_range(versions[dependency], str(expression))
            if result is not True:
                reason = "unverifiable" if result is None else "out_of_range"
                errors.append(f"{component} compatibility {dependency}{expression!s}: {reason}")
    return sorted(errors)


def compose_release(
    manifests: Sequence[Mapping[str, Any]],
    *,
    release_id: str,
    graph_hash: str,
    contract_hashes: Mapping[str, str],
    evidence: Mapping[str, Any],
    signature: str,
    channel: str = "canary",
    required_components: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Create one immutable ecosystem composition or raise before any effect."""
    if not isinstance(release_id, str) or not release_id.strip():
        raise ReleaseTrainError("release_id must be a non-empty string")
    if not isinstance(graph_hash, str) or not graph_hash.strip():
        raise ReleaseTrainError("graph_hash must be a non-empty string")
    if channel not in CHANNELS:
        raise ReleaseTrainError(f"channel must be one of {CHANNELS}")
    if not isinstance(signature, str) or not signature.strip():
        raise ReleaseTrainError("signature is required; unsigned composition is blocked")
    if not isinstance(contract_hashes, Mapping) or not contract_hashes:
        raise ReleaseTrainError("contract_hashes must be a non-empty object")
    evidence_errors = validate_evidence(evidence)
    if evidence_errors:
        raise ReleaseTrainError("; ".join(evidence_errors))
    if not manifests:
        raise ReleaseTrainError("at least one component manifest is required")

    unique: Dict[str, Mapping[str, Any]] = {}
    errors: List[str] = []
    for manifest in manifests:
        errors.extend(_component_errors(manifest))
        component = manifest.get("component") if isinstance(manifest, Mapping) else None
        if not isinstance(component, str) or not component:
            continue
        if component in unique:
            errors.append(f"duplicate component manifest: {component}")
        else:
            unique[component] = manifest
        if _manifest_channel(manifest) != channel:
            errors.append(f"{component} channel does not match composition channel {channel}")
    for required in required_components or ():
        if required not in unique:
            errors.append(f"required component missing: {required}")
    errors.extend(_check_compatibility(list(unique.values())))
    if errors:
        raise ReleaseTrainError("; ".join(sorted(set(errors))))

    components: Dict[str, Dict[str, Any]] = {}
    for name in sorted(unique):
        manifest = unique[name]
        digest, artifact_digests = _artifact_digest(manifest["artifacts"])
        components[name] = {
            "version": manifest["version"],
            "commit": manifest["commit"],
            "digest": digest,
            "artifact_digests": artifact_digests,
            "channel": channel,
        }
    composition: Dict[str, Any] = {
        "schema": ECOSYSTEM_SCHEMA,
        "release_id": release_id,
        "components": components,
        "graph_hash": graph_hash,
        "contract_hashes": dict(sorted(contract_hashes.items())),
        "status": {
            "overall": "green",
            "components": {name: "green" for name in sorted(components)},
        },
        "evidence": copy.deepcopy(dict(evidence)),
        "rollout": {
            "channel": channel,
            "state": "candidate" if channel == "canary" else "awaiting-canary",
            "canary_release_id": release_id if channel == "canary" else None,
            "rollback": False,
        },
        "signature": signature,
        "provenance": {
            "schema": TRAIN_SCHEMA,
            "composition_hash": _hash(components),
        },
    }
    errors = validate_composition(composition, expected_graph_hash=graph_hash)
    if errors:
        raise ReleaseTrainError("; ".join(errors))
    return composition


def validate_composition(
    composition: Any,
    *,
    expected_graph_hash: Optional[str] = None,
    expected_channel: Optional[str] = None,
) -> List[str]:
    """Return all reasons a composition cannot be promoted."""
    errors = list(validate_ecosystem_release(composition))
    if not isinstance(composition, Mapping):
        return errors
    if expected_graph_hash is not None and composition.get("graph_hash") != expected_graph_hash:
        errors.append("composition graph_hash does not match current graph")
    if expected_channel is not None and composition.get("rollout", {}).get("channel") != expected_channel:
        errors.append("composition channel does not match requested channel")
    if not isinstance(composition.get("signature"), str) or not str(composition.get("signature")).strip():
        errors.append("composition signature is missing")
    rollout = composition.get("rollout")
    if isinstance(rollout, Mapping) and rollout.get("channel") not in CHANNELS:
        errors.append("rollout.channel is invalid")
    status = composition.get("status")
    if isinstance(status, Mapping) and status.get("overall") != "green":
        errors.append("composition status is not green")
    errors.extend(validate_evidence(composition.get("evidence")))
    return sorted(set(errors))


def empty_state() -> Dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "stable": None,
        "canary": None,
        "history": [],
        "last_action": None,
    }


def _validate_state(state: Any) -> List[str]:
    if not isinstance(state, Mapping):
        return ["state must be an object"]
    errors: List[str] = []
    if state.get("schema") != STATE_SCHEMA:
        errors.append("state schema is invalid")
    for field in ("stable", "canary"):
        value = state.get(field)
        if value is not None:
            errors.extend(f"{field}: {item}" for item in validate_composition(value))
    history = state.get("history")
    if not isinstance(history, list):
        errors.append("state.history must be a list")
    else:
        for index, entry in enumerate(history):
            if not isinstance(entry, Mapping) or not isinstance(entry.get("release_id"), str):
                errors.append(f"state.history[{index}] must contain release_id")
    return sorted(set(errors))


def _release_id(composition: Mapping[str, Any]) -> str:
    return str(composition.get("release_id", ""))


def promote_composition(
    state: Mapping[str, Any],
    composition: Mapping[str, Any],
    *,
    channel: str,
    canary_evidence: Optional[Mapping[str, Any]] = None,
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the next state; never mutates ``state`` or ``composition``."""
    errors = _validate_state(state)
    errors.extend(validate_composition(composition, expected_channel=channel))
    if channel not in CHANNELS:
        errors.append(f"channel must be one of {CHANNELS}")
    if errors:
        raise ReleaseTrainError("; ".join(sorted(set(errors))))
    result = copy.deepcopy(dict(state))
    release_id = _release_id(composition)
    if channel == "canary":
        result["canary"] = copy.deepcopy(dict(composition))
        result["last_action"] = {
            "schema": PROMOTION_SCHEMA, "action": "promote", "channel": channel,
            "release_id": release_id, "at": timestamp or _now(),
        }
        return result

    candidate = result.get("canary")
    if not isinstance(candidate, Mapping) or _release_id(candidate) != release_id:
        raise ReleaseTrainError("stable promotion requires the same release to pass canary first")
    evidence = canary_evidence
    if not isinstance(evidence, Mapping) or not _green(evidence.get("status")):
        raise ReleaseTrainError("stable promotion requires green canary evidence")
    previous = result.get("stable")
    if previous is not None:
        result.setdefault("history", []).append({
            "release_id": _release_id(previous),
            "composition": copy.deepcopy(previous),
            "retired_at": timestamp or _now(),
        })
    promoted = copy.deepcopy(dict(composition))
    promoted["rollout"] = dict(promoted.get("rollout", {}))
    promoted["rollout"].update({
        "channel": "stable", "state": "stable", "canary_release_id": release_id,
        "rollback": False,
    })
    result["stable"] = promoted
    result["canary"] = None
    result["last_action"] = {
        "schema": PROMOTION_SCHEMA, "action": "promote", "channel": channel,
        "release_id": release_id, "at": timestamp or _now(),
        "canary_evidence": copy.deepcopy(dict(evidence)),
    }
    return result


def rollback_composition(
    state: Mapping[str, Any], *, target_release_id: Optional[str] = None, timestamp: Optional[str] = None
) -> Dict[str, Any]:
    """Restore a previous stable composition from the immutable history."""
    errors = _validate_state(state)
    if errors:
        raise ReleaseTrainError("; ".join(errors))
    history = state.get("history", [])
    candidates = [entry for entry in history if isinstance(entry, Mapping)]
    if target_release_id is not None:
        candidates = [entry for entry in candidates if entry.get("release_id") == target_release_id]
    if not candidates:
        raise ReleaseTrainError("no previous stable composition is available for rollback")
    target = candidates[-1].get("composition")
    if not isinstance(target, Mapping):
        raise ReleaseTrainError("rollback history entry has no composition")
    result = copy.deepcopy(dict(state))
    current = result.get("stable")
    if current is not None:
        result.setdefault("history", []).append({
            "release_id": _release_id(current),
            "composition": copy.deepcopy(current),
            "retired_at": timestamp or _now(),
        })
    restored = copy.deepcopy(dict(target))
    restored["rollout"] = dict(restored.get("rollout", {}))
    restored["rollout"].update({"channel": "stable", "state": "stable", "rollback": True})
    result["stable"] = restored
    result["canary"] = None
    result["last_action"] = {
        "schema": PROMOTION_SCHEMA, "action": "rollback", "channel": "stable",
        "release_id": _release_id(restored), "at": timestamp or _now(),
    }
    return result


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseTrainError(f"cannot read JSON {path}: {exc}") from exc


def write_json_atomic(path: Path, value: Any) -> None:
    """Persist JSON atomically and durably enough for a local promotion receipt."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return empty_state()
    state = read_json(path)
    errors = _validate_state(state)
    if errors:
        raise ReleaseTrainError("; ".join(errors))
    return dict(state)


def compose_directory(
    directory: Path,
    *,
    release_id: str,
    graph_hash: str,
    contract_hashes: Mapping[str, str],
    evidence: Mapping[str, Any],
    signature: str,
    channel: str,
    required_components: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    candidates = _load_manifests(directory)
    selected = select_latest_compatible(
        [item for item in candidates if _manifest_channel(item) == channel],
        required_components=required_components or [str(item["component"]) for item in candidates],
    )
    return compose_release(
        selected, release_id=release_id, graph_hash=graph_hash,
        contract_hashes=contract_hashes, evidence=evidence, signature=signature,
        channel=channel, required_components=required_components,
    )


def _load_object_file(path_text: str) -> Any:
    return read_json(Path(path_text))


CHILD_BLOCKERS = (
    {
        "component": "simplicio-mapper",
        "issue": "wesleysimplicio/simplicio-mapper#280",
        "status": "UNVERIFIED",
        "reason": "producer manifest/release lives in the mapper repo",
    },
    {
        "component": "simplicio-dev-cli",
        "issue": "wesleysimplicio/simplicio-dev-cli#232",
        "status": "UNVERIFIED",
        "reason": "Mapper consumer bump and Dev CLI publish are external",
    },
    {
        "component": "simplicio-runtime",
        "issue": "wesleysimplicio/simplicio-runtime#3334",
        "status": "UNVERIFIED",
        "reason": "protocol/version alignment and live registry publish are external",
    },
    {
        "component": "simplicio-agent",
        "issue": "wesleysimplicio/simplicio-agent#483",
        "status": "UNVERIFIED",
        "reason": "runtime.lock regeneration is owned by the agent repo",
    },
    {
        "component": "simplicio-code",
        "issue": "wesleysimplicio/simplicio-code#57",
        "status": "UNVERIFIED",
        "reason": "client/bundle regeneration is owned by the code repo",
    },
    {
        "component": "simplicio-loop-marketing",
        "issue": "wesleysimplicio/simplicio-loop-marketing#95",
        "status": "UNVERIFIED",
        "reason": "marketing extension conformance is external",
    },
    {
        "component": "simplicio-loop-oss",
        "issue": "wesleysimplicio/simplicio-loop-oss#10",
        "status": "UNVERIFIED",
        "reason": "OSS extension conformance is external",
    },
)


def _child_manifest_path(repo_path: Path) -> Optional[Path]:
    """Return the first supported component manifest in a child checkout."""
    for relative in (
        Path(".simplicio/component-release.json"),
        Path("component-release.json"),
        Path("dist/component-release.json"),
        Path("release-manifest.json"),
    ):
        candidate = repo_path / relative
        if candidate.is_file():
            return candidate
    return None


def _scan_child_repositories(workspace_root: str | Path) -> List[Dict[str, Any]]:
    """Measure local child manifests without claiming cross-repo automation."""
    root = Path(workspace_root).expanduser()
    rows: List[Dict[str, Any]] = []
    if not root.is_dir():
        for item in CHILD_BLOCKERS:
            row = dict(item)
            row.update({"status": "UNVERIFIED", "reason": "workspace_root_missing",
                        "workspace_root": str(root)})
            rows.append(row)
        return rows

    for item in CHILD_BLOCKERS:
        row = dict(item)
        repo_path = root / str(item["component"])
        row["workspace_path"] = str(repo_path)
        if not repo_path.is_dir():
            row.update({"status": "UNVERIFIED", "reason": "checkout_missing"})
            rows.append(row)
            continue
        manifest_path = _child_manifest_path(repo_path)
        if manifest_path is None:
            row.update({"status": "UNVERIFIED", "reason": "component_manifest_missing"})
            rows.append(row)
            continue
        row["manifest_path"] = str(manifest_path)
        try:
            manifest = read_json(manifest_path)
        except ReleaseTrainError as exc:
            row.update({"status": "BLOCKED", "reason": "manifest_unreadable",
                        "errors": [str(exc)]})
            rows.append(row)
            continue
        errors = validate_component_release(manifest)
        if errors:
            row.update({"status": "BLOCKED", "reason": "invalid_component_manifest",
                        "errors": errors})
        elif manifest.get("component") != item["component"]:
            row.update({"status": "BLOCKED", "reason": "component_identity_mismatch",
                        "declared_component": manifest.get("component")})
        else:
            row.update({"status": "MEASURED", "reason": "component_manifest_valid",
                        "version": manifest.get("version"), "commit": manifest.get("commit")})
        rows.append(row)
    return rows


def doctor_release_train(
    state_path: str | Path | None = None,
    *,
    workspace_root: str | Path | None = None,
) -> Dict[str, Any]:
    """Report Loop-owned status and measured child manifests, never greenwashing AC."""
    state = empty_state()
    if state_path is not None and Path(state_path).is_file():
        state = load_state(Path(state_path))
    children = (
        _scan_child_repositories(workspace_root)
        if workspace_root is not None
        else [dict(item) for item in CHILD_BLOCKERS]
    )
    measured = sum(item.get("status") == "MEASURED" for item in children)
    blocked = sum(item.get("status") == "BLOCKED" for item in children)
    loop_engine = {
        "compose": True,
        "promote": True,
        "rollback": True,
        "component_schema": "simplicio.component-release/v1",
        "ecosystem_schema": "simplicio.ecosystem-release/v1",
        "status": "MEASURED",
    }
    return {
        "schema": "simplicio.release-train-doctor/v1",
        "loop_engine": loop_engine,
        "installed_channel": {
            "canary": (state.get("canary") or {}).get("release_id") if isinstance(state.get("canary"), Mapping) else None,
            "stable": (state.get("stable") or {}).get("release_id") if isinstance(state.get("stable"), Mapping) else None,
        },
        "children": children,
        "child_manifest_coverage": {
            "status": "MEASURED" if workspace_root is not None and Path(workspace_root).expanduser().is_dir() else "UNVERIFIED",
            "workspace_root": str(Path(workspace_root).expanduser()) if workspace_root is not None else None,
            "total": len(children),
            "measured": measured,
            "blocked": blocked,
            "unverified": len(children) - measured - blocked,
        },
        "eight_repo_conformance": "UNVERIFIED",
        "auto_bump_across_repos": "UNVERIFIED",
        "live_registry_publish": "UNVERIFIED",
        "next_action": "keep #558 open until child-repo hops publish verified artifacts",
        "closes_loop_owned_engine": True,
        "closes_eight_repo_ac": False,
    }


def run_namespace(args: argparse.Namespace) -> int:
    try:
        if args.release_train_command == "doctor":
            payload = doctor_release_train(args.state or None, workspace_root=args.workspace_root or None)
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.release_train_command == "compose":
            contracts = _load_object_file(args.contract_hashes)
            evidence = _load_object_file(args.evidence)
            composition = compose_directory(
                Path(args.manifests), release_id=args.release_id, graph_hash=args.graph_hash,
                contract_hashes=contracts, evidence=evidence, signature=args.signature,
                channel=args.channel, required_components=args.required_component,
            )
            if args.output:
                write_json_atomic(Path(args.output), composition)
            print(json.dumps(composition, ensure_ascii=False, sort_keys=True))
            return 0
        if args.release_train_command == "promote":
            composition = read_json(Path(args.composition))
            state_path = Path(args.state)
            state = load_state(state_path)
            evidence = _load_object_file(args.evidence) if args.evidence else None
            next_state = promote_composition(state, composition, channel=args.channel,
                                             canary_evidence=evidence)
            write_json_atomic(state_path, next_state)
            print(json.dumps(next_state, ensure_ascii=False, sort_keys=True))
            return 0
        if args.release_train_command == "rollback":
            state_path = Path(args.state)
            state = load_state(state_path)
            next_state = rollback_composition(state, target_release_id=args.release_id)
            write_json_atomic(state_path, next_state)
            print(json.dumps(next_state, ensure_ascii=False, sort_keys=True))
            return 0
        raise ReleaseTrainError(f"unknown release-train command: {args.release_train_command}")
    except ReleaseTrainError as exc:
        print(json.dumps({
            "schema": PROMOTION_SCHEMA, "ready": False,
            "reason_code": "release_train_blocked", "error": str(exc),
        }, ensure_ascii=False, sort_keys=True))
        return 2


def configure_subparsers(sub: Any) -> None:
    compose = sub.add_parser("compose", help="compose a signed compatible ecosystem release")
    compose.add_argument("--manifests", required=True, help="directory of component-release JSON manifests")
    compose.add_argument("--release-id", required=True)
    compose.add_argument("--graph-hash", required=True)
    compose.add_argument("--contract-hashes", required=True, help="JSON object file")
    compose.add_argument("--evidence", required=True, help="green conformance/install/e2e evidence JSON")
    compose.add_argument("--signature", required=True)
    compose.add_argument("--channel", choices=CHANNELS, default="canary")
    compose.add_argument("--required-component", action="append", default=[])
    compose.add_argument("--output", default="")
    promote = sub.add_parser("promote", help="promote a composition through canary or stable")
    promote.add_argument("--composition", required=True)
    promote.add_argument("--state", required=True)
    promote.add_argument("--channel", choices=CHANNELS, required=True)
    promote.add_argument("--evidence", default="", help="green canary evidence JSON for stable")
    rollback = sub.add_parser("rollback", help="restore a previous stable composition atomically")
    rollback.add_argument("--state", required=True)
    rollback.add_argument("--release-id", default=None)
    doctor = sub.add_parser("doctor", help="report Loop-owned train status and child-repo blockers")
    doctor.add_argument("--state", default="", help="optional release-train state JSON")
    doctor.add_argument("--workspace-root", default="", help="optional directory containing child checkouts")


def configure_parser(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="release_train_command", required=True)
    configure_subparsers(sub)


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="release_train")
    configure_parser(parser)
    return run_namespace(parser.parse_args(list(argv) if argv is not None else None))


if __name__ == "__main__":
    raise SystemExit(main())
