"""Versioned runtime/provider/model capability registry (issue #287, slice 1 of the EPIC).

This module is deliberately narrow: it is the ``ModelCapabilityRegistry`` half of the
architecture proposed in issue #287 ("Implementar roteamento multi-LLM real com
execução Codex + Claude e receipts auditáveis"). It composes with the existing
runtime-identity concepts already in this repo instead of inventing a parallel one:

- ``runtime`` uses the same vocabulary as ``SIMPLICIO_RUNTIME``
  (see ``simplicio_loop/runner.py`` and ``scripts/agent_identity.py``) -
  e.g. ``"claude"``, ``"codex"``, ``"local-devcli"``.
- ``model_id`` uses the same ``provider/model`` shape already produced by
  ``SIMPLICIO_MODEL`` (see ``simplicio_loop/runner.py:_operator_env``) -
  e.g. ``"codex-cli/gpt-5.4"``.

Declared configuration (what an operator *says* is true) is kept separate from a
measured ``probed`` sub-object (what was actually observed). ``probe()`` never
fabricates a pass: a real, local, non-mutating check (binary on PATH / env var
present) is supported today; Codex/Claude external probes are an explicit,
pluggable hook point that returns ``status="UNVERIFIED"`` until a real probe is
wired - the same evidence discipline ``scripts/runtime_matrix.py`` already uses
for ``external_launch_verified``.

Out of scope here (left for later slices of #287): real Codex/Claude
``RuntimeDriver`` execution, fallback/circuit-breaker semantics, and scheduler
integration.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

SCHEMA = "simplicio.model-capability-registry/v1"
ENTRY_SCHEMA = "simplicio.model-capability-entry/v1"

# Reason-code vocabulary lifted verbatim from the issue #287 body so the router and
# any future driver/scheduler layer share one taxonomy instead of inventing new codes.
REASON_CODES = frozenset((
    "missing_capability",
    "context_limit",
    "runtime_unavailable",
    "auth_missing",
    "policy_denied",
    "budget_exceeded",
    "device_incompatible",
    "capacity_exhausted",
))

PROBE_STATUSES = frozenset(("MEASURED", "UNVERIFIED", "UNAVAILABLE"))

_REQUIRED_ENTRY_FIELDS = ("runtime", "provider", "model_id")


class ModelRegistryError(ValueError):
    """Raised for malformed registry configuration or requirements."""


def _stable_hash(data: Any) -> str:
    blob = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _text(value: Any) -> str:
    return str(value or "").strip()


def _str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        raise ModelRegistryError("expected a list of strings, got a bare string")
    return [str(item).strip() for item in value if str(item).strip()]


def _normalize_entry(raw: Mapping[str, Any], index: int) -> Dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ModelRegistryError(f"registry entry[{index}] must be an object")
    missing = [field for field in _REQUIRED_ENTRY_FIELDS if not _text(raw.get(field))]
    if missing:
        raise ModelRegistryError(f"registry entry[{index}] missing required field(s): {', '.join(missing)}")
    probe_cfg = raw.get("probe") or {}
    if not isinstance(probe_cfg, Mapping):
        raise ModelRegistryError(f"registry entry[{index}].probe must be an object")
    declared_capabilities = sorted(set(_str_list(raw.get("capabilities"))))
    entry = {
        "schema": ENTRY_SCHEMA,
        "runtime": _text(raw.get("runtime")),
        "provider": _text(raw.get("provider")),
        "model_id": _text(raw.get("model_id")),
        "aliases": sorted(set(_str_list(raw.get("aliases")))),
        "capabilities": declared_capabilities,
        "context_window": int(raw.get("context_window") or 0),
        "os": sorted(set(_str_list(raw.get("os")))),
        "arch": sorted(set(_str_list(raw.get("arch")))),
        "probe_config": {
            "kind": _text(probe_cfg.get("kind")) or "stub",
            "target": _text(probe_cfg.get("target")),
        },
    }
    if entry["context_window"] < 0:
        raise ModelRegistryError(f"registry entry[{index}].context_window must be >= 0")
    return entry


def _candidate_key(entry: Mapping[str, Any]) -> Tuple[str, str, str]:
    """Order-independent identity key used for de-dup and deterministic sorting."""
    return (entry["runtime"], entry["provider"], entry["model_id"])


ProbeHook = Callable[[Mapping[str, Any]], Mapping[str, Any]]


class ModelCapabilityRegistry:
    """Loads, hashes, probes and filters a versioned runtime/provider/model registry.

    ``entries`` (declared config) and ``probed`` (measured availability) are kept
    separate on purpose: a registry hash is computed only over declared config so
    the same config always produces the same ``registry_hash`` regardless of what a
    given machine's probe happens to observe at run time.
    """

    def __init__(self, entries: Sequence[Mapping[str, Any]], *,
                 registry_version: str = "1",
                 probe_hooks: Optional[Mapping[str, ProbeHook]] = None) -> None:
        normalized = [_normalize_entry(entry, i) for i, entry in enumerate(entries)]
        keys = [_candidate_key(entry) for entry in normalized]
        if len(set(keys)) != len(keys):
            raise ModelRegistryError("duplicate (runtime, provider, model_id) entries in registry")
        # Reject ambiguous aliases: the same alias string must not resolve to more
        # than one distinct model_id (issue #287: "rejeitar aliases ambíguos").
        alias_owner: Dict[str, str] = {}
        for entry in normalized:
            for alias in entry["aliases"]:
                owner = alias_owner.setdefault(alias, entry["model_id"])
                if owner != entry["model_id"]:
                    raise ModelRegistryError(f"ambiguous alias {alias!r} maps to multiple model_ids")
        # Canonical order is content-derived (runtime, provider, model_id), never the
        # order entries happened to be supplied in - so registry_hash and iteration
        # are stable regardless of how a config file/list orders its entries.
        normalized.sort(key=_candidate_key)
        self.registry_version = _text(registry_version) or "1"
        self.entries: List[Dict[str, Any]] = normalized
        self._probe_hooks: Dict[str, ProbeHook] = dict(probe_hooks or {})

    @classmethod
    def load(cls, path: Union[str, Path], *, probe_hooks: Optional[Mapping[str, ProbeHook]] = None) -> "ModelCapabilityRegistry":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ModelRegistryError("registry file must contain a JSON object")
        entries = payload.get("entries")
        if not isinstance(entries, list):
            raise ModelRegistryError("registry file must contain an 'entries' array")
        return cls(entries, registry_version=str(payload.get("registry_version") or "1"), probe_hooks=probe_hooks)

    @property
    def registry_hash(self) -> str:
        """Stable content hash over declared config only (not probe results)."""
        payload = {
            "schema": SCHEMA,
            "registry_version": self.registry_version,
            "entries": self.entries,
        }
        return _stable_hash(payload)

    def find(self, *, runtime: str, model_id: str) -> Optional[Dict[str, Any]]:
        runtime = _text(runtime)
        model_id = _text(model_id)
        for entry in self.entries:
            if entry["runtime"] != runtime:
                continue
            if entry["model_id"] == model_id or model_id in entry["aliases"]:
                return entry
        return None

    # -- probing -----------------------------------------------------------------

    def probe(self, entry: Mapping[str, Any]) -> Dict[str, Any]:
        """Measure (never fabricate) the current availability of one registry entry.

        Declared config and probe result are always returned as separate objects.
        Supported real, non-mutating probe kinds:

        - ``binary_on_path``: ``probe.target`` is a binary name; MEASURED/available
          iff it resolves on PATH via ``shutil.which``.
        - ``env_var_present``: ``probe.target`` is an environment variable name;
          MEASURED/available iff it is set to a non-empty value.
        - anything else (including the default ``stub``): a pluggable hook point
          for future Codex/Claude probes. If ``probe_hooks[runtime]`` was supplied
          it is invoked; otherwise the probe returns ``UNVERIFIED`` and never
          claims success it did not measure.
        """
        cfg = entry.get("probe_config") or {}
        kind = _text(cfg.get("kind")) or "stub"
        target = _text(cfg.get("target"))
        runtime = entry.get("runtime", "")
        if kind == "binary_on_path":
            available = bool(target) and shutil.which(target) is not None
            return {
                "schema": "simplicio.model-probe/v1",
                "kind": kind,
                "target": target,
                "status": "MEASURED",
                "available": available,
                "probed_at": _now(),
                "detail": ("resolved on PATH" if available else "binary not found on PATH"),
            }
        if kind == "env_var_present":
            available = bool(os.environ.get(target, "").strip())
            return {
                "schema": "simplicio.model-probe/v1",
                "kind": kind,
                "target": target,
                "status": "MEASURED",
                "available": available,
                "probed_at": _now(),
                "detail": ("environment variable present" if available else "environment variable missing/empty"),
            }
        hook = self._probe_hooks.get(runtime)
        if hook is not None:
            result = dict(hook(entry))
            result.setdefault("schema", "simplicio.model-probe/v1")
            result.setdefault("kind", kind)
            result.setdefault("target", target)
            result.setdefault("probed_at", _now())
            if result.get("status") not in PROBE_STATUSES:
                raise ModelRegistryError("probe hook returned an unsupported status")
            return result
        # No real probe wired: report UNVERIFIED rather than fabricate a pass.
        return {
            "schema": "simplicio.model-probe/v1",
            "kind": kind,
            "target": target,
            "status": "UNVERIFIED",
            "available": False,
            "probed_at": _now(),
            "detail": "no probe implementation wired for this runtime; treat as unproven",
        }

    def probe_all(self) -> List[Dict[str, Any]]:
        return [dict(entry, probed=self.probe(entry)) for entry in self.entries]

    # -- eligibility ---------------------------------------------------------------

    def eligible_candidates(self, requirements: Mapping[str, Any]) -> Dict[str, Any]:
        """Return entries meeting hard requirements plus structured eliminations.

        ``requirements`` (all optional except none are strictly required to call
        this method; the router normalizes/validates role semantics):

        - ``required_capabilities``: list[str] - every entry must declare all of these.
        - ``allowed_providers`` / ``denied_providers``: list[str].
        - ``os`` / ``arch``: str - device the task will run on; an entry with a
          non-empty os/arch allow-list must include the requested value.
        - ``context_window_min``: int - entry.context_window must be >= this.
        - ``require_probe_available``: bool (default True) - eliminate entries whose
          probe does not resolve to ``available: True`` (runtime_unavailable /
          auth_missing depending on probe kind).
        """
        required_caps = set(_str_list(requirements.get("required_capabilities")))
        allowed_providers = set(_str_list(requirements.get("allowed_providers")))
        denied_providers = set(_str_list(requirements.get("denied_providers")))
        req_os = _text(requirements.get("os"))
        req_arch = _text(requirements.get("arch"))
        context_min = int(requirements.get("context_window_min") or 0)
        require_probe = requirements.get("require_probe_available", True)

        eligible: List[Dict[str, Any]] = []
        eliminated: List[Dict[str, Any]] = []
        for entry in self.entries:
            reason = self._eliminate_reason(
                entry, required_caps=required_caps, allowed_providers=allowed_providers,
                denied_providers=denied_providers, req_os=req_os, req_arch=req_arch,
                context_min=context_min, require_probe=bool(require_probe),
            )
            if reason is None:
                eligible.append(entry)
            else:
                eliminated.append({
                    "runtime": entry["runtime"],
                    "provider": entry["provider"],
                    "model_id": entry["model_id"],
                    "reason_code": reason,
                })
        return {"eligible": eligible, "eliminated": eliminated}

    def _eliminate_reason(self, entry: Mapping[str, Any], *, required_caps, allowed_providers,
                           denied_providers, req_os: str, req_arch: str, context_min: int,
                           require_probe: bool) -> Optional[str]:
        if denied_providers and entry["provider"] in denied_providers:
            return "policy_denied"
        if allowed_providers and entry["provider"] not in allowed_providers:
            return "policy_denied"
        if required_caps and not required_caps.issubset(set(entry["capabilities"])):
            return "missing_capability"
        if context_min and entry["context_window"] and entry["context_window"] < context_min:
            return "context_limit"
        entry_os = entry.get("os") or []
        if entry_os and req_os and req_os not in entry_os:
            return "device_incompatible"
        entry_arch = entry.get("arch") or []
        if entry_arch and req_arch and req_arch not in entry_arch:
            return "device_incompatible"
        if require_probe:
            probed = self.probe(entry)
            if probed["status"] == "MEASURED" and not probed.get("available"):
                cfg_kind = (entry.get("probe_config") or {}).get("kind")
                return "auth_missing" if cfg_kind == "env_var_present" else "runtime_unavailable"
        return None


__all__ = [
    "ENTRY_SCHEMA",
    "ModelCapabilityRegistry",
    "ModelRegistryError",
    "PROBE_STATUSES",
    "REASON_CODES",
    "SCHEMA",
]
