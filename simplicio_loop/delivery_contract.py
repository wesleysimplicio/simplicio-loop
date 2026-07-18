"""Strict delivery constraints frozen alongside an adaptive loop anchor."""

from __future__ import annotations

from typing import Any, Iterable

DELIVERY_CONTRACT_SCHEMA = "simplicio.delivery-contract/v1"
FIELDS = {
    "open_pr": bool,
    "push_branch": bool,
    "allow_new_files_in_repo": bool,
    "allow_comments_in_code": bool,
    "commit_message_convention": str,
}
DEFAULTS = {
    "open_pr": True,
    "push_branch": False,
    "allow_new_files_in_repo": True,
    "allow_comments_in_code": True,
    "commit_message_convention": "",
}


def validate_contract(payload: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    if payload.get("schema") != DELIVERY_CONTRACT_SCHEMA:
        errors.append("schema must equal " + DELIVERY_CONTRACT_SCHEMA)
    unknown = sorted(set(payload) - {"schema", *FIELDS})
    if unknown:
        errors.append("unknown fields: " + ", ".join(unknown))
    for name, kind in FIELDS.items():
        if name not in payload:
            errors.append("missing field: " + name)
        elif type(payload[name]) is not kind:
            errors.append(f"{name} must be {kind.__name__}")
    return {"schema": DELIVERY_CONTRACT_SCHEMA, "ok": not errors, "errors": errors}


def normalize_contract(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    result = {"schema": DELIVERY_CONTRACT_SCHEMA, **DEFAULTS, **(payload or {})}
    verdict = validate_contract(result)
    if not verdict["ok"]:
        raise ValueError("invalid delivery contract: " + "; ".join(verdict["errors"]))
    return result


def enforce_diff_contract(*, changed_paths: Iterable[str], added_lines: Iterable[str], contract: dict[str, Any], new_paths: Iterable[str] = ()) -> dict[str, Any]:
    normalized = normalize_contract(contract)
    paths = sorted(set(changed_paths))
    errors: list[str] = []
    if not normalized["allow_new_files_in_repo"]:
        new_files = sorted(set(new_paths))
        if new_files:
            errors.append("new files are forbidden: " + ", ".join(new_files))
    if not normalized["allow_comments_in_code"]:
        comment_lines = [line for line in added_lines if line.lstrip().startswith(("#", "//", "/*", "*", "\"\"\""))]
        if comment_lines:
            errors.append(f"new code comments are forbidden ({len(comment_lines)} line(s))")
    return {"schema": "simplicio.delivery-contract-verdict/v1", "ok": not errors, "errors": errors}
