#!/usr/bin/env python3
"""Reconcile a component-release event into one safe Loop bump PR.

This is the Loop-owned edge of release-train #558. It consumes an already verified
simplicio.component-release/v1 event, selects the newest candidate that remains inside
the consumer's declared range, raises only the lower bound, and records the selected
commit/digests in a deterministic lock receipt.

The script never widens an upper bound, installs a candidate outside the current range,
or mutates files when validation/compatibility is blocked. Cross-repository event
delivery, cryptographic signature verification, registry polling, and downstream
promotion remain responsibilities of the authenticated release-train control plane.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from scripts.component_release import validate_component_release
from scripts.release_train import ReleaseTrainError, satisfies_range, select_latest_compatible

SCHEMA = "simplicio.release-train-reconciliation/v1"
LOCK_SCHEMA = "simplicio.release-train-lock/v1"
_EVENT_TYPE = "simplicio.component-release.v1"
_EVENT_TYPES = frozenset({
    _EVENT_TYPE,
    "simplicio.component-release-event/v1",
})
_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")
_SPEC_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)(?P<rest>"
    r"(?:\s*(?:===|==|!=|~=|>=|<=|>|<)\s*[0-9A-Za-z.+-]+(?:\s*,\s*)?)*"
    r")"
)
_QUOTED_RE = re.compile(r"""(["'])([^"']+)\1""")


class ReconciliationError(ValueError):
    """The event cannot be applied safely."""


def _version_tuple(value: Any) -> Optional[Tuple[int, int, int]]:
    if not isinstance(value, str):
        return None
    match = _VERSION_RE.match(value.strip())
    if not match:
        return None
    return tuple(int(match.group(index) or 0) for index in (1, 2, 3))


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _parse_spec(spec: str) -> Optional[Tuple[str, str]]:
    match = _SPEC_RE.match(spec.strip())
    if not match or not match.group("name"):
        return None
    return match.group("name"), match.group("rest").strip()


def _project_dependencies(text: str) -> List[Dict[str, Any]]:
    """Read only [project].dependencies while preserving the source text."""
    rows: List[Dict[str, Any]] = []
    section = ""
    in_dependencies = False
    for line_number, line in enumerate(text.splitlines(keepends=True)):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped
            in_dependencies = False
        if section == "[project]" and not in_dependencies and re.match(
            r"^dependencies\s*=\s*\[", stripped
        ):
            in_dependencies = True
        if in_dependencies:
            match = _QUOTED_RE.search(line)
            if match:
                parsed = _parse_spec(match.group(2))
                if parsed:
                    rows.append({
                        "line": line_number,
                        "name": parsed[0],
                        "rest": parsed[1],
                        "spec": match.group(2),
                        "quote_start": match.start(2),
                        "quote_end": match.end(2),
                    })
            if "]" in line:
                in_dependencies = False
    return rows


def _replace_lower_bound(rest: str, selected_version: str) -> Tuple[str, bool, Optional[str]]:
    """Raise only >= floors; return (new_rest, changed, blocking_reason)."""
    selected = _version_tuple(selected_version)
    if selected is None:
        return rest, False, "selected_version_not_semantic"

    expression = rest.strip()
    exact = re.search(r"===?\s*([0-9A-Za-z.+-]+)", expression)
    if exact:
        if _version_tuple(exact.group(1)) == selected:
            return rest, False, None
        return rest, False, "exact_pin_requires_review"

    lower = re.search(r"(>=|~=|>)\s*([0-9A-Za-z.+-]+)", expression)
    if lower:
        operator, current_text = lower.group(1), lower.group(2)
        current = _version_tuple(current_text)
        if current is None:
            return rest, False, "current_floor_unverifiable"
        if operator != ">=":
            if selected <= current:
                return rest, False, None
            return rest, False, "strict_floor_requires_review"
        if selected <= current:
            return rest, False, None
        updated = expression[: lower.start(2)] + selected_version + expression[lower.end(2) :]
        return updated, updated != rest, None

    if expression:
        return f">={selected_version},{expression}", True, None
    return f">={selected_version}", True, None


def _event_payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = event.get("client_payload")
    if isinstance(payload, Mapping):
        return payload
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        return payload
    return event


def _event_type(event: Mapping[str, Any]) -> str:
    """Read either GitHub's dispatch type or a Runtime event schema.

    Runtime's controller names the same wire family
    ``simplicio.component-release-event/v1`` while the original Loop adapter
    used ``simplicio.component-release.v1``. Accept both names, but keep the
    manifest requirement below fail-closed: an artifact-only event is not
    enough information to edit a consumer dependency safely.
    """
    payload = _event_payload(event)
    value = event.get("event_type") or payload.get("event_type") or payload.get("schema")
    if value is None:
        return _EVENT_TYPE
    if not isinstance(value, str) or value not in _EVENT_TYPES:
        raise ReconciliationError(f"unsupported release-train event type: {value!r}")
    return value


def _event_manifests(event: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    payload = _event_payload(event)
    raw = payload.get("manifests")
    if raw is None:
        raw = payload.get("manifest")
    if raw is None:
        raw = payload.get("component_release")
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        raise ReconciliationError("event must contain manifest or manifests")
    manifests: List[Mapping[str, Any]] = []
    for index, manifest in enumerate(raw):
        if not isinstance(manifest, Mapping):
            raise ReconciliationError(f"manifest[{index}] must be an object")
        errors = validate_component_release(dict(manifest))
        if errors:
            raise ReconciliationError(
                f"manifest[{index}] is invalid: " + "; ".join(errors)
            )
        manifests.append(manifest)
    return manifests


def _event_id(event: Mapping[str, Any]) -> str:
    payload = _event_payload(event)
    value = payload.get("release_id") or payload.get("event_id") or event.get("delivery")
    if not isinstance(value, str) or not value.strip():
        raise ReconciliationError("event release_id/event_id is required")
    return value.strip()


def _manifest_package(manifest: Mapping[str, Any]) -> str:
    value = manifest.get("package") or manifest.get("component")
    return str(value)


def _artifact_rows(manifest: Mapping[str, Any]) -> Tuple[List[str], List[str]]:
    digests: List[str] = []
    signatures: List[str] = []
    for artifact in manifest.get("artifacts", []):
        digest = artifact.get("digest") if isinstance(artifact, Mapping) else None
        signature = artifact.get("signature") if isinstance(artifact, Mapping) else None
        if not isinstance(digest, str) or not digest.strip():
            raise ReconciliationError(f"{manifest.get('component')}: artifact digest missing")
        if not isinstance(signature, str) or not signature.strip():
            raise ReconciliationError(f"{manifest.get('component')}: artifact signature missing")
        digests.append(digest)
        signatures.append(signature)
    return digests, signatures


def _lock_payload(
    release_id: str,
    graph_hash: str,
    selected: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    components: Dict[str, Any] = {}
    for manifest in sorted(selected, key=lambda item: _manifest_package(item)):
        digests, signatures = _artifact_rows(manifest)
        package = _manifest_package(manifest)
        components[package] = {
            "component": manifest.get("component"),
            "package": package,
            "version": manifest.get("version"),
            "commit": manifest.get("commit"),
            "tag": manifest.get("tag"),
            "channel": manifest.get("channel", "stable"),
            "digests": sorted(digests),
            "signatures": sorted(signatures),
            "protocols": manifest.get("protocols", {}),
            "compatibility": manifest.get("compatibility", {}),
        }
    return {
        "schema": LOCK_SCHEMA,
        "release_id": release_id,
        "graph_hash": graph_hash,
        "components": components,
        "lock_hash": _canonical_hash(components),
    }


def _write_json(path: Path, value: Mapping[str, Any]) -> bool:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == rendered:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")
    return True


def reconcile(
    repo: Path,
    event: Mapping[str, Any],
    *,
    lock_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Reconcile one event and return a machine-readable receipt."""
    event_type = _event_type(event)
    release_id = _event_id(event)
    payload = _event_payload(event)
    graph_hash = payload.get("graph_hash", "")
    if not isinstance(graph_hash, str):
        raise ReconciliationError("graph_hash must be a string when supplied")
    manifests = _event_manifests(event)
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file():
        raise ReconciliationError("pyproject.toml is missing")

    dependency_rows = _project_dependencies(pyproject.read_text(encoding="utf-8"))
    dependency_map = {row["name"]: row for row in dependency_rows}
    direct_matches: Dict[str, List[Mapping[str, Any]]] = {}
    for manifest in manifests:
        package = _manifest_package(manifest)
        if package in dependency_map:
            direct_matches.setdefault(package, []).append(manifest)

    base_receipt: Dict[str, Any] = {
        "schema": SCHEMA,
        "release_id": release_id,
        "graph_hash": graph_hash,
        "event_type": event_type,
        "direct_components": sorted(direct_matches),
        "selected": {},
        "changed_files": [],
        "errors": [],
    }
    if not direct_matches:
        base_receipt.update({
            "status": "ignored",
            "next_action": "noop",
            "reason": "event_not_a_direct_dependency",
        })
        return base_receipt

    eligible: List[Mapping[str, Any]] = []
    for package, candidates in direct_matches.items():
        expression = dependency_map[package]["rest"]
        for candidate in candidates:
            result = True if not expression else satisfies_range(
                str(candidate["version"]), expression
            )
            if result is True:
                eligible.append(candidate)
    if not eligible:
        base_receipt.update({
            "status": "blocked",
            "next_action": "manual_compatibility_review",
            "reason": "no_candidate_in_declared_range",
            "errors": [
                f"{package}: all candidates are outside {dependency_map[package]['spec']}"
                for package in sorted(direct_matches)
            ],
        })
        return base_receipt

    try:
        target_components = sorted({str(item["component"]) for item in eligible})
        selected = select_latest_compatible(
            eligible,
            required_components=target_components,
        )
    except ReleaseTrainError as exc:
        base_receipt.update({
            "status": "blocked",
            "next_action": "manual_compatibility_review",
            "reason": "incompatible_composition",
            "errors": [str(exc)],
        })
        return base_receipt

    selected_by_package = {_manifest_package(item): item for item in selected}
    original_pyproject = pyproject.read_text(encoding="utf-8")
    lines = original_pyproject.splitlines(keepends=True)
    changed_files: List[str] = []
    for package, manifest in selected_by_package.items():
        row = dependency_map[package]
        new_rest, changed, reason = _replace_lower_bound(
            row["rest"], str(manifest["version"])
        )
        if reason:
            base_receipt.update({
                "status": "blocked",
                "next_action": "manual_constraint_review",
                "reason": reason,
                "errors": [f"{package}: {reason}"],
            })
            return base_receipt
        if changed:
            original_line = lines[row["line"]]
            lines[row["line"]] = (
                original_line[: row["quote_start"]]
                + package + new_rest
                + original_line[row["quote_end"] :]
            )
    updated_pyproject = "".join(lines)
    if updated_pyproject != original_pyproject:
        pyproject.write_text(updated_pyproject, encoding="utf-8")
        changed_files.append("pyproject.toml")

    lock_path = lock_path or (repo / ".simplicio" / "release-train.lock.json")
    lock = _lock_payload(release_id, graph_hash, selected)
    if _write_json(lock_path, lock):
        changed_files.append(str(lock_path.relative_to(repo)))

    base_receipt.update({
        "status": "changed" if changed_files else "unchanged",
        "next_action": "open_or_update_bump_pr" if changed_files else "noop",
        "selected": {
            _manifest_package(item): {
                "component": item.get("component"),
                "version": item.get("version"),
                "commit": item.get("commit"),
                "digests": sorted(_artifact_rows(item)[0]),
            }
            for item in selected
        },
        "changed_files": changed_files,
        "resolved_count": len(selected),
    })
    return base_receipt


def run(event_path: Path, repo: Path, *, lock_path: Optional[Path], output: Optional[Path]) -> Dict[str, Any]:
    try:
        event = json.loads(event_path.read_text(encoding="utf-8"))
        if not isinstance(event, Mapping):
            raise ReconciliationError("event JSON must be an object")
        receipt = reconcile(repo, event, lock_path=lock_path)
    except (OSError, json.JSONDecodeError, ReconciliationError) as exc:
        receipt = {
            "schema": SCHEMA,
            "status": "blocked",
            "next_action": "manual_event_review",
            "reason": "invalid_event",
            "errors": [str(exc)],
            "changed_files": [],
        }
    if output is not None:
        _write_json(output, receipt)
    return receipt


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="release_train_reconciler")
    subparsers = parser.add_subparsers(dest="command", required=True)
    reconcile_parser = subparsers.add_parser("reconcile")
    reconcile_parser.add_argument("--repo", type=Path, default=Path("."))
    reconcile_parser.add_argument("--event", type=Path, required=True)
    reconcile_parser.add_argument("--lock", type=Path, default=None)
    reconcile_parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "reconcile":
        receipt = run(args.event, args.repo, lock_path=args.lock, output=args.output)
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
