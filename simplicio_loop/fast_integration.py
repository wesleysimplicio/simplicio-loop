"""Bounded Simplicio Fast v2 integration for Loop convergence (issue #748).

Fast owns ingest, bounded context and PlanDAG receipts. Loop owns the attempt and
candidate fence; Runtime may authorize effects, while Fast delegates source writes
to simplicio-dev-cli. Mapper remains the explicit fallback when Fast is unavailable.
"""
from __future__ import annotations

import hashlib
import json
import re
import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

SCHEMA = "simplicio.loop-fast-integration/v1"
PROBE_SCHEMA = "simplicio.fast.integration-status/v1"
RECEIPT_SCHEMA = "simplicio.loop-fast-receipt/v1"
APPLY_RECEIPT_SCHEMA = "simplicio.loop-fast-apply-receipt/v1"
FAST_INGEST_SCHEMA = "simplicio.fast.ingest/v2"
FAST_UNDERSTANDING_SCHEMA = "simplicio.fast.understanding/v2"
FAST_PLAN_SCHEMA = "simplicio.fast.plandag/v2"
FAST_CHANGESET_SCHEMA = "simplicio.fast.changeset/v2"
# v2.0.14 is the current Fast release validated for the Loop flow.  Keep the
# floor aligned with the release that contains the integrated-ready contract;
# accepting an arbitrary 2.x binary would silently re-enable older operators
# that predate the Loop ingest/understand/plan/apply flow.
FAST_MINIMUM = (2, 0, 14)
FAST_CAPABILITIES = ("ingest", "understand", "plan", "apply", "refresh")


class FastIntegrationError(RuntimeError):
    """Fast could not provide a valid bounded result."""


class FastUnavailable(FastIntegrationError):
    """Fast is unavailable and the configured fallback must be used."""


