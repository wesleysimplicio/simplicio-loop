"""Canonical Loop -> Runtime -> simplicio-prompt enrichment bridge.

The portable Loop router selects the smallest capability/skill subset. Runtime
owns activation and canonical route provenance. This module joins both without
shell interpolation, materializes bounded bundled skill bodies, and emits the
receipt consumed by host adapters.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
from collections import OrderedDict
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .route import route as portable_route

ROUTE_SCHEMA = "simplicio.route-decision/v1"
RECEIPT_SCHEMA = "simplicio.prompt-enrichment-receipt/v1"
AUTHORITY_LOCKED = {"writes": False, "effects": False}

DEFAULT_MAX_SKILLS = 8
ABSOLUTE_MAX_SKILLS = 64
DEFAULT_MAX_BYTES = 32 * 1024
ABSOLUTE_MAX_BYTES = 256 * 1024
DEFAULT_TIMEOUT_MS = 1_500
MAX_TIMEOUT_MS = 10_000
CACHE_LIMIT = 128

_HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BUNDLE_SKILLS = Path(__file__).resolve().parent / "_bundle" / "skills"
_CACHE: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


class RuntimeResult(Protocol):
    returncode: int
    stdout: str
    stderr: str


RuntimeRunner = Callable[[list[str], float, Mapping[str, str]], RuntimeResult]
BodyLoader = Callable[[str], str | None]


try:
    from kernel.token_budget import estimate_text as _prompt_estimate_text
except ImportError:  # pragma: no cover - direct dependency should satisfy normal installs.
    _prompt_estimate_text = None


def _prompt_version() -> str:
    try:
        return metadata.version("simplicio-prompt")
    except metadata.PackageNotFoundError:
        return "unavailable"


def _bounded_int(
    env: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = str(env.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _token_estimate(text: str) -> dict[str, Any]:
    if _prompt_estimate_text is not None:
        try:
            estimate = _prompt_estimate_text(text, enabled=True)
            value = {
                "count": int(estimate.count),
                "encoding": str(estimate.encoding),
                "source": f"simplicio-prompt/{_prompt_version()}:{estimate.source}",
            }
            fallback_reason = getattr(estimate, "fallback_reason", None)
            if fallback_reason:
                value["fallback_reason"] = str(fallback_reason)
            return value
        except Exception as error:  # pragma: no cover - defensive provider boundary.
            reason = type(error).__name__
    else:
        reason = "dependency_unavailable"
    count = 0 if not text else max(1, (len(text) + 3) // 4)
    return {
        "count": count,
        "encoding": "char_div4",
        "source": "simplicio-loop-visible-fallback",
        "fallback_reason": reason,
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_handles(values: list[str], max_skills: int) -> list[str]:
    handles: list[str] = []
    seen: set[str] = set()
    for value in values:
        handle = str(value).strip()
        if not handle or not _HANDLE_RE.fullmatch(handle) or handle in seen:
            continue
        seen.add(handle)
        handles.append(handle)
        if len(handles) >= max_skills:
            break
    return handles


def _lane(intent: str) -> str:
    if intent == "orchestrate":
        return "batch"
    if intent in {"mutate", "validate", "govern"}:
        return "standard"
    return "interactive"


def _portable_selected(task: str, max_skills: int) -> tuple[dict[str, Any], list[str]]:
    selected = portable_route(task)
    handles = _canonical_handles(
        ["simplicio-orient", *[str(item) for item in selected.get("skills_to_load", [])]],
        max_skills,
    )
    return selected, handles


def _validate_runtime_route(
    value: Any,
    *,
    max_skills: int,
    max_bytes: int,
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(value, dict):
        return None, "route_not_object"
    if value.get("schema") != ROUTE_SCHEMA:
        return None, "route_schema_incompatible"
    for field in ("decision_id", "lane", "reason", "capability"):
        if not isinstance(value.get(field), str) or not str(value[field]).strip():
            return None, f"route_{field}_missing"
    if value.get("capability") != "prompt.enrich":
        return None, "route_capability_incompatible"
    if value.get("runtime_status") != "available":
        return None, "route_runtime_unavailable"
    authority = value.get("authority")
    if not isinstance(authority, dict) or authority.get("writes") is not False or authority.get("effects") is not False:
        return None, "route_authority_not_locked"
    handles = value.get("selected_handles")
    if not isinstance(handles, list) or any(not isinstance(item, str) for item in handles):
        return None, "route_selected_handles_invalid"

    normalized = dict(value)
    normalized["selected_handles"] = _canonical_handles(handles, max_skills)
    normalized["max_skills"] = min(
        max_skills,
        _positive_int(value.get("max_skills"), max_skills, ABSOLUTE_MAX_SKILLS),
    )
    normalized["max_bytes"] = min(
        max_bytes,
        _positive_int(value.get("max_bytes"), max_bytes, ABSOLUTE_MAX_BYTES),
    )
    normalized["authority"] = dict(AUTHORITY_LOCKED)
    return normalized, None


def _positive_int(value: Any, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if 0 < parsed <= maximum else default


def _json_document(stdout: str) -> dict[str, Any] | None:
    raw = str(stdout or "").strip()
    if not raw:
        return None
    candidates = [raw]
    start = raw.find("{")
    end = raw.rfind("}")
    if 0 <= start < end:
        candidates.append(raw[start : end + 1])
    candidates.extend(reversed([line.strip() for line in raw.splitlines() if line.strip()]))
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _extract_route(payload: dict[str, Any]) -> Any:
    if payload.get("schema") == ROUTE_SCHEMA:
        return payload
    return payload.get("prompt_route") or payload.get("route_decision")


def _route_from_environment(
    env: Mapping[str, str],
    *,
    max_skills: int,
    max_bytes: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    raw = str(env.get("SIMPLICIO_ROUTE_DECISION", "")).strip()
    source = "env:SIMPLICIO_ROUTE_DECISION"
    if not raw:
        route_file = str(env.get("SIMPLICIO_ROUTE_DECISION_FILE", "")).strip()
        if route_file:
            source = "file:SIMPLICIO_ROUTE_DECISION_FILE"
            try:
                path = Path(route_file)
                if path.is_file() and path.stat().st_size <= 1_048_576:
                    raw = path.read_text(encoding="utf-8")
            except OSError:
                raw = ""
    if not raw:
        return None, None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None, {"source": source, "reason_code": "runtime_route_json_invalid"}
    route, reason = _validate_runtime_route(value, max_skills=max_skills, max_bytes=max_bytes)
    if route is None:
        return None, {"source": source, "reason_code": reason or "runtime_route_incompatible"}
    return route, {"source": source, "reason_code": None, "exit_code": 0}


def _default_runner(argv: list[str], timeout_seconds: float, env: Mapping[str, str]) -> RuntimeResult:
    return subprocess.run(
        argv,
        text=True,
        capture_output=True,
        check=False,
        shell=False,
        timeout=timeout_seconds,
        env=dict(env),
    )


def _runtime_route(
    task: str,
    selected_handles: list[str],
    *,
    repo: str | Path | None,
    env: Mapping[str, str],
    max_skills: int,
    max_bytes: int,
    runner: RuntimeRunner | None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    existing, diagnostic = _route_from_environment(
        env,
        max_skills=max_skills,
        max_bytes=max_bytes,
    )
    if existing is not None:
        return existing, diagnostic or {"source": "environment", "reason_code": None}

    declared = str(env.get("SIMPLICIO_RUNTIME_AVAILABLE", "")).strip().lower()
    if declared in {"0", "false", "no", "off"}:
        return None, {
            "source": "runtime",
            "reason_code": "runtime_declared_unavailable",
            "exit_code": None,
        }

    runtime = str(env.get("SIMPLICIO_RUNTIME_BIN", "")).strip() or "simplicio"
    argv = [
        runtime,
        "loop",
        "decide",
        "--task",
        task,
        "--prompt-route",
        "--max-skills",
        str(max_skills),
        "--max-bytes",
        str(max_bytes),
        "--no-write",
        "--json",
    ]
    if repo is not None and str(repo).strip():
        argv.extend(["--repo", str(repo)])
    for handle in selected_handles:
        argv.extend(["--selected-handle", handle])

    timeout_ms = _bounded_int(
        env,
        "SIMPLICIO_PROMPT_ROUTE_TIMEOUT_MS",
        DEFAULT_TIMEOUT_MS,
        100,
        MAX_TIMEOUT_MS,
    )
    execute = runner or _default_runner
    try:
        completed = execute(argv, timeout_ms / 1000.0, env)
    except FileNotFoundError:
        return None, {
            "source": "runtime",
            "reason_code": "runtime_not_found",
            "exit_code": None,
        }
    except subprocess.TimeoutExpired:
        return None, {
            "source": "runtime",
            "reason_code": "runtime_timeout",
            "exit_code": None,
        }
    except OSError as error:
        return None, {
            "source": "runtime",
            "reason_code": "runtime_os_error:" + type(error).__name__,
            "exit_code": None,
        }

    exit_code = int(getattr(completed, "returncode", 1))
    if exit_code != 0:
        return None, {
            "source": "runtime",
            "reason_code": f"runtime_exit_{exit_code}",
            "exit_code": exit_code,
        }
    payload = _json_document(str(getattr(completed, "stdout", "")))
    if payload is None:
        return None, {
            "source": "runtime",
            "reason_code": "runtime_output_not_json",
            "exit_code": exit_code,
        }
    route, reason = _validate_runtime_route(
        _extract_route(payload),
        max_skills=max_skills,
        max_bytes=max_bytes,
    )
    if route is None:
        return None, {
            "source": "runtime",
            "reason_code": reason or "runtime_route_incompatible",
            "exit_code": exit_code,
        }
    return route, {
        "source": "runtime",
        "reason_code": None,
        "exit_code": exit_code,
    }


def _fallback_route(
    task: str,
    selected: dict[str, Any],
    handles: list[str],
    diagnostic: Mapping[str, Any],
    *,
    max_skills: int,
    max_bytes: int,
) -> dict[str, Any]:
    intent = str(selected.get("intent") or "survey")
    fingerprint = hashlib.sha256(task.encode("utf-8")).hexdigest()[:16]
    reason = str(diagnostic.get("reason_code") or "runtime_unavailable")
    return {
        "schema": ROUTE_SCHEMA,
        "decision_id": f"loop-fallback:{fingerprint}",
        "lane": _lane(intent),
        "reason": reason,
        "capability": "prompt.enrich",
        "intent": intent,
        "selected_handles": handles,
        "max_skills": max_skills,
        "max_bytes": max_bytes,
        "runtime_status": "incompatible" if "incompatible" in reason else "unavailable",
        "authority": dict(AUTHORITY_LOCKED),
        "provenance": {
            "producer": "simplicio-loop",
            "portable_route_id": selected.get("route_id"),
            "issue": 1210,
        },
    }


def _default_body_loader(handle: str) -> str | None:
    if not _HANDLE_RE.fullmatch(handle):
        return None
    path = (_BUNDLE_SKILLS / handle / "SKILL.md").resolve()
    root = _BUNDLE_SKILLS.resolve()
    if root not in path.parents or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _truncate_utf8(text: str, limit: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    if limit <= 0:
        return "", True
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def _materialize(
    handles: list[str],
    *,
    max_bytes: int,
    body_loader: BodyLoader | None,
) -> dict[str, Any]:
    loader = body_loader or _default_body_loader
    sections: list[str] = []
    loaded_handles: list[str] = []
    digests: list[str] = []
    missing_handles: list[str] = []
    used = 0
    truncated = False

    for handle in handles:
        body = loader(handle)
        if body is None:
            missing_handles.append(handle)
            continue
        heading = f"## Simplicio skill: {handle}\n\n"
        heading_bytes = len(heading.encode("utf-8"))
        remaining = max_bytes - used
        if remaining <= heading_bytes:
            truncated = True
            break
        included, cut = _truncate_utf8(body.strip(), remaining - heading_bytes)
        if not included:
            truncated = True
            break
        section = heading + included
        if cut:
            section += "\n\n[skill body truncated by simplicio-prompt byte budget]"
        section_bytes = len(section.encode("utf-8"))
        if used + section_bytes > max_bytes:
            section, _ = _truncate_utf8(section, max_bytes - used)
            section_bytes = len(section.encode("utf-8"))
            cut = True
        sections.append(section)
        loaded_handles.append(handle)
        digests.append(_sha256(included))
        used += section_bytes
        truncated = truncated or cut
        if used >= max_bytes:
            break

    return {
        "context": "\n\n".join(sections),
        "loaded_handles": loaded_handles,
        "selected_digests": digests,
        "missing_handles": missing_handles,
        "bytes": used,
        "truncated": truncated,
    }


def _cache_key(
    session_id: str,
    decision_id: str,
    handles: list[str],
    max_bytes: int,
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "session_id": session_id,
                "decision_id": decision_id,
                "handles": handles,
                "max_bytes": max_bytes,
            }
        ).encode("utf-8")
    ).hexdigest()


def _cached_materialize(
    session_id: str,
    route: Mapping[str, Any],
    handles: list[str],
    *,
    max_bytes: int,
    body_loader: BodyLoader | None,
) -> tuple[dict[str, Any], bool]:
    # Tests/custom loaders must observe their own fixture and therefore bypass
    # the process-global package-body cache.
    if body_loader is not None:
        return _materialize(handles, max_bytes=max_bytes, body_loader=body_loader), False

    key = _cache_key(session_id, str(route.get("decision_id") or ""), handles, max_bytes)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            _CACHE.move_to_end(key)
            return dict(cached), True

    value = _materialize(handles, max_bytes=max_bytes, body_loader=None)
    with _CACHE_LOCK:
        _CACHE[key] = dict(value)
        _CACHE.move_to_end(key)
        while len(_CACHE) > CACHE_LIMIT:
            _CACHE.popitem(last=False)
    return value, False


def _receipt_block(receipt: Mapping[str, Any]) -> str:
    return f"<!-- {RECEIPT_SCHEMA}\n{_canonical_json(receipt)}\n-->"


def enrich_user_prompt(
    prompt: str,
    *,
    session_id: str = "",
    repo: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    runner: RuntimeRunner | None = None,
    body_loader: BodyLoader | None = None,
) -> dict[str, Any]:
    """Return canonical route, bounded skill context, and auditable receipt."""

    merged_env = dict(os.environ)
    if env is not None:
        merged_env.update({str(key): str(value) for key, value in env.items()})

    max_skills = _bounded_int(
        merged_env,
        "SIMPLICIO_PROMPT_MAX_SKILLS",
        DEFAULT_MAX_SKILLS,
        1,
        ABSOLUTE_MAX_SKILLS,
    )
    max_bytes = _bounded_int(
        merged_env,
        "SIMPLICIO_PROMPT_MAX_BYTES",
        DEFAULT_MAX_BYTES,
        1_024,
        ABSOLUTE_MAX_BYTES,
    )
    selected, handles = _portable_selected(prompt, max_skills)
    route, diagnostic = _runtime_route(
        prompt,
        handles,
        repo=repo,
        env=merged_env,
        max_skills=max_skills,
        max_bytes=max_bytes,
        runner=runner,
    )
    fallback_used = route is None
    if route is None:
        route = _fallback_route(
            prompt,
            selected,
            handles,
            diagnostic,
            max_skills=max_skills,
            max_bytes=max_bytes,
        )

    route_handles = _canonical_handles(
        [str(item) for item in route.get("selected_handles", handles)],
        min(max_skills, _positive_int(route.get("max_skills"), max_skills, ABSOLUTE_MAX_SKILLS)),
    )
    route["selected_handles"] = route_handles
    effective_max_bytes = min(
        max_bytes,
        _positive_int(route.get("max_bytes"), max_bytes, ABSOLUTE_MAX_BYTES),
    )

    materialized, cache_hit = _cached_materialize(
        session_id,
        route,
        route_handles,
        max_bytes=effective_max_bytes,
        body_loader=body_loader,
    )
    context = str(materialized["context"])
    before = _token_estimate(prompt)
    after = _token_estimate(prompt + ("\n\n" + context if context else ""))
    enrichment_digest = _sha256(
        _canonical_json(
            {
                "route": route,
                "digests": materialized["selected_digests"],
                "context": context,
            }
        )
    )
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "adapter_version": "simplicio-loop/1.0.0",
        "prompt_dependency_version": _prompt_version(),
        "profile": "mandatory",
        "runtime_status": route.get("runtime_status"),
        "route_decision": route,
        "portable_route": {
            "route_id": selected.get("route_id"),
            "intent": selected.get("intent"),
            "selected_capabilities": selected.get("selected_capabilities", []),
        },
        "selected_handles": route_handles,
        "materialized_handles": materialized["loaded_handles"],
        "selected_digests": materialized["selected_digests"],
        "missing_handles": materialized["missing_handles"],
        "fallback": {
            "used": fallback_used,
            "reason_code": diagnostic.get("reason_code"),
            "visible": True,
            "profile": "mandatory",
        },
        "runtime": dict(diagnostic),
        "tokens_before": before["count"],
        "tokens_after": after["count"],
        "token_estimator": {
            "before": before,
            "after": after,
        },
        "bytes_before": len(prompt.encode("utf-8")),
        "bytes_after": len(prompt.encode("utf-8")) + len(context.encode("utf-8")),
        "context_bytes": materialized["bytes"],
        "context_truncated": materialized["truncated"],
        "enrichment_digest": enrichment_digest,
        "authority": dict(AUTHORITY_LOCKED),
        "injection": {"detected": False, "elevated": False},
        "cache": {"hit": cache_hit},
        "session_id": session_id or None,
    }
    block = _receipt_block(receipt)
    additional_context = f"{context}\n\n{block}" if context else block
    return {
        "route": route,
        "portable_route": selected,
        "receipt": receipt,
        "additional_context": additional_context,
    }


def reset_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


__all__ = [
    "AUTHORITY_LOCKED",
    "RECEIPT_SCHEMA",
    "ROUTE_SCHEMA",
    "enrich_user_prompt",
    "reset_cache",
]