class FastStaleChangeset(FastIntegrationError):
    """A candidate does not match the pinned generation/context."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _version(value: str) -> tuple[int, int, int]:
    parts: list[int] = []
    for token in str(value).split(".")[:3]:
        digits = "".join(char for char in token if char.isdigit())
        parts.append(int(digits or 0))
    return tuple((parts + [0, 0, 0])[:3])


def _json_output(stdout: str) -> dict[str, Any]:
    text = (stdout or "").strip()
    if not text:
        raise FastIntegrationError("Fast returned no JSON")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        lines = [line for line in text.splitlines() if line.strip()]
        for line in reversed(lines):
            try:
                value = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        else:
            raise FastIntegrationError("Fast returned invalid JSON")
    if not isinstance(value, dict):
        raise FastIntegrationError("Fast returned a non-object JSON payload")
    return value


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


_READ_ONLY_MARKERS = (
    "read-only",
    "read only",
    "sem editar",
    "inspect-only",
    "inspection only",
    "do not modify",
    "without editing",
)
_MUTATION_NODE_KINDS = {"structured_patch", "source_edit", "source-edit"}
_PATH_TOKEN = re.compile(r"(?<![\\w.-])(?:[A-Za-z0-9_.-]+[\\/])+[A-Za-z0-9_.-]*[\\/]?")
INTENT_POLICY_SCHEMA = "simplicio.loop-fast-intent-policy/v1"


def _read_only_intent(task: str) -> bool:
    normalized = " ".join(str(task).lower().split())
    return any(marker in normalized for marker in _READ_ONLY_MARKERS)


def _explicit_targets(task: str) -> list[tuple[str, bool]]:
    targets: dict[str, bool] = {}
    for match in _PATH_TOKEN.findall(str(task)):
        raw = match.strip("`'\".,;:()[]{}")
        if "://" in raw:
            continue
        is_dir = raw.endswith("/") or raw.endswith("\\")
        normalized = raw.replace("\\", "/").lstrip("./").strip("/")
        if normalized:
            targets[normalized] = targets.get(normalized, False) or is_dir
    return sorted(targets.items())


def _context_paths(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        found: list[str] = []
        for key in ("file", "path", "relative_path"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                found.append(item.replace("\\", "/").lstrip("./").strip("/"))
        return found
    if isinstance(value, str) and ("/" in value or "\\" in value):
        return [value.replace("\\", "/").lstrip("./").strip("/")]
    if isinstance(value, (list, tuple)):
        found: list[str] = []
        for item in value:
            found.extend(_context_paths(item))
        return found
    return []


def _corridor_contains(candidate: str, target: tuple[str, bool]) -> bool:
    path, is_dir = target
    return candidate == path or (
        is_dir and candidate.startswith(path.rstrip("/") + "/")
    )


def _blocker(reason: str, message: str, next_surface: str) -> dict[str, Any]:
    return {
        "code": reason,
        "reason": reason,
        "message": message,
        "next_surface": next_surface,
        "retryable": True,
    }


def _validate_plan_policy(
    root: Path, task: str, understanding: Mapping[str, Any], plan: Mapping[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    read_only = _read_only_intent(task)
    targets = _explicit_targets(task)
    target_names = [path + ("/" if is_dir else "") for path, is_dir in targets]
    policy = {
        "schema": INTENT_POLICY_SCHEMA,
        "intent_mode": "read_only" if read_only else "mutable",
        "mutable_authority": not read_only,
        "explicit_targets": target_names,
        "target_corridor": target_names,
        "validated": False,
    }
    blockers: list[dict[str, Any]] = []
    nodes = [node for node in plan.get("nodes", []) if isinstance(node, Mapping)]
    mutation_nodes = [
        node
        for node in nodes
        if str(node.get("kind") or node.get("capability") or "") in _MUTATION_NODE_KINDS
    ]
    if plan.get("schema") != FAST_PLAN_SCHEMA:
        blockers.append(
            _blocker(
                "PLAN_SCHEMA_UNSUPPORTED",
                "versioned Fast PlanDAG schema is required",
                "fast-plan",
            )
        )
    if read_only and mutation_nodes:
        blockers.append(
            _blocker(
                "READ_ONLY_MUTATION_AUTHORITY",
                "read-only intent cannot produce mutation authority",
                "orient",
            )
        )
    allowed_files: list[str] = []
    for node in mutation_nodes:
        inputs = node.get("inputs") if isinstance(node.get("inputs"), Mapping) else {}
        raw_allowed = (
            inputs.get("allowed_files") if isinstance(inputs, Mapping) else None
        )
        if not isinstance(raw_allowed, list) or not raw_allowed:
            blockers.append(
                _blocker(
                    "MUTATION_POLICY_MISSING",
                    "mutable PlanDAG nodes require a versioned changeset and non-empty allowed_files",
                    "plan-policy",
                )
            )
            continue
        if inputs.get("format") != FAST_CHANGESET_SCHEMA:
            blockers.append(
                _blocker(
                    "MUTATION_POLICY_MISSING",
                    "mutable PlanDAG nodes must declare simplicio.fast.changeset/v2",
                    "plan-policy",
                )
            )
        allowed_files.extend(
            str(path).replace("\\", "/").lstrip("./").strip("/")
            for path in raw_allowed
            if isinstance(path, str) and path.strip()
        )
    if targets:
        unresolved = [path for path, _ in targets if not (root / Path(path)).exists()]
        if unresolved:
            blockers.append(
                _blocker(
                    "TARGET_PATH_UNRESOLVED",
                    "explicit target paths must resolve under the orientation repository: "
                    + ", ".join(unresolved),
                    "target",
                )
            )
        if mutation_nodes and not allowed_files:
            blockers.append(
                _blocker(
                    "TARGET_CORRIDOR_MISSING",
                    "explicit targets must define the mutable target corridor",
                    "target",
                )
            )
        off_target = sorted(
            {
                path
                for path in allowed_files
                if not any(_corridor_contains(path, target) for target in targets)
            }
        )
        if off_target:
            blockers.append(
                _blocker(
                    "TARGET_CORRIDOR_MISMATCH",
                    "mutable allowed_files fall outside explicit targets: "
                    + ", ".join(off_target),
                    "target",
                )
            )
        observed = _context_paths(understanding.get("context")) + _context_paths(
            understanding.get("files")
        )
        if (
            mutation_nodes
            and observed
            and not any(
                _corridor_contains(path, target) or path == target[0]
                for path in observed
                for target in targets
            )
        ):
            blockers.append(
                _blocker(
                    "TARGET_RELEVANCE_INSUFFICIENT",
                    "Fast context contains no span in the explicit target corridor",
                    "fast-understand",
                )
            )
    elif mutation_nodes and not (
        _context_paths(understanding.get("context"))
        + _context_paths(understanding.get("files"))
    ):
        blockers.append(
            _blocker(
                "CONTEXT_RELEVANCE_INSUFFICIENT",
                "mutable PlanDAG authority requires at least one bounded Fast context span",
                "fast-understand",
            )
        )
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in blockers:
        if item["reason"] not in seen:
            seen.add(item["reason"])
            unique.append(item)
    policy["validated"] = not unique
    return policy, unique


def _redact_blocked_plan(
    plan: Mapping[str, Any],
    policy: Mapping[str, Any],
    blockers: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    safe = dict(plan)
    safe["nodes"] = [
        dict(node)
        for node in plan.get("nodes", [])
        if isinstance(node, Mapping)
        and str(node.get("kind") or node.get("capability") or "")
        not in _MUTATION_NODE_KINDS
    ]
    safe["status"] = "BLOCKED"
    safe["intent_policy"] = dict(policy)
    safe["blocked_preconditions"] = [dict(item) for item in blockers]
    safe["plan_hash"] = _hash(
        {
            key: value
            for key, value in safe.items()
            if key not in {"plan_hash", "loop_receipt"}
        }
    )
    return safe


@dataclass(frozen=True)
class FastConfig:
    """Explicit, environment-configurable Fast policy."""

    mode: str = "auto"
    command: tuple[str, ...] = ("simplicio-fast",)
    snapshot: str = ".simplicio/fast/project.sfast"
    state: str = ".simplicio/fast/loop-ingest.json"
    max_bytes: int = 48_000
    timeout_seconds: int = 180
    require_binding: bool = True
    # Engine selection is independent from Loop's availability policy.  In
    # ``auto`` Fast itself performs Rust-first health/capability negotiation;
    # ``rust`` is fail-closed and ``python`` is an explicit compatibility path.
    engine: str = "auto"

    @classmethod
    def from_env(cls) -> "FastConfig":
        mode = os.environ.get("SIMPLICIO_FAST_MODE", "auto").strip().lower()
        if mode not in {"auto", "required", "standalone"}:
            raise ValueError("SIMPLICIO_FAST_MODE must be auto, required, or standalone")
        engine = os.environ.get("SIMPLICIO_FAST_ENGINE", "auto").strip().lower()
        if engine not in {"auto", "rust", "python", "off"}:
            raise ValueError("SIMPLICIO_FAST_ENGINE must be auto, rust, python, or off")
        command = tuple(shlex.split(os.environ.get("SIMPLICIO_FAST_COMMAND", "simplicio-fast")))
        if not command:
            raise ValueError("SIMPLICIO_FAST_COMMAND must not be empty")
        return cls(
            mode=mode,
            engine=engine,
            command=command,
            snapshot=os.environ.get("SIMPLICIO_FAST_SNAPSHOT", cls.snapshot),
            state=os.environ.get("SIMPLICIO_FAST_STATE", cls.state),
            max_bytes=max(1, int(os.environ.get("SIMPLICIO_FAST_MAX_BYTES", str(cls.max_bytes)))),
            timeout_seconds=max(1, int(os.environ.get("SIMPLICIO_FAST_TIMEOUT", str(cls.timeout_seconds)))),
            require_binding=os.environ.get("SIMPLICIO_FAST_REQUIRE_BINDING", "1").strip().lower()
            not in {"0", "false", "no", "off"},
        )

    def digest(self) -> str:
        return _hash({"mode": self.mode, "engine": self.engine, "command": self.command, "snapshot": self.snapshot,
                      "max_bytes": self.max_bytes, "require_binding": self.require_binding})


@dataclass(frozen=True)
class FastProbe:
    version: str
    integrated_ready: bool
    fallback: bool
    reason: str | None
    command: tuple[str, ...]
    capabilities: tuple[str, ...] = FAST_CAPABILITIES
    requested_engine: str = "auto"
    selected_engine: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema": PROBE_SCHEMA,
            "name": "simplicio-fast",
            "command": list(self.command),
            "version": self.version,
            "minimum_version": ".".join(map(str, FAST_MINIMUM)),
            "required_capabilities": list(self.capabilities),
            "missing_capabilities": [],
            "integrated_ready": self.integrated_ready,
            "fallback": self.fallback,
            "status": "ready" if self.integrated_ready else "fallback",
            "reason": self.reason,
            "requested_engine": self.requested_engine,
            "selected_engine": self.selected_engine,
        }
        payload["receipt_hash"] = _hash(payload)
        return payload


def validate_changeset(changeset: Mapping[str, Any], *, generation: str = "",
                       context_hash: str = "", require_binding: bool = True) -> dict[str, Any]:
    """Validate the LLM candidate and bind it to the pinned Fast receipt."""
    if not isinstance(changeset, Mapping):
        raise FastIntegrationError("changeset must be an object")
    envelope = dict(changeset)
    raw = envelope.get("changeset") if isinstance(envelope.get("changeset"), Mapping) else envelope
    if raw.get("schema") != FAST_CHANGESET_SCHEMA:
        raise FastIntegrationError("unsupported changeset schema")
    if not isinstance(raw.get("changes"), list) or not raw["changes"]:
        raise FastIntegrationError("changeset must contain at least one change")
    metadata = envelope.get("fast_receipt") or envelope.get("receipt") or {}
    metadata = metadata if isinstance(metadata, Mapping) else {}
    candidate_generation = str(envelope.get("generation") or metadata.get("generation") or "")
    candidate_context = str(envelope.get("context_hash") or metadata.get("context_hash") or "")
    if require_binding and generation and candidate_generation != generation:
        raise FastStaleChangeset("changeset generation does not match the pinned generation")
    if require_binding and context_hash and candidate_context != context_hash:
        raise FastStaleChangeset("changeset context hash does not match the pinned context")
    if require_binding and generation and not candidate_generation:
        raise FastStaleChangeset("changeset is missing the pinned generation")
    if require_binding and context_hash and not candidate_context:
        raise FastStaleChangeset("changeset is missing the pinned context hash")
    return dict(raw)


class FastLoopIntegration:
    """One bounded ingest/context/plan/apply/refresh pipeline for a Loop attempt."""

    def __init__(self, root: str | Path, *, config: FastConfig | None = None,
                 runtime_apply: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
                 fallback_apply: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
                 runner: Callable[..., subprocess.CompletedProcess[str]] | None = None) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError("Fast root must be a directory")
        self.config = config or FastConfig.from_env()
        self.runtime_apply = runtime_apply
        self.fallback_apply = fallback_apply
        self._runner = runner or subprocess.run
        self._probe_cache: FastProbe | None = None
        self._ingest_receipt: dict[str, Any] | None = None
        self._generation = ""
        self._context_hash = ""

    def _runner_env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.config.engine != "auto":
            env["SIMPLICIO_FAST_ENGINE"] = self.config.engine
        return env

    @property
    def generation(self) -> str:
        return self._generation

    @property
    def context_hash(self) -> str:
        return self._context_hash

    @property
    def snapshot_path(self) -> Path:
        return (self.root / self.config.snapshot).resolve()

    def _state_path(self) -> Path:
        return (self.root / self.config.state).resolve()

    def _run(self, args: Sequence[str]) -> dict[str, Any]:
        command = [*self.config.command, *args]
        try:
            result = self._runner(
                command, cwd=str(self.root), capture_output=True, text=True,
                timeout=self.config.timeout_seconds, check=False, env=self._runner_env(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise FastUnavailable(str(exc)) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "Fast command failed").strip()
            raise FastIntegrationError(f"Fast command failed ({result.returncode}): {detail}")
        return _json_output(result.stdout)

    def probe(self, *, force: bool = False) -> dict[str, Any]:
        if self._probe_cache is not None and not force:
            return self._probe_cache.to_dict()
        if self.config.mode == "standalone":
            probe = FastProbe("0.0.0", False, True, "disabled_by_configuration", self.config.command,
                              requested_engine=self.config.engine)
            self._probe_cache = probe
            return probe.to_dict()
        if self.config.engine == "off":
            probe = FastProbe("0.0.0", False, True, "disabled_by_engine", self.config.command,
                              requested_engine="off")
            self._probe_cache = probe
            return probe.to_dict()
        try:
            version_result = self._run(["--version"])
            version_text = str(version_result.get("version") or version_result.get("fast_version") or "")
        except FastIntegrationError:
            try:
                completed = self._runner(
                    [*self.config.command, "--version"], cwd=str(self.root), capture_output=True,
                    text=True, timeout=self.config.timeout_seconds, check=False, env=self._runner_env(),
                )
                if completed.returncode != 0:
                    raise FastUnavailable((completed.stderr or "command unavailable").strip())
                version_text = (completed.stdout or completed.stderr or "").strip().split()[-1]
            except (OSError, subprocess.SubprocessError, FastUnavailable) as exc:
                probe = FastProbe("0.0.0", False, True, "missing_operator", self.config.command,
                                  requested_engine=self.config.engine)
                self._probe_cache = probe
                if self.config.mode == "required":
                    raise FastUnavailable(str(exc)) from exc
                return probe.to_dict()
        try:
            completed = self._runner(
                [*self.config.command, "doctor", "--json"], cwd=str(self.root), capture_output=True,
                text=True, timeout=self.config.timeout_seconds, check=False, env=self._runner_env(),
            )
            doctor = _json_output(completed.stdout or completed.stderr)
            integration = doctor.get("integration") if isinstance(doctor.get("integration"), Mapping) else {}
            integrated = bool(doctor.get("integrated_ready") or integration.get("integrated_ready"))
            ready = integrated and _version(version_text) >= FAST_MINIMUM
            reason = None if ready else "incompatible_operator"
            selected_engine = str(
                doctor.get("selected_engine") or doctor.get("engine")
                or integration.get("selected_engine") or ""
            ).strip().lower() or None
            if self.config.engine == "rust" and selected_engine not in {"rust"}:
                ready, reason = False, "rust_not_verified"
            elif self.config.engine == "python" and selected_engine not in {None, "python"}:
                ready, reason = False, "python_not_selected"
        except (OSError, subprocess.SubprocessError, FastIntegrationError):
            ready, reason, selected_engine = False, "doctor_failed", None
        if self.config.engine == "auto" and selected_engine is None and ready:
            selected_engine = "rust" if bool(doctor.get("rust_ready")) else "python"
        if self.config.engine == "python" and selected_engine is None and ready:
            selected_engine = "python"
        probe = FastProbe(version_text or "0.0.0", ready, not ready, reason, self.config.command,
                          requested_engine=self.config.engine, selected_engine=selected_engine)
        self._probe_cache = probe
        if not ready and self.config.mode == "required":
            raise FastUnavailable(reason or "Fast is not integrated-ready")
        return probe.to_dict()

    def _fallback(self, stage: str, reason: str) -> dict[str, Any]:
        probe = self.probe()
        payload = {
            "schema": RECEIPT_SCHEMA,
            "status": "FALLBACK",
            "stage": stage,
            "fallback": True,
            "fallback_mode": "mapper-dev-cli",
            "reason": reason,
            "probe": probe,
            "generation": self._generation or None,
            "context_hash": self._context_hash or None,
            "requested_engine": self.config.engine,
            "selected_engine": probe.get("selected_engine"),
        }
        payload["receipt_hash"] = _hash(payload)
        return payload

    def _key(self) -> str:
        try:
            completed = self._runner(["git", "rev-parse", "HEAD"], cwd=str(self.root), capture_output=True,
                                    text=True, timeout=15, check=False)
            commit = (completed.stdout or "").strip() if completed.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            commit = ""
        return _hash({"root": str(self.root), "commit": commit, "config": self.config.digest()})

    def ingest(self) -> dict[str, Any]:
        probe = self.probe()
        if not probe["integrated_ready"]:
            return self._fallback("ingest", str(probe.get("reason") or "Fast unavailable"))
        state_path = self._state_path()
        state: dict[str, Any] = {}
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        key = self._key()
        if (state.get("schema") == RECEIPT_SCHEMA and state.get("cache_key") == key
                and self.snapshot_path.exists() and state.get("generation")):
            self._generation = str(state["generation"])
            self._ingest_receipt = dict(state)
            return dict(state)
        try:
            payload = self._run(["ingest", str(self.root), "--output", str(self.snapshot_path), "--json"])
        except FastIntegrationError as exc:
            if self.config.mode == "required":
                raise
            return self._fallback("ingest", str(exc))
        metrics = payload.get("metrics") if isinstance(payload.get("metrics"), Mapping) else {}
        generation = str(payload.get("generation") or metrics.get("generation") or _hash(payload))
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "status": "MEASURED",
            "stage": "ingest",
            "fallback": False,
            "cache_key": key,
            "snapshot": _relative(self.snapshot_path, self.root),
            "source_commit": str(state.get("source_commit") or ""),
            "generation": generation,
            "fast_receipt": payload,
            "config_hash": self.config.digest(),
            "requested_engine": self.config.engine,
            "selected_engine": probe.get("selected_engine"),
        }
        receipt["receipt_hash"] = _hash(receipt)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        self._generation = generation
        self._ingest_receipt = receipt
        return dict(receipt)

    def understand(self, task: str) -> dict[str, Any]:
        if not isinstance(task, str) or not task.strip():
            raise ValueError("Fast task must be non-empty")
        ingest = self.ingest()
        if ingest.get("fallback"):
            return self._fallback("understand", str(ingest.get("reason") or "Fast unavailable"))
        payload = self._run(["understand", task, "--root", str(self.root), "--snapshot", str(self.snapshot_path),
                             "--max-bytes", str(self.config.max_bytes)])
        context = payload.get("context") if isinstance(payload.get("context"), list) else []
        self._context_hash = _hash({"generation": self._generation, "context": context})
        result = dict(payload)
        result["loop_receipt"] = {
            "schema": RECEIPT_SCHEMA, "status": "MEASURED", "stage": "understand",
            "fallback": False, "generation": self._generation, "context_hash": self._context_hash,
            "context_count": len(context), "context_sha256": _hash(context),
        }
        result["loop_receipt"]["receipt_hash"] = _hash(result["loop_receipt"])
        return result

    def plan(self, task: str) -> dict[str, Any]:
        understanding = self.understand(task)
        if understanding.get("fallback"):
            return understanding
        payload = self._run(["plan", task, "--root", str(self.root), "--snapshot", str(self.snapshot_path),
                             "--max-bytes", str(self.config.max_bytes)])
        if payload.get("schema") != FAST_PLAN_SCHEMA:
            raise FastIntegrationError("Fast plan schema is not v2")
        policy, blockers = _validate_plan_policy(self.root, task, understanding, payload)
        if blockers:
            payload = _redact_blocked_plan(payload, policy, blockers)
        plan_hash = _hash(payload)
        result = dict(payload)
        result["intent_policy"] = policy
        result["loop_receipt"] = {
            "schema": RECEIPT_SCHEMA, "status": "BLOCKED" if blockers else "MEASURED", "stage": "plan", "fallback": False,
            "generation": self._generation, "context_hash": self._context_hash,
            "plan_hash": plan_hash, "understanding_receipt_hash": understanding.get("loop_receipt", {}).get("receipt_hash", ""),
        }
        result["loop_receipt"]["receipt_hash"] = _hash(result["loop_receipt"])
        return result

    def prepare(self, task: str) -> dict[str, Any]:
        """Run ingest -> understand -> plan once and return bounded agent context."""
        ingest = self.ingest()
        if ingest.get("fallback"):
            return {"schema": SCHEMA, "status": "FALLBACK", "ingest": ingest,
                    "understanding": self._fallback("understand", str(ingest.get("reason") or "Fast unavailable"))}
        understanding = self.understand(task)
        if understanding.get("fallback"):
            return {"schema": SCHEMA, "status": "FALLBACK", "ingest": ingest, "understanding": understanding}
        plan = self.plan(task)
        if plan.get("status") == "BLOCKED":
            blockers = list(plan.get("blocked_preconditions") or [])
            return {"schema": SCHEMA, "status": "BLOCKED", "ingest": ingest,
                    "understanding": understanding, "plan": plan,
                    "blocked_preconditions": blockers,
                    "reason": blockers[0].get("reason") if blockers else "plan_policy_blocked",
                    "intent_policy": plan.get("intent_policy"),
                    "generation": self._generation, "context_hash": self._context_hash,
                    "plan_hash": plan["loop_receipt"]["plan_hash"]}
        return {"schema": SCHEMA, "status": "READY", "ingest": ingest,
                "understanding": understanding, "plan": plan,
                "intent_policy": plan.get("intent_policy"), "generation": self._generation,
                "context_hash": self._context_hash, "plan_hash": plan["loop_receipt"]["plan_hash"]}

    def apply(self, changeset: Mapping[str, Any], *, winner: bool = True,
              generation: str | None = None, context_hash: str | None = None) -> dict[str, Any]:
        if not winner:
            payload = {"schema": APPLY_RECEIPT_SCHEMA, "status": "SKIPPED", "fallback": False,
                       "reason": "speculative_candidate_not_winner", "applied": False}
            payload["receipt_hash"] = _hash(payload)
            return payload
        generation = generation or self._generation
        context_hash = context_hash or self._context_hash
        probe = self.probe()
        if not probe["integrated_ready"]:
            if self.fallback_apply is None:
                return self._fallback("apply", str(probe.get("reason") or "Fast unavailable"))
            result = dict(self.fallback_apply(changeset))
            payload = {"schema": APPLY_RECEIPT_SCHEMA, "status": "FALLBACK", "fallback": True,
                       "fallback_mode": "mapper-dev-cli", "applied": bool(result.get("applied")),
                       "dev_cli": result, "generation": generation or None, "context_hash": context_hash or None}
            payload["receipt_hash"] = _hash(payload)
            return payload
        raw = validate_changeset(changeset, generation=generation, context_hash=context_hash,
                                 require_binding=self.config.require_binding)
        runtime_receipt: Mapping[str, Any]
        operation = {"schema": SCHEMA, "kind": "apply", "generation": generation,
                     "context_hash": context_hash, "changeset": raw}
        if self.runtime_apply is None:
            runtime_receipt = {"status": "UNAVAILABLE", "reason": "runtime_not_bound"}
        else:
            try:
                runtime_receipt = dict(self.runtime_apply(operation))
            except Exception as exc:
                runtime_receipt = {"status": "BLOCKED", "reason": str(exc)}
        if str(runtime_receipt.get("status")) in {"BLOCKED", "UNAVAILABLE", "UNCERTAIN"}:
            payload = {"schema": APPLY_RECEIPT_SCHEMA, "status": "BLOCKED", "fallback": False,
                       "applied": False, "runtime": runtime_receipt, "generation": generation,
                       "context_hash": context_hash}
            payload["receipt_hash"] = _hash(payload)
            return payload
        temp_path: Path | None = None
        try:
            self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".changeset.json", delete=False,
                                             dir=str(self.snapshot_path.parent)) as handle:
                json.dump(raw, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                temp_path = Path(handle.name)
            fast_receipt = self._run(["apply", str(temp_path), "--root", str(self.root), "--write"])
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        payload = {"schema": APPLY_RECEIPT_SCHEMA, "status": "READY", "fallback": False,
                   "applied": True, "runtime": dict(runtime_receipt), "fast": fast_receipt,
                   "generation": generation, "context_hash": context_hash}
        payload["receipt_hash"] = _hash(payload)
        return payload

    def refresh(self) -> dict[str, Any]:
        probe = self.probe()
        if not probe["integrated_ready"]:
            return self._fallback("refresh", str(probe.get("reason") or "Fast unavailable"))
        payload = self._run(["refresh", str(self.root), "--output", str(self.snapshot_path), "--json"])
        metrics = payload.get("metrics") if isinstance(payload.get("metrics"), Mapping) else {}
        self._generation = str(payload.get("generation") or metrics.get("generation") or _hash(payload))
        self._ingest_receipt = None
        receipt = {"schema": RECEIPT_SCHEMA, "status": "MEASURED", "stage": "refresh",
                   "fallback": False, "generation": self._generation, "fast_receipt": payload,
                   "no_full_remap": True}
        receipt["receipt_hash"] = _hash(receipt)
        return receipt

    def rollout(self, mode: str, *, generation: str | None = None,
                reason: str | None = None, state: str | Path | None = None) -> dict[str, Any]:
        """Persist an atomic Fast rollout transition for this Loop root."""
        if mode not in {"shadow", "canary", "integrated", "fallback", "rollback"}:
            raise ValueError("unsupported Fast rollout mode")
        state_path = Path(state) if state is not None else self.root / ".simplicio/fast" / "rollout.json"
        command = ["rollout", mode, "--state", str(state_path)]
        if generation:
            command.extend(["--generation", str(generation)])
        if reason:
            command.extend(["--reason", str(reason)])
        try:
            return self._run(command)
        except FastIntegrationError:
            if self.config.mode == "required":
                raise
            raise


__all__ = ["APPLY_RECEIPT_SCHEMA", "FAST_CHANGESET_SCHEMA", "FAST_PLAN_SCHEMA", "FastConfig",
           "FastIntegrationError", "FastLoopIntegration", "FastProbe", "FastStaleChangeset",
           "FastUnavailable", "SCHEMA", "validate_changeset"]
