from __future__ import annotations

import json
import hashlib
import os
import random
import re
import shutil
import subprocess
import string
import sys
from threading import RLock
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Literal, Mapping, Optional, Sequence, Tuple, TypedDict

from .delivery import (build_delivery_receipt, normalize_delivery_target,
                       reconcile_delivery_observation, write_delivery_receipt)
from .evidence import build_evidence_receipt, redact_sensitive_text
from .source_state import github_delivery_payload, infer_github_delivery_state
from . import github_lifecycle as _github_lifecycle
from .orca_lifecycle import sync_orca_status
from .source_adapter import GitHubSourceAdapter
from .task_contract import compile_many, validate_contract
from .technical_debt import record_notice as _record_technical_debt
from .operator_bootstrap import (
    OperatorBootstrapError,
    ensure_operators as _ensure_required_operators,
)
from .plan_contract import PLAN_SCHEMA, validate_plan
from .remote_queue import HTTPRemoteQueue, QueueConflict, QueueUnavailable, build_completion_receipt
from .agent_contract import bind_receipt, build_context_pack
from .receipt_verifier import (EVIDENCE_RECEIPT_SCHEMA as _EVIDENCE_RECEIPT_CONTENT_SCHEMA,
                               OPERATOR_RECEIPT_SCHEMA as _OPERATOR_RECEIPT_CONTENT_SCHEMA,
                               ReceiptStatus, verify_receipt)
from .planning_gate import content_hash as _planning_content_hash
from .planning_gate import evaluate_mutation_authority, mutation_authority_required
from .planning_gate import auto_planning_receipt_enabled
from .planning_gate import build_planning_receipt as _build_planning_receipt
from .planning_gate import publish_planning_receipt as _publish_planning_receipt
from .work_item_claims import AttemptCoordinator, LeaseLostDuringExecution
from .merge_executor import MergeExecutor, MergeExecutorError
from .model_registry import ModelCapabilityRegistry, ModelRegistryError
from .model_router import ModelRouterError, route as _model_route
from .runtime_drivers import CLI_PROBE_HOOKS, driver_for_runtime
from .runtime_context import ContextAuthorizationError, ContextBudgetError, RuntimeContextRequest
from .runtime_execution_receipt import RuntimeExecutionReceiptError
from .runtime_adapter import LoopRuntimeAdapter, RuntimeAdapterError
from .runtime_bridge import RuntimeBridge
from .runtime_effect_adapter import EffectRequest, RuntimeEffectAdapter, RuntimeEffectError
from .canonical_plan import CanonicalPlan, load_canonical_plan
from .authority_boundary import prepare_authorization_handoff
from .verified_delivery import VerifiedAgentDelivery, VerifiedDeliveryError
from .execution_board import ExecutionBoard
from .execution_route import AGENT_KEYWORDS, SCHEMA as EXECUTION_ROUTE_SCHEMA
from .execution_route import _stable_hash as _execution_route_hash
from .execution_route import capability_fingerprint, normalize_capability_manifest, route_receipt_is_current
from .execution_route import decide_route, verify_route_hash
try:
    from scripts.agent_identity import ensure_identity
except ImportError:  # pragma: no cover - installed package without scripts namespace
    ensure_identity = None

try:
    from scripts.distributed_trust_policy import (
        TrustPolicyError,
        authorize as _trust_authorize,
        load_policy as _load_trust_policy,
        resolve_environment as _resolve_trust_environment,
    )
except ImportError:  # pragma: no cover - installed package without scripts namespace
    TrustPolicyError = RuntimeError  # type: ignore[assignment,misc]
    _trust_authorize = None
    _load_trust_policy = None
    _resolve_trust_environment = None

try:
    from scripts.security_audit_log import append_event as _audit_append
except ImportError:  # pragma: no cover - installed package without scripts namespace
    _audit_append = None

RUNNER_SCHEMA = "simplicio.run-manifest/v1"
STATE_SCHEMA = "simplicio.run-state/v1"
OPERATOR_RECEIPT_SCHEMA = "simplicio.operator-receipt/v0"
# Real content/schema/hash/freshness/provenance validation, gating `receipt_status` in
# `_operator_dispatch_attempt()` below (issue #288: presence of a file must not imply
# VERIFIED).
RECEIPT_MAX_AGE_SECONDS = float(os.environ.get("SIMPLICIO_RECEIPT_MAX_AGE_SECONDS", "86400"))
MAINTENANCE_RECEIPT_SCHEMA = "simplicio.maintenance-receipt/v1"
PHASES = [
    "intake",
    "awaiting_decision",
    "mapping",
    "planning",
    "executing",
    "validating",
    "watching",
    "delivering",
    "done",
    "partial",
    "blocked",
    "cancelled",
]
# Mapper >=0.19 provides the freshness/artifact receipt contract required for
# authoritative context and plan generation. Older versions can report a stale
# `fresh=true` inspect result and are therefore not safe as a planning source.
MAPPER_MIN_VERSION = (0, 19, 0)
MAPPER_REQUIRED_VERBS = ("inspect", "handoff", "ask", "sync", "drift")
DEVCLI_REQUIRED_TOKENS = (" task", "--dry-run-task", "--json")
# Issue #135: the operator bridge validates identity + capability + MIN_VERSION, not
# merely `which`. A dev-cli below this tuple is blocked before any mutation.
DEVCLI_MIN_VERSION = (0, 14, 0)
DEVCLI_REQUIRED_CAPABILITIES = ("task", "--dry-run-task", "--json", "--bound-paths", "--target", "--task-spec", "--mode")
DEFAULT_OPERATOR_WORKERS = 6
BATCH_SCHEMA = "simplicio.operator-batch/v1"
BATCH_PREFLIGHT_SCHEMA = "simplicio.operator-batch-preflight/v1"

MaintenanceMode = Literal["active", "maintenance_deferred"]
MaintenanceDisposition = Literal["operator", "backlog_only"]


class OperatorDispatchItem(TypedDict, total=False):
    """Typed input contract for :func:`dispatch_operator_batch`."""

    repo: str
    run_id: str
    task_index: int
    worker_id: str
    isolation_key: str
    task_id: str
    task_spec: Mapping[str, Any]
    isolation: str
    operator_context: Mapping[str, Any]
    distributed_queue: Any
    agent_identity: Mapping[str, Any]
    context_pack: Mapping[str, Any]


class MaintenanceState(TypedDict):
    mode: MaintenanceMode
    disposition: MaintenanceDisposition
    receipt: str
    correction_summary: str
    deferral_reason: str
    evidence_status: str


class MaintenanceDeferredReceipt(TypedDict):
    schema: str
    mode: Literal["maintenance_deferred"]
    disposition: Literal["backlog_only"]
    correction_summary: str
    deferral_reason: str
    resume_instructions: List[str]
    evidence_status: str
    recorded_at: str
    completion_ready: bool
    completion_verdict: str
    completion_reason_code: str


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _rand_token(n: int = 10) -> str:
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choice(chars) for _ in range(n))


def _resolve_trusted_queue_context(url: str) -> tuple[str, Optional[str], Optional[Dict[str, Any]]]:
    """Resolve the distributed-queue destination via the #289 trust policy.

    ``.github/workflows/distributed-183-proof.yml`` -- the workflow this issue's
    exploit scenario named -- was removed repo-wide in #311, but the same
    exfiltration/confused-deputy risk applies to this call site: it is the real,
    currently-used way to hand a bearer token (``SIMPLICIO_REMOTE_QUEUE_TOKEN``)
    to a network destination. Setting ``SIMPLICIO_REMOTE_ENVIRONMENT_ID`` opts
    into fail-closed resolution: the destination comes from the versioned,
    CODEOWNERS-reviewed ``.github/security/distributed-trust-policy.json``, not
    from ``SIMPLICIO_REMOTE_QUEUE_URL``. A freeform ``SIMPLICIO_REMOTE_QUEUE_URL``
    may still be set for local corroboration, but it must match the policy's
    origin exactly -- an attacker-chosen destination is rejected before any
    identity/queue object (and therefore any token) is created.

    Environments without ``SIMPLICIO_REMOTE_ENVIRONMENT_ID`` set fall back to the
    legacy unmanaged path (whatever ``SIMPLICIO_REMOTE_QUEUE_URL`` names) for
    local/dev use; production/CI callers should set the environment id so the
    destination is policy-resolved.

    Returns ``(url, environment_id, policy)``; ``environment_id``/``policy`` are
    ``None`` on the legacy unmanaged path so callers can tell whether
    connect-time enforcement (:mod:`simplicio_loop.secure_transport`) applies.
    """
    environment_id = os.environ.get("SIMPLICIO_REMOTE_ENVIRONMENT_ID", "").strip()
    if not environment_id:
        return url, None, None
    if _resolve_trust_environment is None or _load_trust_policy is None or _trust_authorize is None:
        raise RuntimeError("distributed trust policy module unavailable")
    policy_path = os.environ.get("SIMPLICIO_DISTRIBUTED_TRUST_POLICY", "").strip()
    policy = _load_trust_policy(Path(policy_path)) if policy_path else _load_trust_policy()
    env = _resolve_trust_environment(policy, environment_id)
    repo_slug = os.environ.get("SIMPLICIO_REMOTE_REPO") or os.environ.get("GITHUB_REPOSITORY", "")
    ref = os.environ.get("SIMPLICIO_REMOTE_REF") or os.environ.get("GITHUB_REF", "")
    actor = os.environ.get("SIMPLICIO_REMOTE_ACTOR") or os.environ.get("GITHUB_ACTOR", "")
    ok, reason = _trust_authorize(policy, environment_id, repo_slug.strip(), ref.strip(), actor.strip())
    if not ok:
        raise RuntimeError("distributed trust policy denied: %s" % reason)
    origin = env["origin"]
    trusted_url = "%s://%s:%s%s" % (
        origin["scheme"], origin["hostname"], origin["port"], origin.get("base_path", "/"),
    )
    if url and url.rstrip("/") != trusted_url.rstrip("/"):
        # This is the literal #289 exploit replayed against the real call site:
        # a caller-supplied destination must never override the reviewed
        # policy, or an attacker who controls SIMPLICIO_REMOTE_QUEUE_URL could
        # redirect the bearer token to infrastructure they control.
        raise RuntimeError(
            "distributed trust policy denied: SIMPLICIO_REMOTE_QUEUE_URL does not match the "
            "resolved origin for environment_id '%s'" % environment_id
        )
    return trusted_url, environment_id, policy


def _resolve_trusted_queue_url(url: str) -> str:
    """Backward-compatible wrapper returning only the resolved URL."""
    resolved, _environment_id, _policy = _resolve_trusted_queue_context(url)
    return resolved


def _distributed_configuration(repo: str) -> tuple[Any, Optional[Dict[str, Any]]]:
    """Return the opt-in network coordinator and stable worker identity.

    Local fan-out remains the default when no URL is configured. Once a URL is
    present, an outage has no local fallback: work pauses instead of mutating
    without a remote claim.
    """
    url = os.environ.get("SIMPLICIO_REMOTE_QUEUE_URL", "").strip()
    if not url and not os.environ.get("SIMPLICIO_REMOTE_ENVIRONMENT_ID", "").strip():
        return None, None
    url, environment_id, policy = _resolve_trusted_queue_context(url)
    if ensure_identity is None:
        raise RuntimeError("distributed identity adapter unavailable")
    identity = ensure_identity(
        path=os.environ.get("SIMPLICIO_IDENTITY_FILE") or str(Path(repo) / ".orchestrator" / "agent-identity.json"),
        runtime=os.environ.get("SIMPLICIO_RUNTIME", "unknown-runtime"),
        capabilities=["claim", "heartbeat", "fencing", "receipts", "events", "evidence", "completion"],
    )
    token = _resolve_queue_token(environment_id, policy, identity)
    queue = HTTPRemoteQueue(
        url,
        token=token,
        timeout=float(os.environ.get("SIMPLICIO_REMOTE_QUEUE_TIMEOUT", "5")),
        environment_id=environment_id,
        policy=policy,
    )
    return queue, identity


STATIC_QUEUE_TOKEN_OPT_IN_VAR = "SIMPLICIO_ALLOW_STATIC_QUEUE_TOKEN"

# #289: the queue operations a worker's short-lived credential is scoped to.
# `enqueue` is deliberately excluded -- workers claim/heartbeat/complete/cancel
# existing tasks, they do not create new ones, so a stolen worker credential
# cannot be used to inject work into the queue.
WORKER_QUEUE_OPERATIONS = (
    "pull", "claim", "heartbeat", "complete", "assert-active", "cancel", "release", "events", "task",
)


def _resolve_queue_token(environment_id: Optional[str], policy: Optional[Dict[str, Any]],
                          identity: Optional[Dict[str, Any]]) -> Optional[str]:
    """Resolve the bearer credential for the distributed queue (#289).

    Preferred path: ``SIMPLICIO_REMOTE_QUEUE_TOKEN_SECRET`` is a long-lived
    HMAC *signing* secret (never sent on the wire); a fresh short-lived token
    (:mod:`scripts.short_lived_credentials`) is minted for this process, bound
    to the worker's agent identity as subject and the environment_id as scope,
    with a TTL taken from the policy's ``max_ttl_seconds`` (capped by
    ``SIMPLICIO_REMOTE_QUEUE_TOKEN_TTL_SECONDS`` if set lower), and restricted
    to :data:`WORKER_QUEUE_OPERATIONS` (operation-level scoping, so a leaked
    token cannot be replayed against an operation this worker never needed).
    This is not the OIDC broker exchange #289 describes -- there is no CI
    identity provider to issue the initial trust, and that gap stays
    permanently blocked absent one -- but it replaces an indefinitely-lived
    static secret with one that expires on its own and carries a revocable
    ``jti``.

    The legacy static ``SIMPLICIO_REMOTE_QUEUE_TOKEN`` is no longer a silent
    fallback: it is only honored when the caller has *also* set
    ``SIMPLICIO_ALLOW_STATIC_QUEUE_TOKEN=1`` (explicit opt-in), and every use
    of it appends a ``reject``-adjacent warning line to the #289 audit log
    (:mod:`scripts.security_audit_log`) so an indefinitely-lived credential in
    use is discoverable, not invisible. Without the opt-in flag, a missing
    signing secret fails closed with ``RuntimeError`` rather than silently
    downgrading to the weaker auth mode.
    """
    secret = os.environ.get("SIMPLICIO_REMOTE_QUEUE_TOKEN_SECRET", "").strip()
    if not secret:
        static_token = os.environ.get("SIMPLICIO_REMOTE_QUEUE_TOKEN", "").strip() or None
        opted_in = os.environ.get(STATIC_QUEUE_TOKEN_OPT_IN_VAR, "").strip().lower() in ("1", "true", "yes")
        if static_token and not opted_in:
            if _audit_append is not None:
                _audit_append(
                    None, event="runner.resolve_queue_token", decision="reject",
                    operation=environment_id or "queue",
                    reason="static SIMPLICIO_REMOTE_QUEUE_TOKEN present without "
                           f"{STATIC_QUEUE_TOKEN_OPT_IN_VAR}=1 opt-in",
                )
            raise RuntimeError(
                "SIMPLICIO_REMOTE_QUEUE_TOKEN_SECRET is not set and the legacy static "
                "SIMPLICIO_REMOTE_QUEUE_TOKEN fallback is no longer silent (#289). Set "
                f"{STATIC_QUEUE_TOKEN_OPT_IN_VAR}=1 to explicitly opt into the deprecated "
                "static-token auth mode for local/dev use, or configure "
                "SIMPLICIO_REMOTE_QUEUE_TOKEN_SECRET to use short-lived credentials instead."
            )
        if static_token and _audit_append is not None:
            _audit_append(
                None, event="runner.resolve_queue_token", decision="accept",
                operation=environment_id or "queue",
                reason="deprecated static-token auth mode explicitly opted into via "
                       f"{STATIC_QUEUE_TOKEN_OPT_IN_VAR}=1",
            )
        return static_token
    try:
        from scripts.short_lived_credentials import issue_token
    except ImportError as exc:  # pragma: no cover - installed package without scripts namespace
        raise RuntimeError("short-lived credential module unavailable") from exc
    max_ttl = float((policy or {}).get("environments", {}).get(environment_id, {}).get("max_ttl_seconds", 900)) \
        if policy and environment_id else 900.0
    override_ttl = os.environ.get("SIMPLICIO_REMOTE_QUEUE_TOKEN_TTL_SECONDS", "").strip()
    ttl_seconds = min(float(override_ttl), max_ttl) if override_ttl else max_ttl
    subject = (identity or {}).get("agent_id", "unknown-agent")
    scope = environment_id or "queue"
    return issue_token(secret, subject=subject, scope=scope, ttl_seconds=ttl_seconds,
                       operations=WORKER_QUEUE_OPERATIONS)


def _run_id() -> str:
    return time.strftime("run-%Y%m%d-%H%M%S-", time.gmtime()) + _rand_token(8)


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _default_completion_state() -> Dict[str, Any]:
    return {
        "ready": False,
        "receipt": "",
        "verdict": "DELIVERY_PENDING",
        "reason_code": "oracle_incomplete",
        "tag": "UNVERIFIED",
    }


def _default_maintenance_state() -> MaintenanceState:
    return {
        "mode": "active",
        "disposition": "operator",
        "receipt": "",
        "correction_summary": "",
        "deferral_reason": "",
        "evidence_status": "UNVERIFIED",
    }


def _active_maintenance_state(current: Mapping[str, Any] | None = None) -> MaintenanceState:
    payload = dict(current or {})
    return {
        "mode": "active",
        "disposition": "operator",
        "receipt": str(payload.get("receipt") or ""),
        "correction_summary": str(payload.get("correction_summary") or ""),
        "deferral_reason": str(payload.get("deferral_reason") or ""),
        "evidence_status": str(payload.get("evidence_status") or "UNVERIFIED"),
    }


def _completion_state(run_dir: Path, current: Dict[str, Any] | None = None) -> Dict[str, Any]:
    state = dict(current or _default_completion_state())
    receipt_path = run_dir / "completion-receipt.json"
    if not receipt_path.exists():
        return state
    payload = _load_json(receipt_path)
    state.update({
        "ready": bool(payload.get("ready", False)),
        "receipt": str(receipt_path),
        "verdict": payload.get("verdict", state.get("verdict", "DELIVERY_PENDING")),
        "reason_code": payload.get("reason_code", state.get("reason_code", "oracle_incomplete")),
        "tag": payload.get("tag", state.get("tag", "UNVERIFIED")),
    })
    return state


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_maintenance_deferred_receipt(
    run_dir: Path,
    *,
    correction_summary: str,
    deferral_reason: str,
    resume_instructions: Sequence[str] | str,
    evidence_status: str,
) -> MaintenanceDeferredReceipt:
    completion = _completion_state(run_dir)
    instructions = [str(item).strip() for item in resume_instructions] if not isinstance(resume_instructions, str) else [resume_instructions.strip()]
    payload: MaintenanceDeferredReceipt = {
        "schema": MAINTENANCE_RECEIPT_SCHEMA,
        "mode": "maintenance_deferred",
        "disposition": "backlog_only",
        "correction_summary": correction_summary.strip(),
        "deferral_reason": deferral_reason.strip(),
        "resume_instructions": [item for item in instructions if item],
        "evidence_status": str(evidence_status or "UNVERIFIED"),
        "recorded_at": _now(),
        "completion_ready": False,
        "completion_verdict": str(completion.get("verdict") or "DELIVERY_PENDING"),
        "completion_reason_code": str(completion.get("reason_code") or "oracle_incomplete"),
    }
    _write_json(run_dir / "maintenance-receipt.json", payload)
    return payload


def _contract_path(run_dir: Path) -> Path:
    return run_dir / "task-contract.json"


def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _run_cmd(argv: List[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=180)


def _run_repo_path(run_dir: Path) -> Optional[Path]:
    """Best-effort recovery of a run's repo checkout path from its ``manifest.json``,
    for the #285 lifecycle-comment identity/branch projection. Returns ``None``
    instead of raising when the manifest is missing/malformed (e.g. a test fixture
    that writes a bare ``state.json`` with no manifest) -- callers treat that as
    "no repo context available", never a hard failure.
    """
    try:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        repo = manifest.get("repo")
        return Path(repo).resolve() if repo else None
    except Exception:
        return None


def _git_current_branch(repo_path: Path) -> str:
    """Best-effort current branch name for the #285 lifecycle comment's
    Branch/worktree field. Never raises -- any git failure (detached HEAD, no
    repo, missing git binary) just yields an empty projection rather than a
    fabricated branch name."""
    try:
        result = _run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo_path)
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    branch = (result.stdout or "").strip()
    return branch if branch and branch != "HEAD" else ""


def _dispatch_identity_fields(repo_path: Optional[Path]) -> Dict[str, str]:
    """Best-effort local agent identity for the #285 lifecycle comment's
    Agente/Runtime/Device fields.

    Reuses the same stable per-repo identity file ``_distributed_configuration()``
    creates for the real distributed dispatch path, so a sequential
    (non-distributed) run projects the same genuine ``agent_id``/``runtime``/
    ``device_id`` instead of leaving those fields blank. Never raises -- an
    unavailable ``scripts.agent_identity`` module (installed package without the
    scripts namespace), a missing repo path, or any I/O failure just yields an
    empty projection rather than fabricated identity.
    """
    if ensure_identity is None or repo_path is None:
        return {}
    try:
        identity = ensure_identity(
            path=os.environ.get("SIMPLICIO_IDENTITY_FILE") or str(repo_path / ".orchestrator" / "agent-identity.json"),
            runtime=os.environ.get("SIMPLICIO_RUNTIME", "unknown-runtime"),
        )
    except Exception:
        return {}
    return {
        "agent_id": str(identity.get("agent_id") or ""),
        "runtime": str(identity.get("runtime") or ""),
        "device": str(identity.get("device_id") or ""),
    }


def _operator_env() -> Dict[str, str]:
    env = dict(os.environ)
    env.setdefault(
        "SIMPLICIO_MODEL",
        os.environ.get("SIMPLICIO_LOOP_OPERATOR_MODEL", "codex-cli/gpt-5.4"),
    )
    env.setdefault(
        "SIMPLICIO_CODEX_EFFORT",
        os.environ.get("SIMPLICIO_LOOP_OPERATOR_EFFORT", "medium"),
    )
    loop_test_cmd = os.environ.get("SIMPLICIO_LOOP_TEST_CMD", "").strip()
    if loop_test_cmd and not env.get("SIMPLICIO_TEST_CMD", "").strip():
        env["SIMPLICIO_TEST_CMD"] = loop_test_cmd
    return env


def _operator_timeout(kind: str) -> int:
    default = 60 if kind == "dry_run" else 600
    raw = os.environ.get("SIMPLICIO_LOOP_OPERATOR_TIMEOUT_SEC", "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(30, value)


def _devcli_env(repo_path: Path, base_env: Dict[str, str] | None = None) -> Dict[str, str]:
    env = dict(base_env or os.environ)
    repo_str = str(repo_path)
    current = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = repo_str if not current else f"{repo_str}{os.pathsep}{current}"
    return env


def _devcli_cmd(repo_path: Path, *args: str) -> List[str]:
    if (repo_path / "simplicio" / "cli.py").exists():
        base = [sys.executable, "-m", "simplicio.cli", *args]
    else:
        base = ["simplicio-dev-cli", *args]
    # Route the operator offline. Prefer an OpenAI-compatible local server when
    # SIMPLICIO_BASE_URL is set (e.g. llama-server on 127.0.0.1:11435 with a Q4 model);
    # otherwise fall back to the bundled llama-cpp-python '--local' mode.
    base_url = os.environ.get("SIMPLICIO_BASE_URL", "").strip()
    if not base_url:
        model = os.environ.get("SIMPLICIO_MODEL", "").strip()
        if model.startswith("local/") and "--local" not in base:
            base.append("--local")
    return base

_RUNTIME_EFFECT_ADAPTERS: Dict[str, RuntimeEffectAdapter] = {}
_RUNTIME_EFFECT_BRIDGES: Dict[str, RuntimeBridge] = {}
_RUNTIME_EFFECT_CACHE_LOCK = RLock()


def _execution_profile() -> str:
    profile = os.environ.get("SIMPLICIO_EXECUTION_PROFILE", "standalone").strip().lower()
    if profile not in {"standalone", "runtime-backed"}:
        raise RuntimeEffectError(
            "SIMPLICIO_EXECUTION_PROFILE must be explicitly standalone or runtime-backed"
        )
    return profile


def _runtime_effect_adapter(repo_path: Path, profile: str) -> RuntimeEffectAdapter:
    if profile not in {"standalone", "runtime-backed"}:
        raise RuntimeEffectError("unsupported execution profile")
    if profile == "standalone":
        return RuntimeEffectAdapter(profile=profile)
    key = str(repo_path.resolve())
    with _RUNTIME_EFFECT_CACHE_LOCK:
        adapter = _RUNTIME_EFFECT_ADAPTERS.get(key)
        if adapter is None:
            bridge = _RUNTIME_EFFECT_BRIDGES.get(key)
            if bridge is None:
                bridge = RuntimeBridge()
                _RUNTIME_EFFECT_BRIDGES[key] = bridge
            adapter = RuntimeEffectAdapter(profile="runtime-backed", bridge=bridge)
            _RUNTIME_EFFECT_ADAPTERS[key] = adapter
    return adapter


def _build_effect_request(repo_path: Path, run_id: str, task_index: int,
                          task: Mapping[str, Any], attempt: int,
                          targets: Sequence[str], route_record: Mapping[str, Any],
                          guarded_attempt: Any,
                          canonical_plan: Optional[CanonicalPlan] = None) -> EffectRequest:
    lease = getattr(guarded_attempt, "lease", None)
    lease_id = str(getattr(lease, "lease_id", "") or f"loop-run:{run_id}")
    raw_fence = getattr(lease, "fencing_token", 1)
    try:
        fencing_token = max(1, int(raw_fence))
    except (TypeError, ValueError):
        fencing_token = 1
    transaction_id = f"{run_id}:{task.get('id') or task_index}:{attempt}"
    return EffectRequest(
        workspace=str(repo_path),
        idempotency_key=transaction_id,
        write_set=tuple(f"repo:{target}" for target in (targets or ["repo"])),
        lease_id=lease_id,
        fencing_token=fencing_token,
        cwd=".",
        timeout_ms=_operator_timeout("execute") * 1000,
        attempt=attempt,
        deadline=int(time.time() * 1000) + _operator_timeout("execute") * 1000,
        cancellation_boundary="safe_boundary_only",
        gate_id=str(route_record.get("receipt_sha") or "execution-route"),
        runtime_generation=os.environ.get("SIMPLICIO_RUNTIME_GENERATION") or None,
        transaction_id=transaction_id,
        canonical_plan=canonical_plan,
    )


def _parse_effect_stdout(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {"raw": redact_sensitive_text(value)}
        return dict(parsed) if isinstance(parsed, dict) else {"raw": redact_sensitive_text(value)}
    return {}


def _execute_operator_effect(*, profile: str, adapter: RuntimeEffectAdapter,
                             request: EffectRequest, argv: List[str],
                             env: Mapping[str, str], repo_path: Path,
                             attempt_coordinator: Optional[AttemptCoordinator],
                             guarded_attempt: Any) -> Dict[str, Any]:
    if profile == "runtime-backed":
        effect_receipt = adapter.execute(request, argv, env=env)
        result = dict(effect_receipt.get("result") or {})
        status = str(effect_receipt.get("status") or result.get("status") or "")
        returncode = result.get("returncode")
        if isinstance(returncode, bool) or not isinstance(returncode, int):
            returncode = None
        if status in {"UNAVAILABLE", "UNCERTAIN"}:
            returncode = None
        return {
            "returncode": returncode,
            "stdout": _parse_effect_stdout(result.get("stdout")),
            "stderr": redact_sensitive_text(str(result.get("stderr") or "")),
            "source": "runtime_effect_adapter",
            "effect_receipt": effect_receipt,
            "uncertain": status == "UNCERTAIN" or result.get("status") == "UNCERTAIN",
        }

    fake = os.environ.get("SIMPLICIO_LOOP_FAKE_OPERATOR_EXEC_JSON", "").strip()
    if fake:
        payload = json.loads(fake)
        for rel, content in (payload.get("write_files") or {}).items():
            path = repo_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(content), encoding="utf-8")
        return {
            "returncode": int(payload.get("returncode", 0)),
            "stdout": payload.get("stdout", {}),
            "stderr": redact_sensitive_text(str(payload.get("stderr", ""))),
            "source": "env_override",
            "effect_receipt": None,
            "uncertain": False,
        }

    try:
        if attempt_coordinator is not None and guarded_attempt is not None:
            result = attempt_coordinator.run_guarded(
                guarded_attempt, argv, cwd=repo_path,
                timeout=_operator_timeout("execute"), env=env,
            )
        else:
            result = subprocess.run(
                argv, cwd=str(repo_path), capture_output=True, text=True,
                timeout=_operator_timeout("execute"), env=env,
            )
        return {
            "returncode": result.returncode,
            "stdout": _parse_effect_stdout((result.stdout or "").strip()),
            "stderr": redact_sensitive_text((result.stderr or "").strip()),
            "source": "live_cli",
            "effect_receipt": None,
            "uncertain": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": None,
            "stdout": {},
            "stderr": f"timed out after {exc.timeout}s",
            "source": "live_cli",
            "effect_receipt": None,
            "uncertain": False,
        }



def _repo_fingerprint(repo_path: Path) -> Dict[str, str]:
    """Return a deterministic content fingerprint for mapper freshness gates.

    Git status alone cannot detect two edits to the same path, so the fingerprint includes
    file bytes for the relevant working tree while excluding generated mapper/run artifacts.
    This is intentionally local and model-free; a later mutation can therefore invalidate the
    plan without trusting an LLM's freshness claim.
    """
    digest = hashlib.sha256()
    files = []
    for root, dirs, names in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in {".git", ".orchestrator", ".simplicio", "__pycache__"}]
        for name in names:
            path = Path(root) / name
            try:
                rel = path.relative_to(repo_path).as_posix()
                data = path.read_bytes()
            except (OSError, ValueError):
                continue
            files.append((rel, data))
    for rel, data in sorted(files, key=lambda item: item[0]):
        digest.update(rel.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).digest())
    head = ""
    status = ""
    try:
        head_result = _run_cmd(["git", "rev-parse", "HEAD"], repo_path)
        head = (head_result.stdout or "").strip() if head_result.returncode == 0 else ""
        status_result = _run_cmd(["git", "status", "--porcelain=v1", "--untracked-files=all"], repo_path)
        if status_result.returncode == 0:
            filtered = []
            for raw_line in (status_result.stdout or "").splitlines():
                line = raw_line.rstrip()
                if len(line) <= 3:
                    continue
                path_text = line[3:].strip()
                parts = [part.strip() for part in path_text.split("->")] if "->" in path_text else [path_text]
                normalized = [part.replace("\\", "/").lstrip("./").lower() for part in parts if part.strip()]
                if normalized and all(
                    item.startswith(".orchestrator/")
                    or item.startswith(".simplicio/")
                    or item.startswith(".claude/")
                    for item in normalized
                ):
                    continue
                filtered.append(line)
            status = "\n".join(filtered).strip()
    except Exception:
        pass
    return {
        "head": head,
        "dirty_status_hash": hashlib.sha256(status.encode("utf-8")).hexdigest(),
        "tree_hash": digest.hexdigest(),
    }


def _repo_state_equivalent(left: Dict[str, str], right: Dict[str, str]) -> bool:
    """Return True when repo content and base commit are unchanged.

    `dirty_status_hash` is useful telemetry, but it can drift because helper-generated
    `.orchestrator`/`.simplicio` state or other non-material status noise changes while the
    tracked working tree bytes remain identical. Freshness gates should therefore key on the
    semantic repository state: commit + tree content hash.
    """
    return (
        (left.get("head") or "") == (right.get("head") or "")
        and (left.get("tree_hash") or "") == (right.get("tree_hash") or "")
    )


def _parse_version_tuple(text: str) -> tuple[int, int, int]:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _preflight_override(name: str) -> Dict[str, Any] | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    return json.loads(raw)


def _resolved_identity(command: str, expected_stems: Sequence[str]) -> Dict[str, Any]:
    """Resolve an operator once and fail closed on a PATH identity mismatch."""
    path = shutil.which(command) or ""
    normalized = Path(path).stem.lower() if path else ""
    return {
        "command": command,
        "path": path,
        "identity_ok": bool(path) and any(stem.lower() in normalized for stem in expected_stems),
    }


def _coverage(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    total_scenarios = 0
    total_rules = 0
    for task in tasks:
        total_scenarios += len(task.get("scenarios") or [])
        total_rules += len(task.get("rules") or [])
    return {
        "scenarios": {"verified": 0, "total": total_scenarios},
        "rules": {"verified": 0, "total": total_rules},
    }


def _criteria_text(task: Dict[str, Any]) -> str:
    lines = []
    for scenario in task.get("scenarios") or []:
        parts = []
        if scenario.get("then"):
            parts.extend(scenario["then"])
        else:
            parts.append(scenario.get("title") or scenario.get("id") or "scenario")
        lines.append("- " + " ".join(parts))
    return "\n".join(lines)


def _constraints_text(task: Dict[str, Any]) -> str:
    lines = []
    for rule in task.get("rules") or []:
        lines.append(f"- {rule.get('id')}: {rule.get('text')}")
    deps = (task.get("dependencies") or {}).get("items") or []
    for dep in deps:
        lines.append(f"- dependency: {dep}")
    return "\n".join(lines)


def _task_goal(task: Dict[str, Any]) -> str:
    identity = task.get("identity") or {}
    story = task.get("story") or {}
    parts = [
        p
        for p in [
            identity.get("system"),
            identity.get("feature"),
            identity.get("type"),
            story.get("persona"),
            story.get("desire"),
            story.get("value"),
        ]
        if p
    ]
    return " | ".join(parts)


def _task_spec_payload(task: Mapping[str, Any]) -> Dict[str, Any]:
    """Build the lossless Dev CLI TaskSpec handoff from a Loop task contract.

    Loop's contract is intentionally richer than the public TaskSpec.  The full
    contract is retained in an additive field while the canonical fields are
    mapped explicitly, so the operator never has to reconstruct the task from
    flattened goal/criteria/constraint strings.
    """
    original_text = str(task.get("original_text") or "")
    if not original_text.strip():
        raise RuntimeError("typed TaskSpec handoff requires task-contract original_text")
    normalized = original_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    source_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    identity = dict(task.get("identity") or {})
    story = dict(task.get("story") or {})

    acceptance_criteria = []
    for scenario in task.get("scenarios") or []:
        item = dict(scenario)
        item.setdefault("original_text", " ".join(
            str(part).strip()
            for part in (
                item.get("title", ""),
                *[str(value) for value in item.get("given") or []],
                *[str(value) for value in item.get("when") or []],
                *[str(value) for value in item.get("then") or []],
            )
            if str(part).strip()
        ))
        acceptance_criteria.append(item)

    def stateful_items(value: Any, prefix: str) -> list[Dict[str, Any]]:
        if not isinstance(value, Mapping):
            return []
        state = str(value.get("state") or "unknown")
        items = []
        for index, entry in enumerate(value.get("items") or [], start=1):
            if isinstance(entry, Mapping):
                item = dict(entry)
                item.setdefault("id", f"{prefix}{index}")
                item.setdefault("original_text", str(item.get("text") or item.get("summary") or ""))
            else:
                item = {"id": f"{prefix}{index}", "text": str(entry), "original_text": str(entry)}
            item.setdefault("state", state)
            items.append(item)
        return items

    questions = [dict(item) for item in task.get("questions") or [] if isinstance(item, Mapping)]
    assumptions = [dict(item) for item in task.get("assumptions") or [] if isinstance(item, Mapping)]
    blockers = [dict(item) for item in task.get("blockers") or [] if isinstance(item, Mapping)]
    access_path = str(task.get("access_path") or "").strip()
    source = task.get("source") or {}
    task_id = str(identity.get("id") or identity.get("title") or f"TASK-{source_hash[:12].upper()}")
    payload: Dict[str, Any] = {
        "schema": "simplicio.task-spec/v2",
        "task_id": task_id,
        "source": {
            "kind": "simplicio-loop-task-contract",
            "locator": str(source.get("path") or "") or None,
            "encoding": "utf-8",
        },
        "source_hash": source_hash,
        "language": "unknown",
        "system": str(identity.get("system") or "") or None,
        "functionality": str(identity.get("feature") or "") or None,
        "task_type": str(identity.get("type") or "") or None,
        "narrative": {
            "persona": str(story.get("persona") or "") or None,
            "desire": str(story.get("desire") or "") or None,
            "value": str(story.get("value") or "") or None,
        },
        "acceptance_criteria": acceptance_criteria,
        "business_rules": [dict(item) for item in task.get("rules") or [] if isinstance(item, Mapping)],
        "non_functional_requirements": stateful_items(task.get("nfrs"), "NFR"),
        "prototypes": [dict(item) for item in task.get("prototypes") or [] if isinstance(item, Mapping)],
        "attachments": [],
        "navigation": ([{"id": "NAV1", "path": access_path, "original_text": access_path}]
                        if access_path else []),
        "dependencies": stateful_items(task.get("dependencies"), "DEP"),
        "impact_signals": dict(task.get("impact_signals") or {}),
        "additional_information": [
            {"id": f"INFO{index}", "text": str(item), "original_text": str(item)}
            for index, item in enumerate(task.get("additional_information") or [], start=1)
        ],
        "uncertainties": questions + assumptions + blockers,
        "human_gates": [dict(item) for item in questions],
        "verification_commands": [],
        "source_span": {},
        "original_text": original_text,
        # Additive field: preserves every Loop-only field and makes the handoff
        # auditable without teaching the Dev CLI private Loop schema.
        "loop_task_contract": json.loads(json.dumps(dict(task), ensure_ascii=False)),
    }
    return payload


def _task_spec_hash(payload: Mapping[str, Any]) -> str:
    """Return the Dev CLI canonical TaskSpec hash for receipt correlation."""
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _context_handoff_args(
    repo_path: Path,
    run_root: Path,
    *,
    attempt_id: str = "",
    lease_id: str = "",
    fencing_token: str = "",
    require_authorization: bool = False,
) -> Tuple[List[str], Dict[str, Any]]:
    """Project canonical Mapper context artifacts into the Dev CLI argv.

    The compact Mapper handoff is not a ContextSnapshot.  This helper only
    forwards explicitly supplied canonical artifacts and never derives a
    handle from a path or from ``mapper_context_hash``.  Missing artifacts are
    recorded as a diagnostic; the integrated Dev CLI remains the fail-closed
    owner of the final context gate.
    """
    authorization_args, authorization_handoff = prepare_authorization_handoff(
        run_root, required=require_authorization,
    )
    mapper_path = run_root / "mapper-context.json"
    if not mapper_path.exists():
        return list(authorization_args), {
            "status": "missing", "reason_code": "CONTEXT_ARTIFACTS_UNAVAILABLE",
            "authorization": authorization_handoff,
        }
    try:
        mapper = _load_json(mapper_path)
    except (OSError, ValueError):
        return list(authorization_args), {
            "status": "invalid", "reason_code": "CONTEXT_ARTIFACTS_INVALID",
            "authorization": authorization_handoff,
        }
    handoff = mapper.get("handoff") if isinstance(mapper.get("handoff"), Mapping) else {}
    stdout = handoff.get("stdout") if isinstance(handoff.get("stdout"), Mapping) else handoff
    if not isinstance(stdout, Mapping):
        stdout = {}

    def first_value(keys: Sequence[str]) -> Any:
        for container in (mapper, stdout):
            for key in keys:
                value = container.get(key) if isinstance(container, Mapping) else None
                if value not in (None, "", {}):
                    return value
        return None

    def persist_artifact(value: Any, filename: str) -> Path | None:
        if isinstance(value, Mapping):
            path = run_root / filename
            _write_json(path, dict(value))
            return path
        if not isinstance(value, str) or not value.strip():
            return None
        candidate = Path(value)
        if not candidate.is_absolute():
            for base in (repo_path, run_root):
                resolved = (base / candidate).resolve()
                if resolved.exists():
                    return resolved
            return None
        return candidate.resolve() if candidate.exists() else None

    snapshot_path = persist_artifact(
        first_value(("context_snapshot", "canonical_context_snapshot", "context_snapshot_path")),
        "context-snapshot.json",
    )
    pack_path = persist_artifact(
        first_value(("canonical_context_pack", "context_pack_path")),
        "context-pack.json",
    )
    execution_path = persist_artifact(
        first_value(("execution_context", "canonical_execution_context", "execution_context_path")),
        "execution-context.json",
    )
    context_handle = first_value(("context_handle", "canonical_context_handle"))
    if not all((snapshot_path, pack_path, execution_path, context_handle)):
        return list(authorization_args), {
            "status": "missing",
            "reason_code": "CONTEXT_ARTIFACTS_INCOMPLETE",
            "snapshot": bool(snapshot_path),
            "pack": bool(pack_path),
            "execution_context": bool(execution_path),
            "context_handle": bool(context_handle),
            "authorization": authorization_handoff,
        }
    args = [
        "--context-snapshot", str(snapshot_path),
        "--context-pack", str(pack_path),
        "--execution-context", str(execution_path),
        "--context-handle", str(context_handle),
    ]
    if all(value.strip() for value in (attempt_id, lease_id, fencing_token)):
        args.extend([
            "--attempt-id", attempt_id,
            "--lease-id", lease_id,
            "--fencing-token", fencing_token,
        ])
    args.extend(authorization_args)
    return args, {
        "status": "propagated",
        "context_handle": str(context_handle),
        "snapshot_path": str(snapshot_path),
        "pack_path": str(pack_path),
        "execution_context_path": str(execution_path),
        "authorization": authorization_handoff,
    }


def _auto_fan_out_enabled() -> bool:
    """Return whether batch execution may provision isolated workers automatically.

    Fan-out is the safe default for independent tasks.  Operators can explicitly opt out
    with ``SIMPLICIO_LOOP_AUTO_FAN_OUT=0`` when a repository cannot create worktrees (for
    example, a read-only checkout); the ordinary shared-run serial guard remains active in
    either mode.
    """
    raw = os.environ.get("SIMPLICIO_LOOP_AUTO_FAN_OUT", "1").strip().lower()
    return raw not in {"0", "false", "no", "off", "disabled"}


def _guarded_dispatch_enabled() -> bool:
    """Opt-in gate (issue #288) for threading ``AttemptCoordinator.run_guarded`` through the
    real operator dispatch path instead of a raw, unguarded ``subprocess.run``.

    Off by default -- following the same pattern as ``SIMPLICIO_REQUIRE_MUTATION_AUTHORITY``
    in #284's ``planning_gate.py`` wiring -- so existing callers/fixtures that pass a
    distributed queue without the fuller identity/heartbeat contract are unaffected. Set
    ``SIMPLICIO_GUARDED_DISPATCH=1`` to require a heartbeat-guarded, lease-fenced attempt for
    every distributed-queue dispatch (a real worker whose lease is stolen mid-mutation is
    killed and reported as ``lease_lost_during_execution`` instead of finishing unguarded).
    """
    return str(os.environ.get("SIMPLICIO_GUARDED_DISPATCH") or "").strip().lower() in ("1", "true", "yes")


def _auto_merge_enabled() -> bool:
    """Opt-in gate (issue #288) for calling ``MergeExecutor`` for real once a dispatch
    attempt's receipt pair is ``VERIFIED``.

    Off by default: creating/merging a real PR is a side effect with real consequences (an
    actual GitHub API call, a real merge), so it must be explicitly requested via
    ``SIMPLICIO_AUTO_MERGE_PR=1`` plus a resolvable repo slug
    (``SIMPLICIO_REMOTE_REPO``/``GITHUB_REPOSITORY``) and a worktree branch on the item -- any
    of those missing is reported as ``attempted: False`` rather than silently skipped.
    """
    return str(os.environ.get("SIMPLICIO_AUTO_MERGE_PR") or "").strip().lower() in ("1", "true", "yes")


def _merge_repo_slug() -> str:
    return str(os.environ.get("SIMPLICIO_REMOTE_REPO") or os.environ.get("GITHUB_REPOSITORY") or "").strip()


def _dispatch_merge_pr(item: Mapping[str, Any], *, receipt: str, run_id: str) -> Dict[str, Any]:
    """Create/poll/merge the PR for a claimed item's worktree branch and reconcile the merge
    against the remote (issue #288).

    Formalizes the ad-hoc ``gh pr create`` / ``gh pr merge --squash --delete-branch`` pattern
    this project's own delivery workflow already performs by hand at the end of every task
    (CLAUDE.md / AGENTS.md "Process" sections) as a real, reusable call instead of prose an
    operator must remember. Never raises for an ordinary "cannot merge yet/here" outcome --
    those come back as ``attempted: True, merged: False`` with a specific reason so a caller
    can retry or escalate; only a hard `gh` transport failure surfaces as an error field.
    """
    context = item.get("worktree_context") or {}
    branch = str(context.get("branch") or "").strip()
    repo_slug = _merge_repo_slug()
    if not branch or not repo_slug:
        return {"attempted": False, "reason": "missing_branch_or_repo_slug", "merged": False}
    base = str(os.environ.get("SIMPLICIO_MERGE_BASE") or "main").strip()
    task_id = str(item.get("task_id") or "")
    title = ("simplicio-loop: %s" % task_id) if task_id else "simplicio-loop: automated delivery"
    body = ("Automated delivery for work item `%s` (run `%s`).\n\nOperator receipt: `%s`\n"
            % (task_id, run_id, receipt))
    try:
        executor = MergeExecutor(repo=repo_slug)
        pr = executor.ensure_pr(branch=branch, base=base, title=title, body=body)
        pr_number = int(pr.get("number") or 0)
        if not pr_number:
            return {"attempted": True, "merged": False, "reason_code": "NO_PR_NUMBER",
                    "detail": "ensure_pr did not resolve a PR number", "pr": pr}
        result = executor.merge(pr_number)
        return {"attempted": True, "pr": pr, **result.to_dict()}
    except MergeExecutorError as exc:
        return {"attempted": True, "merged": False, "reconciled": False,
                "reason_code": exc.reason_code, "detail": str(exc)}


def _model_routed_dispatch_enabled() -> bool:
    """Opt-in gate (issue #287) for threading ``model_router.route()``'s selection
    through the real dispatch path instead of a hardcoded runtime.

    Off by default -- following the same pattern as ``SIMPLICIO_GUARDED_DISPATCH``
    (#288) and ``SIMPLICIO_AUTO_MERGE_PR`` (#288) above -- so existing callers/fixtures
    that dispatch without a model registry configured are unaffected. Set
    ``SIMPLICIO_MODEL_ROUTED_DISPATCH=1`` to compute a real routing-decision-receipt
    for every dispatch attempt and, when a real ``CodexRuntimeDriver``/
    ``ClaudeRuntimeDriver`` is wired for the selected runtime, genuinely invoke it and
    persist a ``runtime-execution-receipt`` alongside the operator's own receipts. A
    routing block or driver failure never blocks the underlying dev-cli operator
    mutation this repo already performs -- this is additional, real audit evidence
    layered on top of it, not a replacement for the operator contract.
    """
    return str(os.environ.get("SIMPLICIO_MODEL_ROUTED_DISPATCH") or "").strip().lower() in ("1", "true", "yes")


def _verified_delivery_gate_enabled() -> bool:
    """Opt-in gate (issue #288) for routing a dispatch attempt's completion decision through
    the real ``LoopRuntimeAdapter``/``VerifiedAgentDelivery``/``ExecutionBoard`` evidence +
    watcher + delivery contract instead of the bare ``execution_state == "applied"`` check.

    ``LoopRuntimeAdapter`` and ``VerifiedAgentDelivery`` are real, fully tested classes
    (``simplicio_loop/runtime_adapter.py``, ``verified_delivery.py``) but had zero references
    in the dispatch path -- the #288 audit named this the highest-value remaining gap: an
    attempt could be reported ``succeeded`` on ``execution_state == "applied"`` alone, with no
    fresh COMPLETE evidence receipt, no measured watcher pass, and no recorded delivery
    convergence actually required. Off by default -- following the same pattern as
    ``SIMPLICIO_GUARDED_DISPATCH``/``SIMPLICIO_AUTO_MERGE_PR`` above -- so existing
    callers/fixtures that dispatch without a watcher run are unaffected. Set
    ``SIMPLICIO_VERIFIED_DELIVERY_GATE=1`` to demote a dispatch attempt whose evidence pair,
    watcher, or delivery gate is not genuinely satisfied from ``succeeded`` to ``failed``,
    even when the underlying dev-cli operator itself applied cleanly.
    """
    return str(os.environ.get("SIMPLICIO_VERIFIED_DELIVERY_GATE") or "").strip().lower() in ("1", "true", "yes")


def _run_verified_delivery_gate(
    *, run_id: str, task_id: str, actor: str, attempt_id: str,
    receipt_verdict: Mapping[str, Any], evidence_receipt: str, watcher_receipt: str,
    merge: Optional[Mapping[str, Any]], worktree_context: Mapping[str, Any],
) -> Dict[str, Any]:
    """Drive the real evidence+watcher+delivery gated completion check (issue #288) for one
    dispatch attempt.

    Never raises: a failed gate comes back as ``verified: False`` with a ``reason`` so the
    caller can demote ``succeeded`` to ``failed`` without crashing the scheduler. This is a
    strict superset of the pre-existing ``execution_state == "applied"`` check -- it can only
    turn a would-be success into a failure, never the reverse.
    """
    schema = "simplicio.verified-delivery-gate/v1"
    try:
        if receipt_verdict.get("status") != ReceiptStatus.VERIFIED:
            return {"schema": schema, "verified": False, "status": "UNVERIFIED",
                    "reason": "operator/evidence receipt pair is not VERIFIED"}
        watcher_state: Dict[str, Any] = {}
        if watcher_receipt and Path(watcher_receipt).exists():
            try:
                watcher_state = json.loads(Path(watcher_receipt).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                watcher_state = {}
        if watcher_state.get("status") != "MEASURED" or not watcher_state.get("match"):
            return {"schema": schema, "verified": False, "status": "UNVERIFIED",
                    "reason": "no measured watcher pass recorded for this attempt"}
        runtime = LoopRuntimeAdapter(run_id=run_id, work_item_id=task_id, actor=actor or "loop",
                                     standalone=True)
        runtime.negotiate()
        board = ExecutionBoard(run_id=run_id)
        delivery = VerifiedAgentDelivery(runtime=runtime, board=board, attempt_id=attempt_id)
        for phase in ("intake", "mapping", "planning", "executing", "validating", "watching", "delivering"):
            delivery.transition(phase)
        evidence_payload = {"schema": "simplicio.ac-evidence/v1", "status": "PASS", "ready": True,
                            "verdict": "COMPLETE", "receipt_id": evidence_receipt or attempt_id}
        delivery.record_evidence(evidence_payload)
        challenge = str(watcher_state.get("challenge") or watcher_receipt)
        delivery.record_watcher(match=True, challenge=challenge)
        merge_info = dict(merge or {})
        if merge_info.get("merged"):
            delivery_payload = {
                "target": "merge-queue", "satisfied": True,
                "merge_queue": {
                    "receipt_sha": str(merge_info.get("merge_commit_sha") or ""),
                    "status": "accepted",
                    "branch": str(worktree_context.get("branch") or ""),
                    "worktree_path": str(worktree_context.get("worktree_path")
                                        or worktree_context.get("path") or ""),
                },
            }
        else:
            delivery_payload = {"target": "local-fixture", "satisfied": True}
        delivery.record_delivery(delivery_payload)
        result = delivery.complete(evidence_payload)
        projection = board.replay()
        return {"schema": schema, "verified": True, "status": "VERIFIED",
                "board_status": projection.get("status"), "delivery": result.get("delivery")}
    except (VerifiedDeliveryError, RuntimeAdapterError) as exc:
        return {"schema": schema, "verified": False, "status": "UNVERIFIED", "reason": str(exc)}


_DEFAULT_MODEL_REGISTRY_ENTRIES: Tuple[Dict[str, Any], ...] = (
    {
        "runtime": "codex", "provider": "openai", "model_id": "codex-cli/gpt-5.6-luna",
        "aliases": ["codex-cli"], "capabilities": ["execute", "review"],
        "probe": {"kind": "codex-cli", "target": "codex"},
    },
    {
        "runtime": "claude", "provider": "anthropic", "model_id": "claude-code/sonnet-5",
        "aliases": ["claude-code"], "capabilities": ["execute", "review"],
        "probe": {"kind": "claude-cli", "target": "claude"},
    },
)


def _default_model_registry() -> ModelCapabilityRegistry:
    """Build the standard two-runtime (Codex + Claude) registry, wired to the real
    ``--version`` probes in ``runtime_drivers.py`` -- never a fabricated availability
    check. A caller that needs a different registry shape (e.g. a config file) can
    still build/pass its own ``ModelCapabilityRegistry``; this is only the default
    used by the opt-in dispatch wiring below.
    """
    return ModelCapabilityRegistry(_DEFAULT_MODEL_REGISTRY_ENTRIES, probe_hooks=CLI_PROBE_HOOKS)


def _route_runtime_for_item(item: Mapping[str, Any], *, role: str = "executor",
                             registry: Optional[ModelCapabilityRegistry] = None) -> Dict[str, Any]:
    """Compute one real ``routing-decision-receipt`` for a dispatch attempt.

    Never raises for an ordinary routing block (no eligible candidate, e.g. neither
    CLI installed) -- that comes back as a receipt with ``blocked=True`` and an
    explicit ``block_reason`` so a caller can record/report it; only malformed input
    surfaces as ``ModelRouterError``/``ModelRegistryError``.
    """
    registry = registry or _default_model_registry()
    requirements = {"role": role, "required_capabilities": ["execute"]}
    return _model_route(requirements, registry)


def _execute_routed_runtime(item: Mapping[str, Any], run_dir: Path, *,
                             registry: Optional[ModelCapabilityRegistry] = None) -> Dict[str, Any]:
    """Route + (when a real driver is wired for the selection) genuinely execute one
    LLM-runtime attempt for this dispatch, persisting both receipts under
    ``run_dir/loop/`` for audit.

    This never fabricates execution: when routing is blocked (no eligible
    candidate) or no real driver exists for the selected runtime, the returned
    summary says so explicitly (``executed: False``) rather than skipping silently
    or pretending a result. A driver invocation failure (missing binary, auth/policy
    block, timeout) is itself a genuine, honestly-reported outcome -- captured in the
    persisted ``runtime-execution-receipt`` exactly as observed.
    """
    summary: Dict[str, Any] = {
        "routed": False, "executed": False,
        "routing_decision_receipt": "", "runtime_execution_receipt": "",
    }
    try:
        routing_receipt = _route_runtime_for_item(item, registry=registry)
    except (ModelRouterError, ModelRegistryError) as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        return summary
    summary["routed"] = True
    loop_dir = run_dir / "loop"
    loop_dir.mkdir(parents=True, exist_ok=True)
    routing_path = loop_dir / "routing-decision-receipt.json"
    _write_json(routing_path, routing_receipt)
    summary["routing_decision_receipt"] = str(routing_path)
    summary["selected"] = routing_receipt.get("selected")
    summary["blocked"] = bool(routing_receipt.get("blocked"))
    if routing_receipt.get("blocked") or not routing_receipt.get("selected"):
        summary["block_reason"] = str(routing_receipt.get("block_reason") or "")
        return summary
    selected = routing_receipt["selected"]
    driver = driver_for_runtime(selected.get("runtime"))
    if driver is None:
        summary["reason"] = f"no real driver wired for runtime {selected.get('runtime')!r}"
        return summary
    context_pack = item.get("context_pack") if isinstance(item.get("context_pack"), Mapping) else {}
    goal = str(context_pack.get("goal") or item.get("task_id") or "").strip()
    if not goal:
        summary["reason"] = "no task goal text available to prompt the runtime"
        return summary
    repo_path = Path(str(item.get("repo") or "."))
    context_request: Optional[RuntimeContextRequest] = None
    if all(context_pack.get(key) for key in (
        "mapper_envelope_hash", "plan_hash", "authorized_targets", "target",
    )):
        try:
            context_request = RuntimeContextRequest(
                goal=goal,
                acceptance_criteria=tuple(context_pack.get("acs") or context_pack.get("acceptance_criteria") or ()),
                source_refs=tuple(context_pack.get("source_refs") or ()),
                verification_routes=tuple(context_pack.get("verification_routes") or ()),
                graph_evidence=tuple(context_pack.get("graph_evidence") or ()),
                trusted_constraints=tuple(context_pack.get("trusted_constraints") or ()),
                untrusted_evidence=tuple(context_pack.get("untrusted_evidence") or ()),
                authorized_targets=tuple(context_pack.get("authorized_targets") or ()),
                target=str(context_pack.get("target") or ""),
                remaining_budget_tokens=int(context_pack.get("remaining_budget_tokens") or 0),
                mapper_envelope_hash=str(context_pack.get("mapper_envelope_hash") or ""),
                plan_hash=str(context_pack.get("plan_hash") or ""),
            )
            result = driver.execute_context(
                context_request, cwd=repo_path if repo_path.exists() else None,
                expected_mapper_envelope_hash=str(context_pack.get("mapper_envelope_hash")),
                expected_plan_hash=str(context_pack.get("plan_hash")),
            )
        except (ContextAuthorizationError, ContextBudgetError, TypeError, ValueError) as exc:
            summary["error"] = f"RuntimeContextError: {exc}"
            return summary
    else:
        result = driver.execute(goal, cwd=repo_path if repo_path.exists() else None)
    base_sha = ""
    head_sha = ""
    changed: List[str] = []
    if repo_path.exists():
        fingerprint = _repo_fingerprint(repo_path)
        base_sha = head_sha = str(fingerprint.get("head") or "")
        try:
            changed = _changed_paths(repo_path)
        except Exception:
            changed = []
    try:
        execution_receipt = driver.build_receipt(
            route_id=hashlib.sha256(json.dumps(routing_receipt, sort_keys=True).encode("utf-8")).hexdigest()[:16],
            requested={"runtime": selected.get("runtime"), "provider": selected.get("provider"),
                       "model_id": selected.get("model_id"), "verified": True},
            session={
                "worker_id": str(item.get("worker_id") or ""),
                "device_id": os.environ.get("SIMPLICIO_DEVICE_ID", ""),
                "attempt_id": str(item.get("task_index") or ""),
                "lease_id": "", "fence_token": "",
            },
            result=result,
            tree={"base_sha": base_sha, "head_sha": head_sha, "changed_paths": changed},
            evidence_refs=(
                ["runtime-context:" + context_request.request_hash,
                 "mapper-envelope:" + context_request.mapper_envelope_hash,
                 "plan:" + context_request.plan_hash]
                if context_request is not None else None
            ),
        )
    except RuntimeExecutionReceiptError as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        return summary
    execution_path = loop_dir / "runtime-execution-receipt.json"
    _write_json(execution_path, execution_receipt)
    summary["executed"] = True
    summary["runtime_execution_receipt"] = str(execution_path)
    summary["execution_ok"] = bool(result.ok)
    summary["execution_stop_reason"] = result.stop_reason
    summary["execution_error"] = result.error
    return summary


def _auto_worktree_dispatch(
    repo: str,
    run_id: str,
    contract: Mapping[str, Any],
    plan: Mapping[str, Any],
    indices: Sequence[int],
) -> Tuple[Any, Dict[int, Dict[str, Any]], str]:
    """Build an isolated queue for a default batch when task impact is independent.

    This helper intentionally fails closed: missing plan targets, a non-git checkout, an
    overlapping impact key, or a queue allocation error all leave the caller with the
    existing shared-run serial path.  It never claims parallel execution without distinct
    worktree contexts.
    """
    if not _auto_fan_out_enabled() or len(indices) < 2:
        return None, {}, "auto_fan_out_disabled" if not _auto_fan_out_enabled() else "single_task"
    root = Path(repo).resolve()
    if not (root / ".git").exists():
        return None, {}, "not_git_checkout"
    try:
        from scripts.worktree_queue import TaskSpec, WorktreeQueue
    except ImportError:  # pragma: no cover - installed bundle without optional adapter
        try:
            from worktree_queue import TaskSpec, WorktreeQueue
        except ImportError:
            return None, {}, "worktree_adapter_unavailable"

    tasks = list(contract.get("tasks") or [])
    steps = list(plan.get("steps") or [])
    specs = []
    contexts: Dict[int, Dict[str, Any]] = {}
    for index in indices:
        if index > len(tasks) or index > len(steps):
            return None, {}, "plan_task_mismatch"
        step = steps[index - 1] if isinstance(steps[index - 1], Mapping) else {}
        targets = [str(value) for value in (step.get("candidate_targets") or []) if str(value).strip()]
        # A worktree without an authorized target cannot be executed; serial fallback gives
        # the caller the same clear preflight failure instead of manufacturing a lane.
        if not targets:
            return None, {}, "missing_plan_targets"
        task_id = f"{run_id}-task-{index}"
        specs.append(TaskSpec(id=task_id, goal=_task_goal(tasks[index - 1]), files_affected=targets))
    graph = WorktreeQueue.conflict_graph(specs)
    if any(graph.values()):
        return None, {}, "overlapping_task_impacts"
    try:
        queue = WorktreeQueue(
            repo_root=str(root),
            run_id=run_id,
            state_path=str(root / ".simplicio" / "loop-runs" / run_id / "worktree-queue.json"),
            worktree_root=str(root / ".simplicio" / "loop-worktrees" / run_id),
        )
        # Registration is an explicit preflight gate.  Allocation happens inside the
        # dispatcher, before any worker starts, and is persisted by the queue.
        queue.register_tasks(specs)
    except Exception:
        return None, {}, "worktree_preflight_failed"
    for index, spec in zip(indices, specs):
        contexts[index] = {
            "task_id": spec.id,
            "task_spec": {
                "id": spec.id,
                "goal": spec.goal,
                "files_affected": list(spec.files_affected),
            },
            "isolation": "worktree",
            "isolation_key": spec.id,
        }
    return queue, contexts, ""


def _write_scratchpad(loop_dir: Path, goal: str, max_iterations: int, promise: str) -> None:
    body = "\n".join(
        [
            "---",
            "iteration: 1",
            f"max_iterations: {max_iterations}",
            f'completion_promise: "{promise}"',
            "evidence_required: true",
            "mode: converge",
            f'started_at: "{_now()}"',
            "---",
            "",
            goal,
            "",
        ]
    )
    (loop_dir / "scratchpad.md").write_text(body, encoding="utf-8")


def _write_watcher_challenge(loop_dir: Path, goal_fp: str) -> None:
    payload = {
        "challenge": f"wch-{_rand_token(12)}",
        "iteration": 1,
        "goal_fp": goal_fp,
        "written_at": _now(),
    }
    _write_json(loop_dir / "watcher_challenge.json", payload)


def _transition(run_dir: Path, state: Dict[str, Any], to_phase: str, reason: str,
                receipt: str = "", extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if to_phase not in PHASES:
        raise ValueError(f"invalid phase {to_phase!r}")
    entry = {
        "ts": _now(),
        "from": state.get("phase"),
        "to": to_phase,
        "reason": reason,
        "receipt": receipt,
    }
    if extra:
        entry["extra"] = extra
    history = state.setdefault("history", [])
    history.append(entry)
    state["phase"] = to_phase
    state["updated_at"] = entry["ts"]
    _write_json(run_dir / "state.json", state)
    _append_jsonl(run_dir / "transitions.jsonl", entry)
    _record_event(run_dir, state, {
        "phase": "phase_transition",
        "to_phase": to_phase,
        "from_phase": entry["from"],
        "reason": reason,
        "receipt": receipt,
    }, transition_extra=extra)
    return state


# Canonical phase-event kinds consumed by simplicio_loop.progress.build_progress (#181).
_PHASE_EVENT_KINDS = {
    "intake", "mapping", "planning", "executing", "validating",
    "watching", "delivering", "done", "partial", "blocked", "cancelled",
    "awaiting_decision", "mapper_fresh", "watcher_challenge", "operator_receipt",
    "worker_claimed", "worktree_created", "test_gate", "completion_verdict",
}


def _record_event(run_dir: Path, state: Dict[str, Any], event: Dict[str, Any],
                  transition_extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Append one normalized progress event to ``state['events']`` (#181).

    Every loop stage emits these so external dashboards and LLMs can render
    real per-stage progress (see ``docs/PROGRESS_PROTOCOL.md``).  ``progress.py``
    already normalizes and renders ``state['events']``; previously the runner
    never populated it.
    """
    event = dict(event)
    event.setdefault("event_id", "evt-" + hashlib.sha256(
        (json.dumps(event, sort_keys=True, ensure_ascii=False) + _now()).encode("utf-8")
    ).hexdigest()[:12])
    event.setdefault("ts", _now())
    event.setdefault("run_id", state.get("run_id", ""))
    event.setdefault("phase", state.get("phase", ""))
    task_ids = state.get("task_ids") or []
    ac_ids = state.get("ac_ids") or []
    if not event.get("task_id") and task_ids:
        event["task_id"] = task_ids[0]
    if not event.get("ac_ids") and ac_ids:
        event["ac_ids"] = list(ac_ids)
    if not event.get("receipt") and not event.get("blocker"):
        event["blocker"] = event.get("reason") or event.get("message") or ""
    kind = event.get("kind") or event.get("phase")
    if kind in _PHASE_EVENT_KINDS and "kind" not in event:
        event["kind"] = kind
    if transition_extra and "extra" not in event:
        event["extra"] = transition_extra
    events = state.setdefault("events", [])
    events.append(event)
    state["updated_at"] = event["ts"]
    _write_json(run_dir / "state.json", state)
    _append_jsonl(run_dir / "events.jsonl", event)
    _sync_github_lifecycle(run_dir, state, event)
    _sync_orca_lifecycle(run_dir, state, event)
    return state


def _sync_orca_lifecycle(run_dir: Path, state: Dict[str, Any], event: Dict[str, Any]) -> None:
    """Project the same lifecycle event onto the active Orca Dev card.

    Orca is optional and host-local: a missing Orca context is recorded as a
    typed skip rather than treated as a failed delivery or a reason to touch a
    different worktree.
    """
    lifecycle_state = _github_lifecycle.lifecycle_state_for_phase_event(
        str(event.get("kind") or event.get("phase") or ""))
    if not lifecycle_state:
        return
    try:
        receipt = sync_orca_status(
            state, {**event, "lifecycle_state": lifecycle_state},
        )
        _append_jsonl(run_dir / "orca-sync.jsonl", {
            "run_id": str(state.get("run_id") or ""),
            "event": str(event.get("kind") or event.get("phase") or ""),
            **receipt,
        })
    except Exception as exc:  # noqa: BLE001 -- optional host integration is fail-open
        try:
            _append_jsonl(run_dir / "orca-sync-errors.jsonl", {
                "run_id": str(state.get("run_id") or ""), "error": str(exc),
            })
        except Exception:
            pass


def _github_source_adapter(owner: str, repo: str, *, publish_comment_fn: Callable,
                           outbox_dir: Optional[str | Path] = None) -> GitHubSourceAdapter:
    """One construction point for the #285 `GitHubSourceAdapter` binding runner.py uses,
    so every runner call site goes through the `SourceAdapter` Protocol surface instead
    of calling `github_lifecycle.py`'s free functions directly. Same defaults
    (``subprocess.run``, 20s timeout) `github_lifecycle.publish_lifecycle_state()` itself
    defaults to -- this is a binding, not a behavior change.
    """
    return GitHubSourceAdapter(owner, repo, publish_comment_fn=publish_comment_fn, outbox_dir=outbox_dir)


def _sync_github_lifecycle(run_dir: Path, state: Dict[str, Any], event: Dict[str, Any]) -> None:
    """Project one phase event onto the #285 GitHub lifecycle canonical comment.

    Best-effort and fail-open, exactly like the existing `pr_evidence.py
    progress-comment` command it complements: enabled whenever the run state
    carries a ``source_issue`` dict (``{"owner": ..., "repo": ..., "issue": ...}``).
    ``SIMPLICIO_LOOP_GITHUB_LIFECYCLE_SYNC=0`` (or another explicit falsy value)
    is the temporary legacy opt-out; leaving it unset keeps GitHub coordination on.
    Any
    failure (no `gh`, no network, transport error, import error) is logged to
    ``lifecycle-sync-errors.jsonl`` under the run directory and swallowed -- this
    sync must never abort or fail the run. It only ever handles the intermediate
    lifecycle projection (CLAIMED/PLANNED/IN_PROGRESS/...); the authoritative,
    fail-closed close operation is
    :func:`simplicio_loop.github_lifecycle.close_source_issue`, invoked explicitly at
    completion time by the caller that owns that decision, never automatically from
    this generic per-event hook.
    """
    if str(os.environ.get("SIMPLICIO_LOOP_GITHUB_LIFECYCLE_SYNC") or "").strip().lower() in (
        "0", "false", "no", "off", "legacy",
    ):
        return
    source_issue = state.get("source_issue") or {}
    owner, repo, issue = source_issue.get("owner"), source_issue.get("repo"), source_issue.get("issue")
    if not (owner and repo and issue):
        return
    lifecycle_state = _github_lifecycle.lifecycle_state_for_phase_event(
        str(event.get("kind") or event.get("phase") or ""))
    if not lifecycle_state:
        return
    try:
        scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from pr_evidence import publish_comment as _publish_comment  # local import: optional dep

        # #285 remaining gap: project the run's real identity/runtime/device/lease/
        # branch onto the rendered comment instead of leaving those fields blank even
        # though `render_lifecycle_comment` supports them. `event` wins over derived
        # defaults when the emitting call site already knows its lease/branch (e.g.
        # `execute_operator()`'s guarded dispatch, which has a real `WorkItemAttempt`
        # lease and worktree branch on hand); otherwise fall back to a best-effort
        # local identity/branch lookup so a plain sequential run is not blank either.
        repo_path = _run_repo_path(run_dir)
        identity = _dispatch_identity_fields(repo_path)
        lease = state.get("lease") if isinstance(state.get("lease"), Mapping) else {}
        lease_id = str(event.get("lease_id") or lease.get("lease_id") or "")
        fencing_token = str(event.get("fencing_token") or lease.get("fencing_token") or "")
        branch = str(
            event.get("branch") or state.get("branch")
            or (_git_current_branch(repo_path) if repo_path is not None else "")
        )
        worktree = str(event.get("worktree_path") or state.get("worktree_path") or "")

        # #285 remaining gap: go through the `GitHubSourceAdapter` Protocol binding
        # instead of calling `github_lifecycle.publish_lifecycle_state()` directly --
        # same underlying call (no behavior change), but now expressed through the
        # single adapter surface every source (GitHub or otherwise) is meant to plug
        # into.
        adapter = _github_source_adapter(str(owner), str(repo), publish_comment_fn=_publish_comment)
        receipt = adapter.update_status(
            str(issue), lifecycle_state,
            run_id=str(state.get("run_id") or ""),
            attempt_id=str(event.get("task_id") or state.get("run_id") or ""),
            fencing_token=fencing_token,
            progress=str(event.get("message") or ""),
            agent_id=identity.get("agent_id", ""),
            runtime=identity.get("runtime", ""),
            device=identity.get("device", ""),
            lease_id=lease_id,
            branch=branch,
            worktree=worktree,
        )
        # Persist the receipt into the run dir so the completion oracle (#285's remaining gap:
        # "CLOSE_PENDING_RECONCILIATION" must actually gate COMPLETE, not sit inert) can see it.
        # This hook only ever projects intermediate states; a genuine
        # CLOSE_PENDING_RECONCILIATION comes from the explicit `close_source_issue` call, which
        # persists its own receipt the same way -- see `simplicio_loop/oracle.py`.
        _github_lifecycle.persist_lifecycle_receipt(receipt, run_dir)
    except Exception as exc:  # noqa: BLE001 -- best-effort sync, never blocks the loop
        try:
            _append_jsonl(run_dir / "lifecycle-sync-errors.jsonl",
                         {"ts": _now(), "kind": event.get("kind"), "error": str(exc)})
        except Exception:
            pass


def _maybe_auto_build_planning_receipt(
    run_root: Path, state: Dict[str, Any], run_id: str,
    contract: Dict[str, Any], plan: Dict[str, Any], plan_validation: Dict[str, Any],
    repo_path: Optional[Path] = None,
) -> None:
    """#284 remaining gap: wire ``planning_gate.build_planning_receipt()`` into the
    REAL ``arm_run()`` dispatch path so the mutation-authority gate in
    ``execute_operator()``/``execute_operator_batch()`` is self-sufficient, instead
    of only ever being satisfiable by a caller remembering to run the separate
    ``scripts/planning_gate.py build`` CLI first.

    Mandatory-by-default via ``planning_gate.auto_planning_receipt_enabled()`` --
    the same polarity-flip pattern ``mutation_authority_required()`` used for
    ``SIMPLICIO_REQUIRE_MUTATION_AUTHORITY`` (#284/#360). Unset/blank now means
    ON: every real ``arm_run()`` dispatch self-builds a matching
    ``planning-receipt.json`` so ``execute_operator()``/``execute_operator_batch()``
    are self-sufficient, instead of only ever being satisfiable by a caller
    remembering to run the separate ``scripts/planning_gate.py build`` CLI first.
    A caller that truly needs the legacy opt-in posture (or a test asserting the
    missing-receipt fail-closed path) sets ``SIMPLICIO_LOOP_AUTO_PLANNING_RECEIPT``
    to an explicit falsy value (``0/false/no/off/legacy``); see
    ``tests/planning_gate_fixtures.py`` and
    ``docs/adr/0004-planning-gate-rollout.md`` for the rollout/migration strategy.

    When a GitHub ``source_issue`` is present on the run state AND
    ``SIMPLICIO_LOOP_GITHUB_LIFECYCLE_SYNC`` is also enabled, this additionally
    captures a fresh source snapshot (folding it into the mutation-authority
    identity so a later source edit invalidates the authority) and publishes the
    resulting receipt as PLANNED/BLOCKED on the canonical GitHub comment via
    ``planning_gate.publish_planning_receipt()`` -- the #284-specific projection,
    distinct from (and complementary to) the generic per-phase-event sync
    ``_sync_github_lifecycle()`` already performs for CLAIMED/DISCOVERED/etc.

    Best-effort and fail-open: any failure here (bad `gh` auth, no network, import
    error) is logged to ``lifecycle-sync-errors.jsonl`` and swallowed, exactly like
    ``_sync_github_lifecycle()`` -- this must never abort or fail the run.
    """
    if not auto_planning_receipt_enabled():
        return
    try:
        attempt = int((state or {}).get("attempts", 0)) + 1
        source_snapshot = None
        source_issue = (state or {}).get("source_issue") or {}
        owner, repo_name, issue = source_issue.get("owner"), source_issue.get("repo"), source_issue.get("issue")
        lifecycle_sync_on = str(os.environ.get("SIMPLICIO_LOOP_GITHUB_LIFECYCLE_SYNC") or "").strip().lower() not in (
            "0", "false", "no", "off", "legacy",
        )
        if lifecycle_sync_on and owner and repo_name and issue:
            try:
                from .source_snapshot import capture_github_issue_snapshot
                source_snapshot = capture_github_issue_snapshot(f"{owner}/{repo_name}", str(issue))
            except Exception:
                source_snapshot = None
        receipt = _build_planning_receipt(
            run_id=run_id, attempt=attempt, contract=contract, plan=plan,
            plan_validation=plan_validation, source_snapshot=source_snapshot,
        )
        (run_root / "planning-receipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        if source_snapshot is not None and lifecycle_sync_on:
            scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
            from pr_evidence import publish_comment as _publish_comment  # local import: optional dep

            # #285 remaining gap: project real identity/runtime/device/branch/plan onto
            # the PLANNED comment instead of leaving those fields blank, the same
            # projection `_sync_github_lifecycle()` now performs for CLAIMED/etc. No
            # lease/fencing token exists yet at this point in `arm_run()` (it is minted
            # only when a distributed claim happens later), so that field is left blank
            # here rather than fabricated.
            identity = _dispatch_identity_fields(repo_path)
            branch = _git_current_branch(repo_path) if repo_path is not None else ""
            plan_steps = [
                str(step.get("description") or step.get("goal") or step.get("id") or "").strip()
                for step in (plan.get("steps") or [])
                if isinstance(step, Mapping)
                and str(step.get("description") or step.get("goal") or step.get("id") or "").strip()
            ]
            lifecycle_receipt = _publish_planning_receipt(
                receipt, publish_comment_fn=_publish_comment,
                agent_id=identity.get("agent_id", ""),
                runtime=identity.get("runtime", ""),
                device=identity.get("device", ""),
                branch=branch,
                plan_steps=plan_steps,
            )
            if lifecycle_receipt is not None:
                _github_lifecycle.persist_lifecycle_receipt(lifecycle_receipt, run_root)
    except Exception as exc:  # noqa: BLE001 -- best-effort, never blocks the run
        try:
            _append_jsonl(run_root / "lifecycle-sync-errors.jsonl",
                         {"ts": _now(), "kind": "planning_receipt_auto_build", "error": str(exc)})
        except Exception:
            pass


def _emit_event(run_dir: Path, state: Dict[str, Any], kind: str, *,
                receipt: str = "", blocker: str = "", message: str = "",
                **extra: Any) -> Dict[str, Any]:
    """Emit one named visual event with the run's canonical provenance."""
    event: Dict[str, Any] = {"kind": kind, "receipt": receipt, "blocker": blocker,
                             "message": message}
    event.update(extra)
    return _record_event(run_dir, state, event)


def _task_ac_ids(task: Mapping[str, Any]) -> List[str]:
    """Return acceptance-criterion/scenario IDs from a compiled task."""
    return [str(item.get("id") or "") for item in (task.get("scenarios") or [])
            if isinstance(item, Mapping) and item.get("id")]


def _recoverable_operator_error(tool: str, exc: BaseException) -> bool:
    """Return True only for operator installation/version/capability failures."""
    if isinstance(exc, FileNotFoundError):
        missing = str(getattr(exc, "filename", "") or "")
        return Path(missing).name == tool or tool.lower() in str(exc).lower()
    message = str(exc or "").lower()
    if tool.lower() not in message:
        return False
    return any(marker in message for marker in (
        "no such file or directory",
        "unavailable",
        "below minimum version",
        "missing required capabilities",
        "version probe failed",
    ))


def _run_with_operator_recovery(
    tool: str,
    run_root: Path,
    operation: Callable[[], Dict[str, Any]],
) -> Dict[str, Any]:
    """Run one operator step, bootstrap the stack once if eligible, then retry once."""
    try:
        return operation()
    except Exception as first_error:
        if not _recoverable_operator_error(tool, first_error):
            raise
        try:
            bootstrap = _ensure_required_operators(run_root, force=True)
        except OperatorBootstrapError as bootstrap_error:
            raise RuntimeError(
                f"{first_error}; automatic {tool} recovery failed: {bootstrap_error}"
            ) from bootstrap_error
        state_path = run_root / "state.json"
        if state_path.exists():
            state = _load_json(state_path)
            state["operator_bootstrap"] = {
                "ready": bootstrap.get("status") in {"installed", "already_available"},
                "receipt": str(run_root / "operator-bootstrap.json"),
                "recovered_tool": tool,
            }
            _write_json(state_path, state)
            _emit_event(
                run_root,
                state,
                "operator_bootstrap",
                receipt=str(run_root / "operator-bootstrap.json"),
                message=f"{tool} repaired; retrying the blocked stage once",
            )
        try:
            result = operation()
        except Exception as retry_error:
            raise RuntimeError(
                f"{tool} remained unavailable after automatic recovery: {retry_error}"
            ) from retry_error
        receipt_path = run_root / "operator-bootstrap.json"
        if receipt_path.exists():
            bootstrap = _load_json(receipt_path)
            bootstrap["retry_succeeded"] = True
            bootstrap["recovered_tool"] = tool
            bootstrap["recovered_at"] = _now()
            _write_json(receipt_path, bootstrap)
        return result


def _preflight_mapper(repo_path: Path, run_root: Path) -> Dict[str, Any]:
    override = _preflight_override("SIMPLICIO_LOOP_FAKE_MAPPER_PREFLIGHT_JSON")
    if override is not None:
        identity = {"command": "simplicio-mapper", "path": "", "identity_ok": True}
        version_stdout = str(override.get("version_stdout", ""))
        help_stdout = str(override.get("help_stdout", ""))
        version_rc = int(override.get("version_returncode", 0))
        help_rc = int(override.get("help_returncode", 0))
    else:
        identity = _resolved_identity("simplicio-mapper", ("simplicio-mapper",))
        version = _run_cmd(["simplicio-mapper", "--version"], repo_path)
        help_result = _run_cmd(["simplicio-mapper", "--help"], repo_path)
        version_stdout = (version.stdout or "").strip()
        help_stdout = (help_result.stdout or "").strip()
        version_rc = version.returncode
        help_rc = help_result.returncode
    parsed_version = _parse_version_tuple(version_stdout)
    missing_verbs = [verb for verb in MAPPER_REQUIRED_VERBS if verb not in help_stdout]
    task_aware_flags = ("--goal", "--task-file", "--task-fingerprint")
    supported_task_aware_flags = [flag for flag in task_aware_flags if flag in help_stdout]
    receipt = {
        "tool": "simplicio-mapper",
        "returncode": version_rc,
        "stdout": version_stdout,
        "help_returncode": help_rc,
        "help_stdout": help_stdout,
        "version": ".".join(str(part) for part in parsed_version),
        "min_version": ".".join(str(part) for part in MAPPER_MIN_VERSION),
        "version_ok": parsed_version >= MAPPER_MIN_VERSION,
        "required_verbs": list(MAPPER_REQUIRED_VERBS),
        "missing_verbs": missing_verbs,
        "task_aware_flags": list(task_aware_flags),
        "supported_task_aware_flags": supported_task_aware_flags,
        "task_aware_supported": len(supported_task_aware_flags) == len(task_aware_flags),
        "repo_state": _repo_fingerprint(repo_path),
        "path": identity["path"],
        "identity_ok": identity["identity_ok"],
        "checked_at": _now(),
    }
    _write_json(run_root / "mapper-preflight.json", receipt)
    if version_rc != 0 or help_rc != 0:
        raise RuntimeError("simplicio-mapper unavailable")
    if not receipt["identity_ok"]:
        raise RuntimeError("simplicio-mapper identity mismatch")
    if parsed_version < MAPPER_MIN_VERSION:
        raise RuntimeError("simplicio-mapper below minimum version")
    if missing_verbs:
        raise RuntimeError("simplicio-mapper missing required capabilities")
    return receipt


def _operator_capability_gaps(help_stdout: str, task_help_stdout: str) -> Tuple[List[str], List[str]]:
    """Derive dev-cli capability gaps from the exact persisted help surfaces."""
    capability_surface = " ".join(part for part in (help_stdout, task_help_stdout) if part)
    missing_tokens = [
        token for token in DEVCLI_REQUIRED_TOKENS
        if token not in (" " + capability_surface)
    ]
    missing_capabilities = [
        capability for capability in DEVCLI_REQUIRED_CAPABILITIES
        if capability not in capability_surface
    ]
    return missing_tokens, missing_capabilities


def _preflight_operator(repo_path: Path, run_root: Path) -> Dict[str, Any]:
    # Issue #135: the operator bridge validates identity + capability + MIN_VERSION,
    # not merely `which`. A wrong homonym (PATH resolves but the stem mismatches) or a
    # version below DEVCLI_MIN_VERSION blocks before any mutation.
    override = _preflight_override("SIMPLICIO_LOOP_FAKE_DEVCLI_PREFLIGHT_JSON")
    if override is not None:
        identity = {"command": "simplicio-dev-cli", "path": str(override.get("path", "")), "identity_ok": True}
        help_stdout = str(override.get("help_stdout", ""))
        help_rc = int(override.get("help_returncode", 0))
        task_help_stdout = str(override.get("task_help_stdout", help_stdout))
        task_help_rc = int(override.get("task_help_returncode", help_rc))
        version_stdout = str(override.get("version_stdout", "simplicio-py 0.14.0"))
        version_rc = int(override.get("version_returncode", 0))
    else:
        identity = _resolved_identity("simplicio-dev-cli", ("simplicio-dev-cli", "simplicio-py"))
        env = _devcli_env(repo_path)
        help_result = subprocess.run(
            _devcli_cmd(repo_path, "--help"),
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        task_help_result = subprocess.run(
            _devcli_cmd(repo_path, "task", "--help"),
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        version_result = subprocess.run(
            _devcli_cmd(repo_path, "--version"),
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        help_stdout = (help_result.stdout or "").strip()
        help_rc = help_result.returncode
        task_help_stdout = (task_help_result.stdout or "").strip()
        task_help_rc = task_help_result.returncode
        version_stdout = (version_result.stdout or "").strip()
        version_rc = version_result.returncode
    missing_tokens, missing_capabilities = _operator_capability_gaps(help_stdout, task_help_stdout)
    parsed_version = _parse_version_tuple(version_stdout)
    receipt = {
        "tool": "simplicio-dev-cli",
        "returncode": help_rc,
        "help_stdout": help_stdout,
        "task_help_returncode": task_help_rc,
        "task_help_stdout": task_help_stdout,
        "required_tokens": list(DEVCLI_REQUIRED_TOKENS),
        "missing_tokens": missing_tokens,
        "path": identity["path"],
        "identity_ok": identity["identity_ok"],
        "version_stdout": version_stdout,
        "version_returncode": version_rc,
        "version": ".".join(str(part) for part in parsed_version),
        "min_version": ".".join(str(part) for part in DEVCLI_MIN_VERSION),
        "version_ok": parsed_version >= DEVCLI_MIN_VERSION,
        "required_capabilities": list(DEVCLI_REQUIRED_CAPABILITIES),
        "missing_capabilities": missing_capabilities,
        "repo_state": _repo_fingerprint(repo_path),
        "checked_at": _now(),
    }
    _write_json(run_root / "operator-preflight.json", receipt)
    if help_rc != 0 or task_help_rc != 0:
        raise RuntimeError("simplicio-dev-cli unavailable")
    if not receipt["identity_ok"]:
        raise RuntimeError("simplicio-dev-cli identity mismatch")
    if missing_tokens or missing_capabilities:
        raise RuntimeError("simplicio-dev-cli missing required capabilities")
    if version_rc != 0:
        raise RuntimeError("simplicio-dev-cli version probe failed")
    if not receipt["version_ok"]:
        raise RuntimeError(
            "simplicio-dev-cli below minimum version %s (found %s)"
            % (receipt["min_version"], receipt["version"])
        )
    return receipt


def _validate_mapper_receipt(payload: Mapping[str, Any], repo_path: Path) -> None:
    """Require the mapper's own artifact receipt, not a caller-supplied freshness flag."""
    inspect = payload.get("inspect") or {}
    inspect_out = inspect.get("stdout") or {}
    status = inspect_out.get("status") or {}
    evidence = inspect_out.get("evidence") or {}
    artifacts = evidence.get("artifacts") or {}
    if not status.get("artifacts_present") or not status.get("fresh"):
        raise RuntimeError("mapper artifacts are missing or stale")
    # context_cache is an optional cache artifact the mapper does not always emit;
    # it must not block the loop when only that one is missing.
    required_artifacts = {
        key: item for key, item in artifacts.items()
        if isinstance(item, Mapping) and key != "context_cache"
    }
    if not required_artifacts or any(
        not bool(item.get("exists")) for item in required_artifacts.values()
    ):
        raise RuntimeError("mapper artifact evidence is incomplete")
    handoff = ((payload.get("handoff") or {}).get("stdout") or {}).get("context_pack") or {}
    for item in handoff.get("files") or []:
        raw = item.get("path") if isinstance(item, Mapping) else ""
        if not raw:
            continue
        try:
            candidate = (repo_path / str(raw)).resolve() if not Path(str(raw)).is_absolute() else Path(str(raw)).resolve()
            candidate.relative_to(repo_path.resolve())
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"mapper returned path outside authorized repo: {raw}") from exc


def _require_json_receipt(path: Path, label: str) -> Dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"missing required {label} receipt: {path.name}")
    try:
        payload = _load_json(path)
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"invalid required {label} receipt: {path.name}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid required {label} receipt: {path.name}")
    return payload


def _validate_run_receipts(
    repo_path: Path,
    run_dir: Path,
    contract: Mapping[str, Any],
    *,
    state: Mapping[str, Any] | None = None,
    manifest: Mapping[str, Any] | None = None,
    require_dry_run: bool = True,
) -> Dict[str, Any]:
    """Require a current, run-bound mapper -> plan -> operator receipt chain."""
    mapper = _require_json_receipt(run_dir / "mapper-context.json", "mapper context")
    plan = _require_json_receipt(run_dir / "plan.json", "plan")
    mapper_preflight = _require_json_receipt(run_dir / "mapper-preflight.json", "mapper preflight")
    operator_preflight = _require_json_receipt(run_dir / "operator-preflight.json", "operator preflight")
    operator = _require_json_receipt(run_dir / "operator-receipt.json", "operator")

    expected_run_id = str((manifest or {}).get("run_id") or run_dir.name)
    if run_dir.name != expected_run_id:
        raise RuntimeError("run receipts are not bound to the current run")
    if state is not None and str(state.get("run_id") or "") != expected_run_id:
        raise RuntimeError("run state is not bound to the current run")
    expected_contract_hash = str((manifest or {}).get("collection_hash") or "")
    contract_hash = str(contract.get("collection_hash") or "")
    if expected_contract_hash and contract_hash != expected_contract_hash:
        raise RuntimeError("task contract is not bound to the current run")
    if mapper.get("run_id") != expected_run_id or plan.get("run_id") != expected_run_id:
        raise RuntimeError("mapper and plan receipts are not bound to the current run")
    if operator.get("run_id") != expected_run_id:
        raise RuntimeError("operator receipt is not bound to the current run")
    if mapper.get("task_contract_hash") != contract_hash or plan.get("task_contract_hash") != contract_hash:
        raise RuntimeError("mapper and plan receipts do not match the task contract")
    mapper_context_hash = str(plan.get("mapper_context_hash") or "")
    if not mapper_context_hash:
        raise RuntimeError("plan receipt has no mapper context hash")
    actual_mapper_context_hash = hashlib.sha256(
        (run_dir / "mapper-context.json").read_bytes()
    ).hexdigest()
    if actual_mapper_context_hash != mapper_context_hash:
        raise RuntimeError("plan receipt does not match the current mapper context bytes")

    for name, payload in (("scan", mapper.get("scan")), ("inspect", mapper.get("inspect")),
                          ("handoff", mapper.get("handoff"))):
        if not isinstance(payload, Mapping) or payload.get("returncode") != 0:
            raise RuntimeError(f"stale mapper context: {name} did not complete successfully")
    _validate_mapper_receipt(mapper, repo_path)
    planned_state = mapper.get("repo_state_after") or {}
    current_state = _repo_fingerprint(repo_path)
    mapper_before = mapper.get("repo_state_before") or {}
    if not mapper_before.get("tree_hash") or not planned_state.get("tree_hash"):
        raise RuntimeError("mapper context has no repository fingerprint")
    if not _repo_state_equivalent(mapper_before, planned_state) or not _repo_state_equivalent(planned_state, current_state):
        raise RuntimeError("stale mapper context: repository changed after planning")

    if not mapper_preflight.get("identity_ok") or not mapper_preflight.get("version_ok"):
        raise RuntimeError("mapper preflight receipt is not valid")
    if mapper_preflight.get("missing_verbs"):
        raise RuntimeError("mapper preflight receipt is missing required capabilities")
    mapper_preflight_state = mapper_preflight.get("repo_state") or {}
    if not mapper_preflight_state.get("tree_hash") or not _repo_state_equivalent(mapper_preflight_state, current_state):
        raise RuntimeError("stale mapper preflight receipt: repository changed")
    if not operator_preflight.get("identity_ok") or not operator_preflight.get("version_ok"):
        raise RuntimeError("operator preflight receipt is not valid")
    list_fields = (
        "required_tokens", "missing_tokens", "required_capabilities", "missing_capabilities",
    )
    text_fields = ("help_stdout", "task_help_stdout")
    for field in list_fields:
        value = operator_preflight.get(field)
        if field not in operator_preflight:
            raise RuntimeError(f"operator preflight receipt is missing required field: {field}")
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise RuntimeError(f"operator preflight receipt has invalid field type: {field}")
    for field in text_fields:
        if field not in operator_preflight:
            raise RuntimeError(f"operator preflight receipt is missing required field: {field}")
        if not isinstance(operator_preflight[field], str):
            raise RuntimeError(f"operator preflight receipt has invalid field type: {field}")
    if operator_preflight["required_tokens"] != list(DEVCLI_REQUIRED_TOKENS):
        raise RuntimeError("operator preflight receipt required_tokens do not match the canonical contract")
    if operator_preflight["required_capabilities"] != list(DEVCLI_REQUIRED_CAPABILITIES):
        raise RuntimeError("operator preflight receipt required_capabilities do not match the canonical contract")
    recomputed_missing_tokens, recomputed_missing_capabilities = _operator_capability_gaps(
        operator_preflight["help_stdout"], operator_preflight["task_help_stdout"],
    )
    if operator_preflight["missing_tokens"] != recomputed_missing_tokens:
        raise RuntimeError("operator preflight receipt missing_tokens do not match persisted help")
    if operator_preflight["missing_capabilities"] != recomputed_missing_capabilities:
        raise RuntimeError("operator preflight receipt missing_capabilities do not match persisted help")
    if recomputed_missing_tokens or recomputed_missing_capabilities:
        raise RuntimeError("operator preflight receipt is missing required capabilities")
    operator_preflight_state = operator_preflight.get("repo_state") or {}
    if not operator_preflight_state.get("tree_hash") or not _repo_state_equivalent(operator_preflight_state, current_state):
        raise RuntimeError("stale operator preflight receipt: repository changed")

    tasks = list(contract.get("tasks") or [])
    validation = validate_plan(
        plan, tasks, repo_path,
        contract_hash=contract_hash,
        current_state=current_state,
    )
    if not validation["valid"]:
        raise RuntimeError("stale or invalid plan receipt: " + ", ".join(validation["errors"]))
    if (plan.get("deterministic") or {}).get("verified") is not True:
        raise RuntimeError("plan receipt is not deterministic")
    context_pack = ((mapper.get("handoff") or {}).get("stdout") or {}).get("context_pack") or {}
    mapper_pack_hash = str(plan.get("mapper_pack_hash") or "")
    context_pack_hash = str(context_pack.get("pack_hash") or "")
    if mapper_pack_hash and context_pack_hash and mapper_pack_hash != context_pack_hash:
        raise RuntimeError("plan receipt does not match the mapper context fingerprint")

    plan_hash = hashlib.sha256((run_dir / "plan.json").read_bytes()).hexdigest()
    if operator.get("task_contract_hash") != contract_hash:
        raise RuntimeError("operator receipt does not match the task contract")
    if operator.get("plan_hash") != plan_hash:
        raise RuntimeError("operator receipt does not match the current plan")
    if operator.get("mapper_pack_hash") != plan.get("mapper_pack_hash"):
        raise RuntimeError("operator receipt does not match the mapper context")
    if operator.get("mapper_context_hash") != mapper_context_hash:
        raise RuntimeError("operator receipt does not match the mapper receipt")
    operator_state = operator.get("repo_state_before") or {}
    if not operator_state.get("tree_hash") or not _repo_state_equivalent(operator_state, current_state):
        raise RuntimeError("stale operator receipt: repository changed")
    if require_dry_run and (operator.get("execution_state") != "dry_run" or operator.get("returncode") != 0):
        raise RuntimeError("operator receipt is not a fresh successful dry-run preflight")
    if not operator.get("target_within_repo") or not operator.get("authorized_targets"):
        raise RuntimeError("operator receipt has no authorized target")
    target = str(operator.get("target") or "")
    authorized_targets = {str(item) for item in operator.get("authorized_targets") or []}
    planned_targets = {
        str(item) for step in plan.get("steps") or []
        if isinstance(step, Mapping) for item in step.get("candidate_targets") or []
    }
    try:
        (repo_path / target).resolve().relative_to(repo_path.resolve())
    except (OSError, ValueError):
        raise RuntimeError("operator receipt target is outside the authorized repository")
    if not target or target not in authorized_targets or target not in planned_targets:
        raise RuntimeError("operator receipt target is not authorized by the plan")
    if state is not None:
        if state.get("phase") in {"blocked", "done", "cancelled"}:
            raise RuntimeError(f"run is not runnable: {state.get('phase')}")
        if not (state.get("mapper") or {}).get("ready") or not (state.get("operator") or {}).get("ready"):
            raise RuntimeError("run is not runnable: mapper/operator receipts are not ready")
    return {
        "mapper": mapper,
        "plan": plan,
        "operator_preflight": operator_preflight,
        "operator": operator,
        "repo_state": current_state,
        "plan_hash": plan_hash,
    }


def _persist_batch_preflight_block(
    run_dir: Path,
    state: Dict[str, Any],
    repo_path: Path,
    reason: str,
    task_indices: Sequence[int] = (),
) -> Path:
    diagnostic_path = run_dir / "operator-batch-preflight.json"
    blocker = {
        "kind": "operator_batch_preflight",
        "reason_code": "operator_batch_preflight_failed",
        "message": reason,
        "run_id": str(state.get("run_id") or run_dir.name),
        "scope": "global",
    }
    diagnostic = {
        "schema": BATCH_PREFLIGHT_SCHEMA,
        "status": "BLOCKED",
        "run_id": blocker["run_id"],
        "task_indices": [int(index) for index in task_indices],
        "blocker": blocker,
        "repo_state": _repo_fingerprint(repo_path),
        "checked_at": _now(),
    }
    _write_json(diagnostic_path, diagnostic)
    state["blockers"] = [blocker]
    state["current_action"] = "operator_batch_preflight_blocked"
    state["next_action"] = "repair_mapper_or_repo"
    _write_json(run_dir / "state.json", state)
    if state.get("phase") not in {"done", "cancelled"}:
        _transition(
            run_dir, state, "blocked", "operator batch prerequisite validation failed",
            receipt=str(diagnostic_path), extra={"error": reason, "scope": "global"},
        )
    _emit_event(
        run_dir, state, "blocked", receipt=str(diagnostic_path),
        blocker=blocker["reason_code"], message="operator batch blocked before dispatch", scope="global",
    )
    return diagnostic_path


def _run_mapper(repo_path: Path, run_root: Path, task_path: str = "", goal: str = "",
                task_fingerprint: str = "", target_hint: str = "") -> Dict[str, Any]:
    before = _repo_fingerprint(repo_path)
    mapper_preflight = _preflight_mapper(repo_path, run_root)
    scan = _run_cmd(["simplicio-mapper", "scan", ".", "--json", "--sync"], repo_path)
    inspect = _run_cmd(["simplicio-mapper", "inspect", ".", "--json", "--await"], repo_path)
    handoff_argv = ["simplicio-mapper", "handoff", ".", "--json", "--await"]
    task_aware_supported = bool(mapper_preflight.get("task_aware_supported"))
    if task_aware_supported and goal.strip():
        handoff_argv.extend(["--goal", goal.strip()])
    if task_aware_supported and task_path.strip():
        handoff_argv.extend(["--task-file", task_path.strip()])
    if task_aware_supported and task_fingerprint.strip():
        handoff_argv.extend(["--task-fingerprint", task_fingerprint.strip()])
    if task_aware_supported and target_hint.strip():
        handoff_argv.extend(["--target", target_hint.strip()])
    handoff = _run_cmd(handoff_argv, repo_path)
    payload = {
        "scan": {
            "returncode": scan.returncode,
            "stdout": json.loads(scan.stdout) if scan.stdout.strip() else {},
            "stderr": (scan.stderr or "").strip(),
        },
        "inspect": {
            "returncode": inspect.returncode,
            "stdout": json.loads(inspect.stdout) if inspect.stdout.strip() else {},
            "stderr": (inspect.stderr or "").strip(),
        },
        "handoff": {
            "returncode": handoff.returncode,
            "stdout": json.loads(handoff.stdout) if handoff.stdout.strip() else {},
            "stderr": (handoff.stderr or "").strip(),
        },
        "generated_at": _now(),
        "repo_state_before": before,
        "repo_state_after": _repo_fingerprint(repo_path),
    }
    _write_json(run_root / "mapper-context.json", payload)
    if scan.returncode != 0 or inspect.returncode != 0 or handoff.returncode != 0:
        raise RuntimeError("mapper scan/inspect/handoff failed")
    if not _repo_state_equivalent(payload["repo_state_before"], payload["repo_state_after"]):
        raise RuntimeError("repository changed during mapper survey; freshness cannot be proven")
    # Test fixtures intentionally replace the operator preflight; production runs
    # always use the mapper's own artifact/freshness receipt.
    if _preflight_override("SIMPLICIO_LOOP_FAKE_MAPPER_PREFLIGHT_JSON") is None:
        _validate_mapper_receipt(payload, repo_path)
    return payload


def _build_plan(tasks: List[Dict[str, Any]], mapper_payload: Dict[str, Any], repo_path: Path,
                contract_hash: str = "") -> Dict[str, Any]:
    return _build_plan_with_hints(tasks, mapper_payload, repo_path, "", contract_hash=contract_hash)


def _extract_repo_file_hints(task_text: str, repo_path: Path) -> List[str]:
    hints: List[str] = []
    for match in re.finditer(r"(?P<path>[A-Za-z0-9_./\\-]+\.(?:py|ts|tsx|js))", task_text or ""):
        raw = match.group("path").strip().replace("\\", "/")
        candidate = Path(raw)
        try:
            resolved = (repo_path / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
            rel = resolved.relative_to(repo_path.resolve()).as_posix()
        except (OSError, ValueError):
            continue
        low = rel.lower()
        if low.startswith(".orchestrator/") or low.startswith(".claude/") or low.startswith(".github/"):
            continue
        if low.startswith(".venv/") or low.startswith("venv/") or "/site-packages/" in low:
            continue
        if "/_bundle/" in low:
            continue
        if rel not in hints:
            hints.append(rel)
    return hints


def _task_mapper_context(mapper_payload: Mapping[str, Any], task_index: int) -> Dict[str, Any]:
    """Return one task's Mapper envelope, with a single-task compatibility adapter."""
    contexts = mapper_payload.get("task_contexts") or []
    if isinstance(contexts, Sequence) and not isinstance(contexts, (str, bytes)):
        for context in contexts:
            if isinstance(context, Mapping) and int(context.get("task_index") or 0) == task_index:
                return dict(context)
    handoff = mapper_payload.get("handoff") or {}
    return {
        "schema": "simplicio.task-mapper-context/v1",
        "task_index": task_index,
        "task_fingerprint": str(mapper_payload.get("task_fingerprint") or ""),
        "handoff": dict(handoff) if isinstance(handoff, Mapping) else {},
        "context_hash": str(mapper_payload.get("mapper_context_hash") or ""),
        "compatibility_adapter": "single-task-shared-handoff",
    }


def _task_mapper_text(task: Mapping[str, Any]) -> str:
    parts = [_task_goal(dict(task))]
    if task.get("original_text"):
        parts.append(str(task["original_text"]))
    parts.extend(str(item.get("title") or "") for item in task.get("scenarios") or []
                 if isinstance(item, Mapping))
    parts.extend(str(item.get("id") or "") for item in task.get("rules") or []
                 if isinstance(item, Mapping))
    return " ".join(part.strip() for part in parts if part and part.strip())


def _task_context_plan_data(context: Mapping[str, Any], task: Mapping[str, Any],
                            repo_path: Path, task_text: str = "") -> Dict[str, Any]:
    handoff = context.get("handoff") if isinstance(context.get("handoff"), Mapping) else {}
    stdout = handoff.get("stdout") if isinstance(handoff.get("stdout"), Mapping) else handoff
    pack = stdout.get("context_pack") if isinstance(stdout, Mapping) else {}
    if not isinstance(pack, Mapping):
        pack = {}
    files = pack.get("files") or []
    targets = []
    for item in files:
        path = str(item.get("path") or "") if isinstance(item, Mapping) else ""
        if not path:
            continue
        try:
            resolved = (repo_path / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
            path = resolved.relative_to(repo_path.resolve()).as_posix()
        except (OSError, ValueError):
            continue
        low = path.lower()
        if (low.startswith((".orchestrator/", ".claude/", ".github/", ".venv/", "venv/"))
                or "/site-packages/" in low or "/_bundle/" in low):
            continue
        if path not in targets:
            targets.append(path)
    if not targets:
        targets = _candidate_targets({"handoff": {"stdout": {"context_pack": dict(pack)}}}, repo_path)
    explicit = _extract_repo_file_hints(task_text, repo_path)
    for hint in _extract_repo_file_hints(_task_mapper_text(task), repo_path):
        if hint not in explicit:
            explicit.append(hint)
    ordered = []
    for path in explicit + targets:
        if path not in ordered:
            ordered.append(path)
    tests = sorted({
        str(test_path).replace("\\", "/")
        for item in files
        if isinstance(item, Mapping)
        for test_path in item.get("tests") or []
        if str(test_path).strip()
    })
    fidelity = pack.get("fidelity") if isinstance(pack.get("fidelity"), Mapping) else {}
    selection = pack.get("selection") if isinstance(pack.get("selection"), Mapping) else {}
    token_fit = pack.get("token_budget_fit") if isinstance(pack.get("token_budget_fit"), Mapping) else {}
    return {
        "targets": ordered,
        "tests": tests,
        "pack_hash": str(pack.get("pack_hash") or context.get("pack_hash") or ""),
        "context_hash": str(context.get("context_hash") or ""),
        "token_budget": int(pack.get("token_budget") or token_fit.get("token_budget") or 8000),
        "fidelity": dict(fidelity),
        "selection": dict(selection),
        "abstention": str(pack.get("abstention_reason") or selection.get("abstention_reason") or ""),
    }


def _build_plan_with_hints(tasks: List[Dict[str, Any]], mapper_payload: Dict[str, Any], repo_path: Path,
                           task_text: str, *, contract_hash: str = "") -> Dict[str, Any]:
    shared_handoff = ((mapper_payload.get("handoff") or {}).get("stdout") or {}).get("context_pack") or {}
    task_contexts = []
    steps = []
    aggregate_targets: List[str] = []
    for index, task in enumerate(tasks, start=1):
        context = _task_mapper_context(mapper_payload, index)
        data = _task_context_plan_data(context, task, repo_path, task_text)
        task_contexts.append({
            "task_index": index,
            "task_id": str(task.get("id") or ""),
            "task_fingerprint": str(context.get("task_fingerprint") or ""),
            "context_hash": data["context_hash"],
            "pack_hash": data["pack_hash"],
            "token_budget": data["token_budget"],
            "fidelity": data["fidelity"],
            "selection": data["selection"],
            "abstention": data["abstention"],
            "compatibility_adapter": context.get("compatibility_adapter", ""),
        })
        for path in data["targets"]:
            if path not in aggregate_targets:
                aggregate_targets.append(path)
        task_steps = []
        for scenario in task.get("scenarios") or []:
            task_steps.append({
                "kind": "scenario",
                "id": scenario.get("id"),
                "title": scenario.get("title"),
                "rule_refs": scenario.get("rule_refs") or [],
                "verification_intent": scenario.get("verification_intent"),
                "mapper_context_hash": data["context_hash"],
                "task_contract_hash": contract_hash,
                "plan": {
                    "read_paths": list(data["targets"]),
                    "change_paths": list(data["targets"]),
                    "test_paths": list(data["tests"]),
                    "test_commands": ["operator validation and repository test gate"],
                    "no_code_change": False,
                },
                "status": "pending",
            })
        rule_ids = [str(rule.get("id")) for rule in task.get("rules") or [] if rule.get("id")]
        steps.append({
            "task_index": index,
            "title": (task.get("identity") or {}).get("title") or _task_goal(task),
            "task_fingerprint": str(context.get("task_fingerprint") or ""),
            "mapper_context_hash": data["context_hash"],
            "context_pack_hash": data["pack_hash"],
            "candidate_targets": list(data["targets"]),
            "mapped_tests": list(data["tests"]),
            "selection": {"token_budget": data["token_budget"], **data["selection"]},
            "fidelity": data["fidelity"],
            "abstention": data["abstention"],
            "to_create": [],
            "rule_ids": rule_ids,
            "steps": task_steps,
        })
    shared_pack_hash = str(shared_handoff.get("pack_hash") or "") if isinstance(shared_handoff, Mapping) else ""
    plan = {
        "schema": PLAN_SCHEMA,
        "task_contract_hash": contract_hash,
        "generated_at": _now(),
        "task_count": len(tasks),
        "mapper_targets": aggregate_targets,
        "mapper_pack_hash": shared_pack_hash,
        "context_pack_hash": shared_pack_hash,
        "task_contexts": task_contexts,
        "repo_state": mapper_payload.get("repo_state_after") or {},
        "freshness": {
            "verified": _repo_state_equivalent(mapper_payload.get("repo_state_before") or {},
                                               mapper_payload.get("repo_state_after") or {}),
            "checked_at": mapper_payload.get("generated_at", ""),
            "current_state": _repo_fingerprint(repo_path),
        },
        "steps": steps,
    }
    deterministic_input = {
        "schema": plan["schema"],
        "task_contract_hash": contract_hash,
        "mapper_pack_hash": plan["mapper_pack_hash"],
        "task_contexts": task_contexts,
        "repo_state": plan["repo_state"],
        "steps": plan["steps"],
    }
    plan["deterministic"] = {
        "verified": True,
        "algorithm": "mapper-derived-task-context-v1",
        "input_hash": hashlib.sha256(
            json.dumps(deterministic_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
    }
    return plan


def _fallback_targets(repo_path: Path) -> List[str]:
    out: List[str] = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [
            d for d in dirs
            if d not in {".git", ".orchestrator", ".claude", ".simplicio", "__pycache__", ".venv", "venv", "site-packages"}
        ]
        for name in files:
            if not name.endswith((".py", ".ts", ".tsx", ".js")):
                continue
            full = Path(root) / name
            try:
                rel = full.relative_to(repo_path).as_posix()
            except ValueError:
                continue
            low = rel.lower()
            if "/_bundle/" in low or low.startswith(".github/"):
                continue
            out.append(rel)
    out.sort()
    return out[:8]


def _candidate_targets(mapper_payload: Dict[str, Any], repo_path: Path) -> List[str]:
    handoff = ((mapper_payload.get("handoff") or {}).get("stdout") or {}).get("context_pack") or {}
    files = handoff.get("files") or []
    ranked = []
    for item in files:
        path = item.get("path") if isinstance(item, dict) else None
        if not path:
            continue
        try:
            resolved = (repo_path / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
            resolved.relative_to(repo_path.resolve())
        except (OSError, ValueError):
            continue
        low = path.lower()
        if low.startswith(".orchestrator/") or low.startswith(".claude/"):
            continue
        if low.startswith(".venv/") or low.startswith("venv/") or "/site-packages/" in low:
            continue
        if low.startswith(".github/"):
            continue
        if "/_bundle/" in low.replace("\\", "/"):
            continue
        if low.endswith(".py") or low.endswith(".ts") or low.endswith(".tsx") or low.endswith(".js"):
            ranked.append(path)
    ranked = ranked[:8]
    return ranked or _fallback_targets(repo_path)


def _build_anchor(tasks: List[Dict[str, Any]], contract_hash: str) -> Dict[str, Any]:
    criteria = []
    index = 1
    for task_index, task in enumerate(tasks, start=1):
        for scenario in task.get("scenarios") or []:
            criteria.append({
                "id": f"AC{index}",
                "task_index": task_index,
                "scenario_id": scenario.get("id"),
                "title": scenario.get("title"),
                "rule_refs": scenario.get("rule_refs") or [],
                "status": "pending",
            })
            index += 1
    return {
        "schema": "simplicio.anchor/v1",
        "contract_hash": contract_hash,
        "criteria": criteria,
        "created_at": _now(),
    }


def _prepare_operator_receipt(repo_path: Path, run_root: Path, task: Dict[str, Any],
                              target: str) -> Dict[str, Any]:
    try:
        target_path = (repo_path / target).resolve() if not Path(target).is_absolute() else Path(target).resolve()
        target_path.relative_to(repo_path.resolve())
    except (OSError, ValueError) as exc:
        raise ValueError(f"operator target outside authorized repo: {target!r}") from exc
    _preflight_operator(repo_path, run_root)
    task_spec_path = run_root / "task-spec.json"
    task_spec = _task_spec_payload(task)
    task_spec_hash = _task_spec_hash(task_spec)
    _write_json(task_spec_path, task_spec)
    context_args, context_handoff = _context_handoff_args(repo_path, run_root)
    fake = os.environ.get("SIMPLICIO_LOOP_FAKE_OPERATOR_JSON", "").strip()
    if fake:
        payload = json.loads(fake)
        receipt = {
            "schema": OPERATOR_RECEIPT_SCHEMA,
            "mode": "dry_run",
            "tool": "simplicio-dev-cli",
            "execution_state": payload.get("execution_state", "dry_run"),
            "target": target,
            "goal": _task_goal(task),
            "argv": payload.get("argv", []),
            "returncode": payload.get("returncode", 0),
            "stdout": payload.get("stdout", {}),
            "stderr": payload.get("stderr", ""),
            "timed_out": False,
            "measured_at": _now(),
            "source": "env_override",
            "context_handoff": context_handoff,
            "repo_state_before": _repo_fingerprint(repo_path),
            "task_spec_path": str(task_spec_path),
            "task_spec_hash": task_spec_hash,
        }
        _write_json(run_root / "operator-receipt.json", receipt)
        return receipt

    argv = _devcli_cmd(
        repo_path, "task", "--root", str(repo_path), "--task-spec", str(task_spec_path),
        "--mode", "integrated", "--target", target, "--dry-run-task", "--json",
        "--bound-paths", target,
    )
    argv.extend(context_args)
    try:
        op_env = _devcli_env(repo_path, _operator_env())
        result = subprocess.run(
            argv,
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=_operator_timeout("dry_run"),
            env=op_env,
        )
        stdout = (result.stdout or "").strip()
        parsed = {}
        if stdout:
            try:
                parsed = json.loads(stdout)
            except ValueError:
                parsed = {"raw": stdout}
        receipt = {
            "schema": OPERATOR_RECEIPT_SCHEMA,
            "mode": "dry_run",
            "tool": "simplicio-dev-cli",
            "execution_state": "dry_run" if result.returncode == 0 else "blocked",
            "target": target,
            "goal": _task_goal(task),
            "argv": argv,
            "returncode": result.returncode,
            "stdout": parsed,
            "stderr": (result.stderr or "").strip(),
            "timed_out": False,
            "measured_at": _now(),
            "source": "live_cli",
            "context_handoff": context_handoff,
            "provider_config": {
                "model": op_env.get("SIMPLICIO_MODEL", ""),
                "effort": op_env.get("SIMPLICIO_CODEX_EFFORT", ""),
            },
            "repo_state_before": _repo_fingerprint(repo_path),
            "task_spec_path": str(task_spec_path),
            "task_spec_hash": task_spec_hash,
        }
    except subprocess.TimeoutExpired as exc:
        op_env = _operator_env()
        receipt = {
            "schema": OPERATOR_RECEIPT_SCHEMA,
            "mode": "dry_run",
            "tool": "simplicio-dev-cli",
            "execution_state": "blocked",
            "target": target,
            "goal": _task_goal(task),
            "argv": argv,
            "returncode": None,
            "stdout": {},
            "stderr": f"timed out after {exc.timeout}s",
            "timed_out": True,
            "measured_at": _now(),
            "source": "live_cli",
            "context_handoff": context_handoff,
            "provider_config": {
                "model": op_env.get("SIMPLICIO_MODEL", ""),
                "effort": op_env.get("SIMPLICIO_CODEX_EFFORT", ""),
            },
            "repo_state_before": _repo_fingerprint(repo_path),
            "task_spec_path": str(task_spec_path),
            "task_spec_hash": task_spec_hash,
        }
    _write_json(run_root / "operator-receipt.json", receipt)
    return receipt


def arm_run(repo: str, task_path: str, delivery: str, max_iterations: int) -> Dict[str, Any]:
    repo_path = Path(repo).resolve()
    delivery = normalize_delivery_target(delivery)
    raw = Path(task_path).read_text(encoding="utf-8")
    compiled = compile_many(raw, source_path=str(Path(task_path).resolve()))
    tasks = compiled.get("tasks") or []
    validation_errors: List[str] = []
    validation_warnings: List[str] = []
    for idx, task in enumerate(tasks, start=1):
        verdict = validate_contract(task)
        validation_errors.extend([f"task[{idx}] {e}" for e in verdict["errors"]])
        validation_warnings.extend([f"task[{idx}] {w}" for w in verdict["warnings"]])
    if validation_errors:
        raise ValueError("invalid task contract: " + "; ".join(validation_errors))

    run_id = _run_id()
    # Keep loop run state under .simplicio/ (which simplicio-mapper ignores for
    # freshness) instead of .orchestrator/ (which the mapper sees as repo churn and
    # marks artifacts_not_fresh, blocking the loop before any implementation work).
    run_root = repo_path / ".simplicio" / "loop-runs" / run_id
    loop_dir = run_root / "loop"
    loop_dir.mkdir(parents=True, exist_ok=True)

    promise = f"run-{run_id}-verified"
    manifest = {
        "schema": RUNNER_SCHEMA,
        "run_id": run_id,
        "repo": str(repo_path),
        "task_path": str(Path(task_path).resolve()),
        "delivery_target": delivery,
        "max_iterations": max_iterations,
        "completion_promise": promise,
        "created_at": _now(),
        "task_count": compiled["task_count"],
        "collection_hash": compiled["collection_hash"],
    }
    _write_json(run_root / "manifest.json", manifest)
    _write_json(run_root / "task-contract.json", compiled)
    _write_json(loop_dir / "anchor.json", _build_anchor(tasks, compiled["collection_hash"]))
    goal = "\n\n".join([_task_goal(task) for task in tasks if _task_goal(task)]).strip() or raw.strip()
    _write_scratchpad(loop_dir, goal, max_iterations, promise)
    first_goal_fp = (tasks[0].get("source") or {}).get("hash", "") if tasks else ""
    _write_watcher_challenge(loop_dir, first_goal_fp)
    state = {
        "schema": STATE_SCHEMA,
        "run_id": run_id,
        "phase": "intake",
        "delivery_target": delivery,
        "created_at": _now(),
        "updated_at": _now(),
        "task_count": compiled["task_count"],
        "coverage": _coverage(tasks),
        "validation": {"errors": validation_errors, "warnings": validation_warnings},
        "current_action": "task_contract_compiled",
        "next_action": "mapper_scan_required",
        "delivery": {"target": delivery, "current_state": "planned", "ready": False, "receipt": ""},
        "completion": _default_completion_state(),
        "maintenance": _default_maintenance_state(),
        "mapper": {"ready": False, "receipt": "", "targets": []},
        "operator": {"ready": False, "receipt": "", "target": "", "execution_state": "proposed"},
        "evidence": {"ready": False, "receipt": "", "status": "UNVERIFIED"},
        "blockers": [],
        "attempts": 0,
        "history": [],
        "events": [],
        "task_ids": [str(task.get("id") or "") for task in tasks if task.get("id")],
        "ac_ids": [ac_id for task in tasks for ac_id in _task_ac_ids(task)],
    }
    _write_json(run_root / "state.json", state)
    _emit_event(run_root, state, "contract_frozen", receipt=str(run_root / "task-contract.json"),
                message="task contract compiled and frozen")
    _emit_event(run_root, state, "watcher_challenge", receipt=str(loop_dir / "watcher_challenge.json"),
                message="watcher challenge created")
    _append_jsonl(
        run_root / "transitions.jsonl",
        {
            "ts": _now(),
            "from": None,
            "to": "intake",
            "reason": "run armed from raw task",
            "receipt": str(run_root / "task-contract.json"),
        },
    )
    _transition(run_root, state, "mapping", "task contract compiled and persisted; mapper required",
                receipt=str(run_root / "task-contract.json"))
    try:
        primary_goal = _task_goal(tasks[0]) if tasks else raw.strip()
        mapper_payload = _run_with_operator_recovery(
            "simplicio-mapper",
            run_root,
            lambda: _run_mapper(
                repo_path,
                run_root,
                task_path=str(Path(task_path).resolve()),
                goal=primary_goal,
                task_fingerprint=compiled["collection_hash"],
            ),
        )
        mapper_payload["run_id"] = run_id
        mapper_payload["task_contract_hash"] = compiled["collection_hash"]
        _write_json(run_root / "mapper-context.json", mapper_payload)
        state = _load_json(run_root / "state.json")
        state["mapper"] = {
            "ready": True,
            "receipt": str(run_root / "mapper-context.json"),
            "targets": _candidate_targets(mapper_payload, repo_path),
        }
        state["current_action"] = "mapper_context_persisted"
        state["next_action"] = "plan_ready_for_decision"
        _write_json(run_root / "state.json", state)
        _emit_event(run_root, state, "mapper_fresh", receipt=str(run_root / "mapper-context.json"),
                    message="mapper scan, inspect, and handoff are fresh")
        _transition(run_root, state, "planning", "mapper scan/inspect/handoff persisted",
                    receipt=str(run_root / "mapper-context.json"))
        plan = _build_plan_with_hints(tasks, mapper_payload, repo_path, raw,
                                      contract_hash=compiled["collection_hash"])
        plan["run_id"] = run_id
        plan["mapper_context_hash"] = hashlib.sha256(
            (run_root / "mapper-context.json").read_bytes()
        ).hexdigest()
        plan_validation = validate_plan(
            plan, tasks, repo_path,
            contract_hash=compiled["collection_hash"],
            current_state=_repo_fingerprint(repo_path),
        )
        plan["validation"] = plan_validation
        if not plan_validation["valid"]:
            if any("targets_missing" in error for error in plan_validation["errors"]):
                raise RuntimeError("mapper-derived plan has no authorized operator target")
            raise RuntimeError("mapper-derived plan failed validation: " + ", ".join(plan_validation["errors"]))
        _write_json(run_root / "plan.json", plan)
        state = _load_json(run_root / "state.json")
        state["current_action"] = "plan_materialized"
        _maybe_auto_build_planning_receipt(run_root, state, run_id, compiled, plan, plan_validation, repo_path)
        candidates = ((plan.get("steps") or [{}])[0].get("candidate_targets") or [])
        if not candidates:
            raise RuntimeError("mapper-derived plan has no authorized operator target")
        receipt = _run_with_operator_recovery(
            "simplicio-dev-cli",
            run_root,
            lambda: _prepare_operator_receipt(
                repo_path, run_root, tasks[0], candidates[0]
            ),
        )
        plan_hash = hashlib.sha256((run_root / "plan.json").read_bytes()).hexdigest()
        receipt["run_id"] = run_id
        receipt["task_contract_hash"] = compiled["collection_hash"]
        receipt["plan_hash"] = plan_hash
        receipt["mapper_pack_hash"] = plan.get("mapper_pack_hash", "")
        receipt["mapper_context_hash"] = plan.get("mapper_context_hash", "")
        receipt["authorized_targets"] = [candidates[0]]
        receipt["target_within_repo"] = True
        _write_json(run_root / "operator-receipt.json", receipt)
        if receipt.get("execution_state") != "dry_run" or receipt.get("returncode") != 0:
            raise RuntimeError(
                "operator preflight blocked the run: "
                + str(receipt.get("stderr") or receipt.get("execution_state") or "unknown failure")
            )
        state["operator"] = {
            "ready": True,
            "receipt": str(run_root / "operator-receipt.json"),
            "target": candidates[0],
            "execution_state": receipt.get("execution_state", "proposed"),
        }
        evidence = build_evidence_receipt(str(run_root))
        _write_json(run_root / "evidence-receipt.json", evidence)
        state["evidence"] = {
            "ready": False,
            "receipt": str(run_root / "evidence-receipt.json"),
            "status": evidence.get("status", "UNVERIFIED"),
        }
        delivery_receipt = build_delivery_receipt(str(run_root), delivery, current_state="implemented")
        write_delivery_receipt(str(run_root), delivery_receipt)
        state["delivery"] = {
            "target": delivery,
            "current_state": delivery_receipt["current_state"],
            "ready": delivery_receipt["ready"],
            "receipt": str(run_root / "delivery-receipt.json"),
            "source_checked_at": delivery_receipt["source_checked_at"],
        }
        state["current_action"] = "operator_dry_run_recorded"
        state["next_action"] = "await_operator_decision"
        _write_json(run_root / "state.json", state)
        _emit_event(run_root, state, "plan_ready", receipt=str(run_root / "plan.json"),
                    message="validated plan materialized")
        if state.get("operator", {}).get("receipt"):
            _emit_event(run_root, state, "operator_receipt",
                        receipt=str(run_root / "operator-receipt.json"),
                        message="operator dry-run receipt persisted")
        _transition(run_root, state, "awaiting_decision", "plan derived from task contract + mapper",
                    receipt=str(run_root / "plan.json"))
    except Exception as exc:
        state = _load_json(run_root / "state.json")
        message = str(exc)
        if "no authorized operator target" in message or "operator preflight blocked" in message:
            state["blockers"] = [{
                "kind": "run_preflight",
                "reason_code": "no_authorized_target" if "target" in message else "operator_dry_run_failed",
                "message": message,
                "run_id": run_id,
            }]
        else:
            state["blockers"] = [message]
        state["current_action"] = "mapping_failed"
        state["next_action"] = "repair_mapper_or_repo"
        evidence_path = run_root / "evidence-receipt.json"
        if not evidence_path.exists():
            mapper_path = run_root / "mapper-context.json"
            operator_path = run_root / "operator-receipt.json"
            plan_path = run_root / "plan.json"
            if not plan_path.exists() and not mapper_path.exists():
                _write_json(mapper_path, {
                    "run_id": run_id,
                    "task_contract_hash": compiled["collection_hash"],
                    "status": "blocked",
                    "error": message,
                })
            if not plan_path.exists() and not operator_path.exists():
                _write_json(operator_path, {
                    "schema": OPERATOR_RECEIPT_SCHEMA,
                    "run_id": run_id,
                    "task_contract_hash": compiled["collection_hash"],
                    "execution_state": "blocked",
                    "status": "blocked",
                    "returncode": None,
                    "changed_paths": [],
                    "error": message,
                })
            if mapper_path.exists() and operator_path.exists():
                evidence = build_evidence_receipt(str(run_root))
                _write_json(evidence_path, evidence)
                state["evidence"] = {
                    "ready": False,
                    "receipt": str(evidence_path),
                    "status": evidence.get("status", "UNVERIFIED"),
                }
        _write_json(run_root / "state.json", state)
        _transition(run_root, state, "blocked", "mapper integration failed",
                    receipt=str(run_root / "mapper-context.json"), extra={"error": str(exc)})
    return {"manifest": manifest, "state": _load_json(run_root / "state.json"), "run_dir": str(run_root)}


def _changed_paths(repo_path: Path) -> List[str]:
    try:
        result = _run_cmd(["git", "diff", "--name-only", "HEAD"], repo_path)
        paths = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        status = _run_cmd(["git", "status", "--porcelain=v1", "--untracked-files=all"], repo_path)
        for line in (status.stdout or "").splitlines():
            if len(line) > 3 and line[3:].strip() not in paths:
                paths.append(line[3:].strip())
        return sorted(set(paths))
    except Exception:
        return []


def _capture_operator_checkpoint(run_dir: Path, repo_path: Path, targets: List[str]) -> Dict[str, Any]:
    checkpoint_dir = run_dir / "checkpoint"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for target in sorted(set(t for t in targets if t)):
        path = repo_path / target
        exists = path.exists()
        content = path.read_text(encoding="utf-8") if exists else None
        files.append({
            "path": target,
            "exists": exists,
            "content": content,
        })
    return {
        "kind": "file-snapshot/v1",
        "created_at": _now(),
        "safe_targets": sorted(set(t for t in targets if t)),
        "files": files,
    }


def _restore_operator_checkpoint(checkpoint: Dict[str, Any], repo_path: Path, changed_paths: List[str]) -> Dict[str, Any]:
    targets = sorted(set(str(path) for path in (checkpoint.get("safe_targets") or []) if str(path)))
    changed = sorted(set(str(path) for path in (changed_paths or []) if str(path)))
    snapshots = {item["path"]: item for item in (checkpoint.get("files") or []) if isinstance(item, dict) and item.get("path")}
    if not changed:
        for rel in targets:
            snap = snapshots.get(rel)
            if not snap:
                continue
            path = repo_path / rel
            exists_now = path.exists()
            content_now = path.read_text(encoding="utf-8") if exists_now else None
            if bool(snap.get("exists")) != exists_now or (snap.get("exists") and snap.get("content") != content_now):
                changed.append(rel)
    if not changed:
        return {"attempted": False, "restored": False, "reason": "no_changed_paths"}
    if not targets:
        return {"attempted": False, "restored": False, "reason": "checkpoint_targets_missing"}
    if any(path not in targets for path in changed):
        return {"attempted": False, "restored": False, "reason": "changed_paths_outside_checkpoint_scope"}
    for rel in changed:
        snap = snapshots.get(rel)
        if not snap:
            return {"attempted": False, "restored": False, "reason": f"missing_snapshot:{rel}"}
        path = repo_path / rel
        if snap.get("exists"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(snap.get("content") or "", encoding="utf-8")
        elif path.exists():
            path.unlink()
    return {"attempted": True, "restored": True, "reason": "restored_checkpoint"}


def _operator_failure_fingerprint(returncode: int | None, stderr: str, stdout: Any) -> str:
    parts = [f"returncode={returncode}"]
    if stderr:
        parts.append(f"stderr={stderr}")
    if stdout:
        if isinstance(stdout, dict):
            parts.append("stdout=" + json.dumps(stdout, ensure_ascii=False, sort_keys=True))
        else:
            parts.append(f"stdout={stdout}")
    blob = " | ".join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _operator_receipt_hash(receipt: Mapping[str, Any]) -> str:
    """Stable sha256 over the canonical receipt body (excluding receipt_hash itself).

    Issue #135: the receipt is the durable proof a production diff was produced through the
    bridge. The hash lets the diff-coverage gate bind a `git diff` path to exactly one receipt.
    """
    canonical = {k: v for k, v in receipt.items() if k != "receipt_hash"}
    blob = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _operator_run_diff_coverage(repo_path: Path, run_dir: Path) -> Dict[str, Any]:
    """Issue #135: every production diff path must be covered by an operator receipt.

    The bridge is the ONLY allowed mutation path. Any path `git diff` names that no operator
    receipt covers (i.e. was edited outside the bridge, or by a dev-cli failure that silently
    unlocked manual editing) makes the run non-concludable.
    """
    changed = _changed_paths(repo_path)
    covered: List[str] = []
    receipts: List[Dict[str, Any]] = []
    # Collect every operator receipt in this run (execute + batch lanes).
    for candidate in sorted(run_dir.glob("operator-receipt*.json")):
        try:
            receipts.append(_load_json(candidate))
        except (OSError, ValueError, TypeError):
            continue
    for receipt in receipts:
        status = str(receipt.get("status") or receipt.get("execution_state") or "")
        if status not in ("applied", "no_change"):
            continue
        covered.extend(str(p) for p in (receipt.get("changed_paths") or []) if str(p))
    covered_set = {str(p) for p in covered}
    uncovered = [p for p in changed if p not in covered_set]
    coverage_ok = not uncovered
    return {
        "changed_paths": changed,
        "covered_paths": sorted(covered_set),
        "uncovered_paths": uncovered,
        "coverage_ok": coverage_ok,
        "receipt_count": len(receipts),
    }


def conclude_run(repo: str, run_id: str, *, force: bool = False) -> Dict[str, Any]:
    """Gate run conclusion on full operator-receipt diff coverage (issue #135).

    `force=True` is the explicit human override (the safety policy's human gate) and still
    records the violation rather than silently passing.
    """
    status = read_status(repo, run_id)
    run_dir = Path(status["run_dir"])
    repo_path = Path(status["manifest"]["repo"]).resolve()
    coverage = _operator_run_diff_coverage(repo_path, run_dir)
    state = status["state"]
    if not coverage["coverage_ok"] and not force:
        raise RuntimeError(
            "cannot conclude run: production diff paths without an operator receipt: "
            + ", ".join(coverage["uncovered_paths"])
        )
    gate = {
        "kind": "operator_run_diff_coverage",
        "coverage_ok": coverage["coverage_ok"],
        "uncovered_paths": coverage["uncovered_paths"],
        "forced": bool(force),
        "checked_at": _now(),
    }
    state.setdefault("gates", []).append(gate)
    state["operator_run_gate"] = gate
    _write_json(run_dir / "state.json", state)
    _transition(
        run_dir, state, state.get("phase") or "done",
        "operator-run diff-coverage gate evaluated",
        receipt=str(run_dir / "state.json"),
        extra={"coverage": coverage},
    )
    return read_status(repo, run_id)


def execute_operator(repo: str, run_id: str, task_index: int = 1, *,
                      attempt_coordinator: Optional[AttemptCoordinator] = None,
                      guarded_attempt: Any = None) -> Dict[str, Any]:
    """Execute one planned task through the real dev-cli and persist an immutable receipt.

    `run` intentionally arms and dry-runs only.  This explicit tick is the mutation boundary;
    it cannot run without the mapper/plan/operator preflight artifacts created by `arm_run`.

    When both ``attempt_coordinator`` and ``guarded_attempt`` (a ``WorkItemAttempt``) are
    supplied (issue #288's guarded dispatch path, gated by ``SIMPLICIO_GUARDED_DISPATCH`` in
    ``_operator_dispatch_attempt``), the mutating dev-cli invocation runs through
    ``AttemptCoordinator.run_guarded`` instead of a raw ``subprocess.run`` -- a background
    thread heartbeats the lease for the life of the subprocess and kills it the instant the
    lease is no longer current, instead of letting a worker that lost its fence keep mutating
    the checkout (the #183 gap). ``LeaseLostDuringExecution`` propagates to the caller, which
    already treats any exception here as a receipted (not scheduler-crashing) failure.
    ``guarded_attempt`` is deliberately named apart from this function's own ``attempt``
    local (the per-task retry counter) so the two can never collide.
    """
    status = read_status(repo, run_id)
    if (status["state"].get("maintenance") or {}).get("disposition") == "backlog_only":
        raise RuntimeError("maintenance deferred: operator execution is blocked until explicit resume")
    run_dir = Path(status["run_dir"])
    repo_path = Path(status["manifest"]["repo"]).resolve()
    contract = _load_json(run_dir / "task-contract.json")
    tasks = contract.get("tasks") or []
    if task_index < 1 or task_index > len(tasks):
        raise ValueError(f"task index out of range: {task_index}")
    plan_path = run_dir / "plan.json"
    mapper_path = run_dir / "mapper-context.json"
    operator_path = run_dir / "operator-receipt.json"
    if not plan_path.exists() or not mapper_path.exists() or not operator_path.exists():
        raise RuntimeError("execution requires fresh mapper, plan, and operator preflight receipts")
    plan = _load_json(plan_path)
    before = _repo_fingerprint(repo_path)
    current = _repo_fingerprint(repo_path)
    planned_state = plan.get("repo_state") or {}
    plan_validation = validate_plan(plan, tasks, repo_path,
                                   contract_hash=contract.get("collection_hash", ""),
                                   current_state=current)
    if not plan_validation["valid"]:
        raise RuntimeError("plan validation failed before operator execution: " + ", ".join(plan_validation["errors"]))
    if planned_state and not _repo_state_equivalent(planned_state, current):
        raise RuntimeError("repository changed after planning; re-run mapper before execution")
    task = tasks[task_index - 1]
    # #694: every production item gets an authoritative route receipt before
    # mutation authority or an execution backend is selected.  The route is a
    # deterministic gate; Runtime remains the physical/policy owner.
    task_text = _task_goal(task)
    worker_capabilities = task.get("worker_capabilities") or task.get("capabilities") or ()
    worker_available = bool(worker_capabilities) or os.environ.get("SIMPLICIO_DETERMINISTIC_WORKER", "1").lower() not in {"0", "false", "no", "off"}
    capability_manifest = {
        "declared": normalize_capability_manifest(worker_capabilities),
        "deterministic_worker_available": worker_available,
    }
    capability_hash = capability_fingerprint(capability_manifest)
    route_path = run_dir / "execution-route.json"
    previous_route = None
    if route_path.exists():
        try:
            candidate = _load_json(route_path)
            if verify_route_hash(candidate):
                previous_route = candidate
        except (OSError, TypeError, ValueError):
            previous_route = None
    route_cache_status = "new"
    route_record = None
    if previous_route and route_receipt_is_current(previous_route, capability_manifest):
        route_record = previous_route
        route_cache_status = "reused"
    else:
        invalidation = {}
        if previous_route:
            route_cache_status = "invalidated"
            invalidation = {
                "status": "invalidated",
                "reason_code": (
                    "capability_manifest_changed"
                    if previous_route.get("capability_fingerprint")
                    else "capability_manifest_missing"
                ),
                "previous_receipt_sha": str(previous_route.get("receipt_sha") or ""),
                "previous_capability_fingerprint": str(previous_route.get("capability_fingerprint") or ""),
            }
        route = decide_route(
            task_text,
            has_deterministic_worker=worker_available,
            is_ambiguous=bool(task.get("ambiguous") or task.get("requires_semantic_review")),
        )
        route_record = route.to_dict()
        route_record.update({
            "run_id": run_id,
            "task_index": task_index,
            "task_id": str(task.get("id") or ""),
            "evidence_handles": sorted({
                str(value) for value in (
                    (plan.get("steps") or [])[task_index - 1].get("mapper_context_hash", ""),
                    (plan.get("steps") or [])[task_index - 1].get("context_pack_hash", ""),
                ) if str(value)
            }),
            "causal_ids": [run_id, str(task.get("id") or task_index)],
            "route_authority": "loop-runner",
            "capability_manifest": capability_manifest,
            "capability_fingerprint": capability_hash,
        })
        if invalidation:
            route_record["invalidation"] = invalidation
        route_record["receipt_sha"] = _execution_route_hash(
            {key: value for key, value in route_record.items() if key != "receipt_sha"}
        )
    if not verify_route_hash(route_record):
        raise RuntimeError("execution-route receipt failed deterministic hash verification")
    _write_json(route_path, route_record)
    operator_state = status["state"].setdefault("operator", {})
    operator_state["execution_route"] = route_record
    operator_state["execution_route_cache"] = {
        "status": route_cache_status,
        "capability_fingerprint": capability_hash,
        "previous_receipt_sha": str((previous_route or {}).get("receipt_sha") or ""),
    }
    _write_json(run_dir / "state.json", status["state"])
    attempt = int((status["state"] or {}).get("attempts", 0)) + 1
    # #284: mutation-authority gate, mandatory by default. execute_operator()
    # refuses to run without a valid planning-receipt.json whose mutation_authority
    # token matches THIS run/attempt/task-contract/plan identity -- any drift (stale
    # plan hash, rotated lease/fence, missing/invalid receipt) blocks fail-closed
    # instead of silently proceeding. Opt out only via an explicit falsy
    # SIMPLICIO_REQUIRE_MUTATION_AUTHORITY (see planning_gate.mutation_authority_required());
    # see simplicio_loop/planning_gate.py and scripts/planning_gate.py.
    if mutation_authority_required():
        # GitHub source drift: if the caller re-captured a fresh source snapshot
        # immediately before this tick (`scripts/planning_gate.py capture-source`,
        # written to `source-snapshot-current.json`), compare its hash against the
        # one the receipt/authority was minted with. Absent that file (local/
        # non-GitHub runs, or a caller that hasn't wired re-capture yet), this is a
        # no-op -- identical to previous behavior.
        current_source_hash = ""
        current_snapshot_path = run_dir / "source-snapshot-current.json"
        if current_snapshot_path.exists():
            try:
                current_source_hash = str((_load_json(current_snapshot_path).get("source") or {}).get("snapshot_hash") or "")
            except Exception:
                current_source_hash = ""
        authority_verdict = evaluate_mutation_authority(
            run_dir, run_id=run_id, attempt=attempt,
            task_contract_hash=str(contract.get("collection_hash") or _planning_content_hash(contract)),
            plan_hash=_planning_content_hash(plan),
            source_snapshot_hash=current_source_hash,
        )
        if not authority_verdict["ok"]:
            raise RuntimeError(
                "mutation authority required (SIMPLICIO_REQUIRE_MUTATION_AUTHORITY) but "
                f"{authority_verdict['reason_code']}: {authority_verdict['reason']}"
            )
    targets = (plan.get("steps") or [])[task_index - 1].get("candidate_targets") or []
    target = targets[0] if targets else status["state"].get("operator", {}).get("target", "")
    if not target:
        raise RuntimeError("plan has no authorized operator target")
    # Issue #135: the decided change is AC-scoped and MUST point at a plan target. Any target
    # expansion beyond authorized_targets routes back to the planner/impact gate before continuing.
    if target not in (targets or []):
        raise RuntimeError(
            "operator target '%s' is outside the plan's authorized_targets %s; "
            "route back to planner/impact gate before continuing" % (target, targets)
        )
    _preflight_operator(repo_path, run_dir)
    task_spec_path = run_dir / "task-spec.json"
    task_spec = _task_spec_payload(task)
    task_spec_hash = _task_spec_hash(task_spec)
    _write_json(task_spec_path, task_spec)
    lease = getattr(getattr(guarded_attempt, "lease", None), "lease_id", "")
    fence = getattr(getattr(guarded_attempt, "lease", None), "fencing_token", "")
    profile = _execution_profile()
    context_args, context_handoff = _context_handoff_args(
        repo_path,
        run_dir,
        attempt_id=f"{run_id}:attempt:{attempt}",
        lease_id=str(lease or ""),
        fencing_token=str(fence or ""),
        require_authorization=profile == "runtime-backed",
    )
    argv = _devcli_cmd(
        repo_path,
        "task",
        "--root",
        str(repo_path),
        "--task-spec",
        str(task_spec_path),
        "--mode",
        "integrated",
        "--target",
        target,
        "--json",
        "--bound-paths",
        target,
    )
    argv.extend(context_args)
    checkpoint = _capture_operator_checkpoint(run_dir, repo_path, targets or [target])
    # #285 remaining gap: this dispatch has a real guarded lease (when the caller wired
    # one) and a real repo checkout/branch on hand -- surface them on the event so
    # `_sync_github_lifecycle()` projects the actual lease/fencing token and branch onto
    # the CLAIMED comment instead of falling back to a blank/best-effort default.
    _emit_event(run_dir, status["state"], "worker_claimed",
                receipt=str(run_dir / "task-contract.json"),
                task_id=str(task.get("id") or ""),
                ac_ids=_task_ac_ids(task),
                message="operator worker claimed task",
                lease_id=str(getattr(getattr(guarded_attempt, "lease", None), "lease_id", "") or ""),
                fencing_token=str(getattr(getattr(guarded_attempt, "lease", None), "fencing_token", "") or ""),
                branch=_git_current_branch(repo_path))
    if item_context := (status["state"].get("operator") or {}).get("worktree_context"):
        _emit_event(run_dir, status["state"], "worktree_created",
                    receipt=str(item_context.get("lock_receipt") or operator_path),
                    message="isolated worktree context available", worktree=item_context)
    op_env = _devcli_env(repo_path, _operator_env())
    provider_config = {
        "model": op_env.get("SIMPLICIO_MODEL", ""),
        "effort": op_env.get("SIMPLICIO_CODEX_EFFORT", ""),
    }
    effect_adapter = _runtime_effect_adapter(repo_path, profile)
    effect_request = _build_effect_request(
        repo_path, run_id, task_index, task, attempt, targets, route_record, guarded_attempt,
        canonical_plan=(
            load_canonical_plan(plan["canonical_plan"], expected_digest=str(plan.get("canonical_plan_digest") or ""))
            if isinstance(plan.get("canonical_plan"), Mapping) else None
        ),
    )
    effect_outcome = _execute_operator_effect(
        profile=profile,
        adapter=effect_adapter,
        request=effect_request,
        argv=argv,
        env=op_env,
        repo_path=repo_path,
        attempt_coordinator=attempt_coordinator,
        guarded_attempt=guarded_attempt,
    )
    returncode = effect_outcome["returncode"]
    stdout = effect_outcome["stdout"]
    stderr = effect_outcome["stderr"]
    source = effect_outcome["source"]
    effect_receipt = effect_outcome.get("effect_receipt")
    uncertain = bool(effect_outcome.get("uncertain"))
    after = _repo_fingerprint(repo_path)
    changed = _changed_paths(repo_path)
    rollback = {"attempted": False, "restored": False, "reason": "not_needed"}
    if returncode != 0 and not uncertain:
        rollback = _restore_operator_checkpoint(checkpoint, repo_path, changed)
        if rollback.get("restored"):
            changed = _changed_paths(repo_path)
            after = _repo_fingerprint(repo_path)
    if uncertain:
        execution_state = "uncertain"
        no_change_proof = None
    elif returncode == 0 and not changed:
        execution_state = "no_change"
        no_change_proof = {
            "satisfying_state": "repository already satisfied the AC; no production diff produced",
            "measured_at": _now(),
            "evidence": str(after.get("tree_hash", "")),
        }
    else:
        execution_state = "applied" if returncode == 0 else "blocked"
        no_change_proof = None
    receipt = {
        "schema": OPERATOR_RECEIPT_SCHEMA,
        "mode": "execute",
        "tool": "simplicio-dev-cli",
        "execution_state": execution_state,
        "status": execution_state,
        "attempt": attempt,
        "retry_budget": 3,
        "target": target,
        "authorized_targets": targets,
        "target_within_repo": True,
        "goal": _task_goal(task),
        "argv": argv,
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": returncode is None,
        "started_at": _now(),
        "finished_at": _now(),
        # #288: receipt_verifier.OPERATOR_RECEIPT_SCHEMA requires "measured_at" for its
        # freshness check. This receipt never carried it, so every real (non-mocked)
        # execute_operator() dispatch was permanently INVALID_SCHEMA/MISSING_FIELD in
        # _verify_worker_receipt_pair() -- the merge gate below could never fire for a genuine
        # attempt. Same instant as finished_at; this is a receipt-completeness fix, not a new
        # measurement.
        "measured_at": _now(),
        "source": source,
        "context_handoff": context_handoff,
        "provider_config": provider_config,
        "execution_profile": profile,
        "executor_profile": (effect_receipt or {}).get("executor_profile", profile),
        "effect_receipt": effect_receipt,
        "effect_transaction_id": (effect_receipt or {}).get("transaction_id", ""),
        "effect_correlation_id": (effect_receipt or {}).get("correlation_id", ""),
        "checkpoint": checkpoint,
        "rollback": rollback,
        "failure_fingerprint": "" if returncode == 0 else _operator_failure_fingerprint(returncode, stderr, stdout),
        "task_contract_hash": contract.get("collection_hash", ""),
        "plan_hash": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "mapper_pack_hash": plan.get("mapper_pack_hash", ""),
        "repo_state_before": before,
        "repo_state_after": after,
        "changed_paths": changed,
        "diff_hash": after.get("tree_hash", ""),
        "no_change_proof": no_change_proof,
        "task_spec_path": str(task_spec_path),
        "task_spec_hash": task_spec_hash,
    }
    receipt["receipt_hash"] = _operator_receipt_hash(receipt)
    _write_json(operator_path, receipt)
    state = status["state"]
    if rollback.get("restored"):
        _emit_event(run_dir, state, "rollback", receipt=str(operator_path),
                    blocker=str(rollback.get("reason") or "operator execution failed"),
                    message="operator changes rolled back")
    state["operator"] = {
        "ready": returncode == 0 and not uncertain,
        "receipt": str(operator_path),
        "target": target,
        "execution_state": receipt["execution_state"],
    }
    state["current_action"] = "operator_executed" if returncode == 0 else "operator_failed"
    state["next_action"] = "watcher_behavioral_verification" if returncode == 0 else "repair_operator_or_plan"
    state["attempts"] = int(state.get("attempts", 0)) + 1
    _write_json(run_dir / "state.json", state)
    _transition(run_dir, state, "validating" if returncode == 0 else "blocked",
                "dev-cli execution receipt persisted", receipt=str(operator_path),
                extra={"changed_paths": changed})
    if returncode == 0:
        evidence = build_evidence_receipt(str(run_dir))
        _write_json(run_dir / "evidence-receipt.json", evidence)
        state = _load_json(run_dir / "state.json")
        state["evidence"] = {"ready": False, "receipt": str(run_dir / "evidence-receipt.json"), "status": evidence.get("status", "UNVERIFIED")}
        _write_json(run_dir / "state.json", state)
        _emit_event(run_dir, state, "operator_receipt", receipt=str(operator_path),
                    message="operator execution receipt persisted")
        _emit_event(run_dir, state, "test_gate", receipt=str(run_dir / "evidence-receipt.json"),
                    blocker="" if evidence.get("status") == "VERIFIED" else "evidence_unverified",
                    message="test and evidence gate evaluated", status=evidence.get("status", "UNVERIFIED"))
    return read_status(repo, run_id)


def verify_run(repo: str, run_id: str) -> Dict[str, Any]:
    """Run the independent watcher and advance a run without a manual tick."""
    status = read_status(repo, run_id)
    run_dir = Path(status["run_dir"])
    repo_path = Path(status["manifest"]["repo"]).resolve()
    state = status["state"]
    if state.get("phase") in {"done", "cancelled"}:
        return status
    watcher = repo_path / "scripts" / "watcher_verify.py"
    if not watcher.exists():
        state["blockers"] = ["watcher_verify.py is unavailable"]
        state["current_action"] = "watcher_unavailable"
        state["next_action"] = "inspect_and_recover"
        _write_json(run_dir / "state.json", state)
        _transition(run_dir, state, "blocked", "independent watcher is unavailable", receipt=str(run_dir / "state.json"))
        return read_status(repo, run_id)
    _transition(run_dir, state, "watching", "automatic conduct reached independent verification", receipt=str(run_dir / "operator-receipt.json"))
    env = dict(os.environ)
    env["SIMPLICIO_RUN_DIR"] = str(run_dir)
    env["SIMPLICIO_LOOP_REPO"] = str(repo_path)
    env["SIMPLICIO_LOOP_DIR"] = str(run_dir / "loop")
    result = subprocess.run([sys.executable, str(watcher), "verify"], cwd=str(repo_path), capture_output=True, text=True, timeout=180, env=env)
    output = redact_sensitive_text((result.stdout or "") + (result.stderr or "")).strip()
    _write_json(run_dir / "watcher-receipt.json", {"schema": "simplicio.watcher-invocation/v1", "returncode": result.returncode, "output": output, "receipt": str(run_dir / "loop" / "watcher_state.json"), "checked_at": _now()})
    watcher_path = run_dir / "loop" / "watcher_state.json"
    watcher_state = _load_json(watcher_path) if watcher_path.exists() else {}
    if result.returncode != 0 or watcher_state.get("status") != "MEASURED" or not watcher_state.get("match"):
        state = read_status(repo, run_id)["state"]
        state["blockers"] = [watcher_state.get("reported") or output or "watcher verification failed"]
        state["current_action"] = "watcher_failed"
        state["next_action"] = "inspect_and_recover"
        state["evidence"] = {"ready": False, "receipt": str(watcher_path), "status": "UNVERIFIED"}
        _write_json(run_dir / "state.json", state)
        _transition(run_dir, state, "blocked", "independent watcher rejected the run", receipt=str(watcher_path))
        return read_status(repo, run_id)
    state = read_status(repo, run_id)["state"]
    state["evidence"] = {"ready": True, "receipt": str(watcher_path), "status": "MEASURED"}
    state["current_action"] = "watcher_verified"
    state["next_action"] = "delivery_reconciliation"
    _write_json(run_dir / "state.json", state)
    _transition(run_dir, state, "delivering", "independent watcher measured all acceptance criteria", receipt=str(watcher_path))
    delivered = reconcile_delivery(repo, run_id, "verified", source_kind="local")
    if not delivered["state"].get("delivery", {}).get("ready"):
        return delivered
    state = delivered["state"]
    state["current_action"] = "run_verified"
    state["next_action"] = "none"
    state["completion"] = {"ready": True, "receipt": str(watcher_path), "verdict": "VERIFIED", "reason_code": "watcher_and_delivery_verified", "tag": "MEASURED"}
    _write_json(run_dir / "state.json", state)
    # wi612 (#612): Quality Matrix + Completion Oracle obrigatorios antes do done (elimina bypass).
    from . import oracle as _oracle
    _qm_ok, _qm_gate, _qm_verdict = _oracle._quality_matrix_gate(run_dir)
    if not _qm_ok:
        state = read_status(repo, run_id)["state"]
        state["blockers"] = [_qm_verdict.get("reason", "quality matrix incomplete")]
        state["current_action"] = "quality_matrix_failed"
        state["next_action"] = "inspect_and_recover"
        state["evidence"] = {"ready": False, "receipt": str(run_dir / "quality-matrix.json"), "status": "UNVERIFIED"}
        _write_json(run_dir / "state.json", state)
        _transition(run_dir, state, "blocked", "quality matrix gate rejected the run", receipt=str(run_dir / "quality-matrix.json"))
        return read_status(repo, run_id)
    _oracle_matrix = _oracle.evaluate_matrix(str(run_dir / "loop"), str(run_dir))
    if not _oracle_matrix.get("parity") or not all(a["ready"] for a in _oracle_matrix.get("adapters", [])):
        state = read_status(repo, run_id)["state"]
        state["blockers"] = ["completion oracle incomplete: " + str(_oracle_matrix.get("signature"))]
        state["current_action"] = "oracle_failed"
        state["next_action"] = "inspect_and_recover"
        state["evidence"] = {"ready": False, "receipt": str(run_dir / "oracle-matrix.json"), "status": "UNVERIFIED"}
        _write_json(run_dir / "state.json", state)
        _transition(run_dir, state, "blocked", "completion oracle rejected the run", receipt=str(run_dir / "oracle-matrix.json"))
        return read_status(repo, run_id)
    _transition(run_dir, state, "done", "automatic task-to-verify conduct completed", receipt=str(watcher_path))
    return read_status(repo, run_id)


def _conduct_run(repo: str, task_path: str, delivery: str = "verified", max_iterations: int = 12, *, retry_budget: int = 3, quality_provider: Optional[str] = None, quality_policy: str = "strict-default") -> Dict[str, Any]:
    """Arm, execute, and independently verify one run as one durable operation.

    Issue #279: this boundary must never leave a run partially armed.  Either the full
    mapper -> plan -> operator preflight -> batch chain succeeds, or the run is left (and
    reported) explicitly ``blocked`` with a diagnostic receipt.  ``execute_operator_batch``
    already fails closed and persists a batch-preflight-block diagnostic before raising when
    the receipt chain is missing or stale, but that exception must not escape uncaught here --
    an uncaught exception is itself a partially-armed, undiagnosed state from the CLI's
    perspective.
    """
    armed = arm_run(repo, task_path, delivery, max_iterations)
    run_id = armed["manifest"]["run_id"]
    if armed["state"].get("phase") == "blocked":
        return armed
    try:
        batch = execute_operator_batch(repo, run_id, max_workers=1, retry_budget=retry_budget, auto_fan_out=False)
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        status = read_status(repo, run_id)
        run_dir = Path(status["run_dir"])
        state = status["state"]
        if state.get("phase") != "blocked":
            # Defensive fallback: execute_operator_batch's own preflight boundary is
            # expected to have already persisted a blocked diagnostic before raising, but
            # guarantee the run never surfaces as anything other than explicitly blocked.
            _transition(run_dir, state, "blocked",
                        "batch dispatch failed before dispatch", extra={"error": str(exc)})
            status = read_status(repo, run_id)
        return status
    status = read_status(repo, run_id)
    if batch.get("failed_task_indices") or status["state"].get("phase") == "blocked":
        return status
    # Issue #613: a quality provider is MANDATORY between execution and the
    # watcher/delivery/Completion Oracle. Fail-closed: BLOCKED when no provider
    # is supplied (never a silent skip). This makes the quality gate non-optional.
    if not quality_provider:
        run_dir = Path(status["run_dir"])
        state = status["state"]
        state["blockers"] = [
            "quality provider mandatory per #613: conduct_run requires a non-None "
            "quality_provider between execution and verify/oracle"
        ]
        state["current_action"] = "quality_provider_missing"
        state["next_action"] = "inspect_and_recover"
        state["evidence"] = {
            "ready": False,
            "receipt": str(run_dir / "quality-matrix.json"),
            "status": "UNVERIFIED",
        }
        _write_json(run_dir / "state.json", state)
        _transition(
            run_dir, state, "blocked",
            "quality provider mandatory per #613: none supplied",
            receipt=str(run_dir / "quality-matrix.json"),
        )
        return read_status(repo, run_id)
    # Issue #613: mandatory quality provider runs AFTER execution and BEFORE
    # the watcher/delivery/Completion Oracle. Fail-closed: BLOCKED on any
    # absent/incompatible/crashing/timed-out provider -- never a silent fallback.
    if quality_provider:
        from .quality_provider import conduct_quality
        run_dir = Path(status["run_dir"])
        head = status.get("manifest", {}).get("head", "") or ""
        diff_hash = status.get("manifest", {}).get("diff_hash", "") or ""
        q = conduct_quality(
            repo, run_id,
            quality_provider=quality_provider, quality_policy=quality_policy,
            attempt=status["state"].get("attempt", 1), head=head, diff_hash=diff_hash,
        )
        if q.get("status") == "BLOCKED":
            state = read_status(repo, run_id)["state"]
            state["blockers"] = [q.get("reason", "quality provider blocked the run")]
            state["current_action"] = "quality_blocked"
            state["next_action"] = "inspect_and_recover"
            state["evidence"] = {
                "ready": False,
                "receipt": str(run_dir / "quality-matrix.json"),
                "status": "UNVERIFIED",
            }
            _write_json(run_dir / "state.json", state)
            _transition(run_dir, state, "blocked",
                        "quality provider blocked the run (fail-closed)",
                        receipt=str(run_dir / "quality-matrix.json"))
            return read_status(repo, run_id)
        # A FAIL provider returns to recovery/implementation per the issue spec
        # (not a direct provider fix); surface it and stop short of verify.
        if q.get("status") == "FAIL":
            state = read_status(repo, run_id)["state"]
            state["blockers"] = [q.get("detail", "quality provider reported FAIL")]
            state["current_action"] = "quality_failed"
            state["next_action"] = "inspect_and_recover"
            _write_json(run_dir / "state.json", state)
            _transition(run_dir, state, "blocked",
                        "quality provider reported FAIL -> recovery",
                        receipt=str(run_dir / "quality-matrix.json"))
            return read_status(repo, run_id)
    return verify_run(repo, run_id)


def conduct_run(repo: str, task_path: str, delivery: str = "verified", max_iterations: int = 12, *, retry_budget: int = 3, quality_provider: Optional[str] = None, quality_policy: str = "strict-default") -> Dict[str, Any]:
    """Conduct a run and attach its public Completion-Oracle-derived outcome."""
    status = _conduct_run(repo, task_path, delivery, max_iterations, retry_budget=retry_budget,
                          quality_provider=quality_provider, quality_policy=quality_policy)
    from .run_outcome import persist_run_outcome
    status["outcome"] = persist_run_outcome(status)
    return status


def _operator_worker_limit(requested: Optional[int], item_count: int) -> int:
    """Resolve a bounded worker count without silently creating an empty pool."""
    if item_count <= 0:
        return 0
    if requested is None or requested <= 0:
        raw = os.environ.get("SIMPLICIO_LOOP_OPERATOR_WORKERS", "").strip()
        try:
            requested = int(raw) if raw else min(DEFAULT_OPERATOR_WORKERS, os.cpu_count() or 1)
        except ValueError:
            requested = min(DEFAULT_OPERATOR_WORKERS, os.cpu_count() or 1)
    return max(1, min(int(requested), item_count))


def _worktree_task_spec(item: Mapping[str, Any]) -> Any:
    """Build the queue's impact contract without importing it at module load time.

    ``runner`` is also shipped as a standalone bundle, so importing the scripts package
    eagerly would make the existing operator API fail in installations that do not ship the
    optional isolation adapter.  The late import keeps that adapter genuinely optional while
    still passing the real ``TaskSpec`` to ``WorktreeQueue`` when it is available.
    """
    try:
        from scripts.worktree_queue import TaskSpec
    except ImportError:  # pragma: no cover - direct scripts/ execution fallback
        from worktree_queue import TaskSpec
    raw = item.get("task_spec")
    if isinstance(raw, TaskSpec):
        return raw
    payload = dict(raw or {}) if isinstance(raw, Mapping) else {}
    task_id = str(item.get("task_id") or "task-%s-%s" % (item.get("run_id"), item.get("task_index")))
    payload.setdefault("id", task_id)
    payload.setdefault("goal", str(item.get("goal") or ""))
    return TaskSpec.from_mapping(payload)


def _allocation_context(allocation: Any, item: Mapping[str, Any]) -> Dict[str, Any]:
    """Reduce an Allocation to JSON-safe, persisted operator context."""
    def value(name: str, default: Any = "") -> Any:
        if isinstance(allocation, Mapping):
            return allocation.get(name, default)
        return getattr(allocation, name, default)

    context = {
        "schema": "simplicio.operator-worktree-context/v1",
        "task_id": str(value("task_id", item.get("task_id") or "")),
        "run_id": str(value("run_id", item.get("run_id") or "")),
        "mode": str(value("mode", item.get("isolation", "worktree")) or item.get("isolation", "worktree")),
        "path": str(value("path", "") or ""),
        "branch": str(value("branch", "") or ""),
        "base_sha": str(value("base_sha", "") or ""),
        "head_sha": str(value("head_sha", "") or ""),
        "tree_sha": str(value("tree_sha", "") or ""),
        "lane": str(value("lane", "") or ""),
        "reattached": bool(value("reattached", False)),
        "lock_receipt": str(value("lock_receipt", "") or ""),
        "source_repo": str(item.get("source_repo") or item.get("repo") or ""),
        "source_run_id": str(item.get("source_run_id") or item.get("run_id") or ""),
    }
    return context


def _persist_isolated_run_context(item: Dict[str, Any], context: Dict[str, Any]) -> None:
    """Persist queue context and clone run receipts into an isolated checkout.

    The copy is filesystem-only (no Git subprocess), making this path deterministic in unit
    tests and safe for callers that provide a fake queue.  If the source run is unavailable,
    the context receipt is still written; the operator then fails closed at its normal
    preflight boundary rather than manufacturing a success.
    """
    path = str(context.get("path") or "")
    source_repo = Path(str(context.get("source_repo") or item.get("repo") or "")).resolve()
    run_id = str(item.get("run_id") or context.get("source_run_id") or "")
    if not path:
        return
    target_root = Path(path).resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    context_dir = target_root / ".orchestrator" / "dispatch-context"
    context_dir.mkdir(parents=True, exist_ok=True)
    context_path = context_dir / (str(context.get("task_id") or item.get("task_index")) + ".json")
    context["context_path"] = str(context_path)
    _write_json(context_path, context)

    source_state_path = source_repo / ".simplicio" / "loop-runs" / run_id / "state.json"
    if source_state_path.exists():
        try:
            source_run = source_state_path.parent
            source_state = _load_json(source_state_path)
            _emit_event(source_run, source_state, "worktree_created",
                        receipt=str(context.get("lock_receipt") or context_path),
                        task_id=str(context.get("task_id") or item.get("task_id") or ""),
                        message="isolated worktree context persisted", worktree=context)
        except (OSError, ValueError, TypeError):
            # The worker's normal receipts remain authoritative if the coordinator is gone.
            pass

    source_run = source_repo / ".simplicio" / "loop-runs" / run_id
    target_run = target_root / ".simplicio" / "loop-runs" / run_id
    if source_run.is_dir() and target_root != source_repo and not target_run.exists():
        target_run.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_run, target_run)
    manifest_path = target_run / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = _load_json(manifest_path)
            manifest["repo"] = str(target_root)
            manifest["run_id"] = run_id
            _write_json(manifest_path, manifest)
        except (OSError, ValueError, TypeError):
            # The operator's ordinary preflight will emit a durable failure receipt.
            pass


def _prepare_worktree_contexts(normalized: List[Dict[str, Any]], worktree_queue: Any) -> None:
    """Allocate/persist optional worktree contexts before any worker starts."""
    if worktree_queue is None or not normalized:
        return
    specs = [_worktree_task_spec(item) for item in normalized]
    register = getattr(worktree_queue, "register_tasks", None)
    if callable(register):
        try:
            register(specs)
        except Exception as exc:
            for item in normalized:
                item["worktree_error"] = f"{type(exc).__name__}: {exc}"
            return
    for item, spec in zip(normalized, specs):
        isolation = str(item.get("isolation") or "worktree").strip().lower()
        if isolation not in {"worktree", "shared"}:
            item["worktree_error"] = "ValueError: unsupported worktree isolation mode"
            continue
        if isolation == "shared":
            # WorktreeQueue intentionally holds one shared-checkout lock.  Defer allocation
            # until this item reaches the serial worker lane so the next item can acquire it
            # only after ``_release_shared_context`` runs.
            item["worktree_deferred"] = True
            item["isolation_key"] = "%s:%s" % (item.get("repo"), item.get("run_id"))
            continue
        try:
            allocation = worktree_queue.allocate(spec)
        except Exception as exc:
            item["worktree_error"] = f"{type(exc).__name__}: {exc}"
            continue
        context = _allocation_context(allocation, item)
        item["worktree_context"] = context
        item["source_repo"] = str(item.get("repo") or "")
        item["source_run_id"] = str(item.get("run_id") or "")
        # Worktree workers get their own run tree; shared mode intentionally retains the
        # original path and is serialized by the isolation key below.
        if context["mode"] == "worktree" and context["path"]:
            try:
                _persist_isolated_run_context(item, context)
            except Exception as exc:
                item["worktree_error"] = f"{type(exc).__name__}: {exc}"
            item["repo"] = context["path"]
            item["isolation_key"] = context["path"]
        else:
            item["isolation_key"] = "%s:%s" % (item.get("repo"), item.get("run_id"))
        recorder = getattr(worktree_queue, "record_context", None)
        if callable(recorder):
            try:
                recorder(context["task_id"], context)
            except Exception as exc:
                # Context persistence is a safety gate: do not run an unreceipted isolated
                # worker.  This preserves fail-closed behavior without changing the API.
                item["worktree_error"] = f"{type(exc).__name__}: {exc}"


def _ensure_deferred_worktree_context(item: Dict[str, Any], worktree_queue: Any) -> None:
    """Acquire one deferred shared-checkout lease immediately before execution."""
    if not item.get("worktree_deferred") or item.get("worktree_context") or item.get("worktree_error"):
        return
    try:
        spec = _worktree_task_spec(item)
        allocation = worktree_queue.allocate(spec, isolation="shared", shared_policy=True)
        context = _allocation_context(allocation, item)
        item["worktree_context"] = context
        item["source_repo"] = str(item.get("repo") or "")
        item["source_run_id"] = str(item.get("run_id") or "")
        _persist_isolated_run_context(item, context)
        recorder = getattr(worktree_queue, "record_context", None)
        if callable(recorder):
            recorder(context["task_id"], context)
    except Exception as exc:
        item["worktree_error"] = f"{type(exc).__name__}: {exc}"


def _release_shared_context(item: Mapping[str, Any], worktree_queue: Any) -> None:
    context = item.get("worktree_context") or {}
    if str(context.get("mode") or "") != "shared":
        return
    teardown = getattr(worktree_queue, "teardown", None)
    task_id = str(context.get("task_id") or item.get("task_id") or "")
    if callable(teardown) and task_id:
        try:
            teardown(task_id)
        except Exception:
            # Never mask the operator receipt with cleanup noise.
            pass


def _operator_dispatch_item(item: Mapping[str, Any]) -> Dict[str, Any]:
    """Normalize one typed operator dispatch item.

    The adapter deliberately accepts only the real ``execute_operator`` boundary.  In
    particular, it has no command/echo fallback: callers that need a dry run must arm a
    run first and use that run's normal preflight receipts.
    """
    repo = str(item.get("repo") or "").strip()
    run_id = str(item.get("run_id") or "").strip()
    try:
        task_index = int(item.get("task_index"))
    except (TypeError, ValueError) as exc:
        raise ValueError("operator dispatch task_index must be an integer") from exc
    if not repo or not run_id or task_index < 1:
        raise ValueError("operator dispatch items require repo, run_id, and a positive task_index")
    normalized = {
        "repo": str(Path(repo).resolve()),
        "run_id": run_id,
        "task_index": task_index,
        "worker_id": str(item.get("worker_id") or f"operator-{task_index}"),
        "task_id": str(item.get("task_id") or f"task-{run_id}-{task_index}"),
    }
    # An isolation key is intentionally explicit.  Two tasks in one run share state.json,
    # operator-receipt.json, and the working tree and therefore cannot safely overlap until
    # the worktree adapter supplies separate contexts.
    normalized["isolation_key"] = str(item.get("isolation_key") or normalized["repo"])
    normalized["isolation"] = str(item.get("isolation") or "worktree")
    if isinstance(item.get("task_spec"), Mapping):
        normalized["task_spec"] = dict(item["task_spec"])
    if isinstance(item.get("operator_context"), Mapping):
        normalized["operator_context"] = dict(item["operator_context"])
    if item.get("distributed_queue") is not None:
        normalized["distributed_queue"] = item["distributed_queue"]
    if isinstance(item.get("agent_identity"), Mapping):
        normalized["agent_identity"] = dict(item["agent_identity"])
    if isinstance(item.get("context_pack"), Mapping):
        normalized["context_pack"] = dict(item["context_pack"])
    if item.get("source_repo"):
        normalized["source_repo"] = str(item["source_repo"])
    if item.get("source_run_id"):
        normalized["source_run_id"] = str(item["source_run_id"])
    if item.get("worktree_context"):
        normalized["worktree_context"] = dict(item["worktree_context"])
    if item.get("worktree_error"):
        normalized["worktree_error"] = str(item["worktree_error"])
    return normalized


def _verify_worker_receipt_pair(operator_receipt_path: str, evidence_receipt_path: str) -> Dict[str, str]:
    """Gate `receipt_status` on real content/schema/hash/freshness/provenance (issue #288).

    Previously this reduced to ``Path(receipt).is_file() and Path(evidence_receipt).is_file()``
    -- an empty ``{}`` file passed just as readily as a genuine receipt. Both receipts are now
    parsed and run through ``receipt_verifier.verify_receipt`` against the schema each producer
    (``_prepare_operator_receipt`` / ``evidence.py::build_evidence_receipt``) actually emits.
    Only a fully verified pair returns ``VERIFIED``; every other case names a specific,
    non-existence reason (``STALE``, ``TAMPERED``, ``INVALID_SCHEMA``, ``MISSING_FIELD``, or the
    legacy ``UNVERIFIED`` when a path is simply absent).
    """
    if not operator_receipt_path or not evidence_receipt_path:
        return {"status": "UNVERIFIED", "reason": "operator or evidence receipt path missing"}
    op_path = Path(operator_receipt_path)
    ev_path = Path(evidence_receipt_path)
    if not op_path.is_file() or not ev_path.is_file():
        return {"status": "UNVERIFIED", "reason": "operator or evidence receipt file missing"}
    try:
        operator_payload = json.loads(op_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"status": ReceiptStatus.INVALID_SCHEMA, "reason": f"operator receipt unreadable: {exc}"}
    try:
        evidence_payload = json.loads(ev_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"status": ReceiptStatus.INVALID_SCHEMA, "reason": f"evidence receipt unreadable: {exc}"}

    now = time.time()
    operator_verdict = verify_receipt(
        operator_payload, schema=_OPERATOR_RECEIPT_CONTENT_SCHEMA,
        max_age_seconds=RECEIPT_MAX_AGE_SECONDS, now=now,
    )
    if not operator_verdict.verified:
        return {"status": operator_verdict.status, "reason": f"operator receipt: {operator_verdict.reason}"}
    evidence_verdict = verify_receipt(
        evidence_payload, schema=_EVIDENCE_RECEIPT_CONTENT_SCHEMA,
        max_age_seconds=RECEIPT_MAX_AGE_SECONDS, now=now,
    )
    if not evidence_verdict.verified:
        return {"status": evidence_verdict.status, "reason": f"evidence receipt: {evidence_verdict.reason}"}
    return {
        "status": ReceiptStatus.VERIFIED,
        "reason": "operator and evidence receipts passed content/schema/hash/freshness/provenance checks",
    }


def _test_only_stall_before_dispatch(task_id: str) -> None:
    """Test-only hook (issue #288 cross-process recovery test): block one named task for a
    controlled number of real wall-clock seconds before it claims/executes.

    This exists solely so a test can start a real orchestrator OS process, let it durably
    journal an earlier task, and then kill the process (a genuine crash, not a simulated
    exception) while it is deterministically stalled mid-batch on a *different* task --
    proving a restarted orchestrator resumes/reconciles cleanly. It is a no-op unless both
    ``SIMPLICIO_LOOP_TEST_SLOW_TASK_ID`` matches this exact task and
    ``SIMPLICIO_LOOP_TEST_SLOW_TASK_SECONDS`` is set, mirroring the project's existing
    ``SIMPLICIO_LOOP_FAKE_*`` opt-in test hooks -- never active in a normal run.
    """
    target = os.environ.get("SIMPLICIO_LOOP_TEST_SLOW_TASK_ID", "").strip()
    if not target or target != str(task_id).strip():
        return
    try:
        seconds = float(os.environ.get("SIMPLICIO_LOOP_TEST_SLOW_TASK_SECONDS", "0") or "0")
    except ValueError:
        seconds = 0.0
    if seconds > 0:
        time.sleep(seconds)


def _remote_worker_dispatch_enabled() -> bool:
    """#286: once the queue is a genuine network ``HTTPRemoteQueue``, the coordinator must
    not execute the operator in its own process -- it enqueues the task envelope and waits
    for an independent ``RemoteWorkerDaemon`` (a different device/process, reachable only
    over the wire) to pull, claim, run, and complete it. This is the fix for the exact gap
    issue #286 named: "execute_operator_batch() cria HTTPRemoteQueue, mas continua
    submetendo _operator_dispatch_attempt() a um ThreadPoolExecutor local; o proprio
    coordenador chama execute_operator()."

    ``SQLiteRemoteQueue`` (issue #288's co-located, same-process guarded-dispatch path) is
    deliberately unaffected -- that queue backend models a single-host attempt coordinator,
    not a remote worker, so every existing #288 test using it keeps its current behavior.
    Opt out with ``SIMPLICIO_REMOTE_WORKER_ONLY=0`` only for a deliberate same-host smoke
    test that wants the old in-process shortcut against a real HTTP queue.
    """
    return str(os.environ.get("SIMPLICIO_REMOTE_WORKER_ONLY") or "1").strip().lower() not in (
        "0", "false", "no", "off", "disabled",
    )


def _operator_dispatch_attempt_remote_worker(
    item: Mapping[str, Any], common: Dict[str, Any], queue: HTTPRemoteQueue, started: float,
) -> Dict[str, Any]:
    """Enqueue-and-wait dispatch for a genuine remote (``HTTPRemoteQueue``) worker (#286).

    The coordinator itself never claims and never calls ``execute_operator()`` here -- it
    publishes the immutable task envelope once (idempotent: ``enqueue`` is a no-op if the
    task_id already exists) and polls ``queue.task()`` (the same authority a remote
    ``RemoteWorkerDaemon`` mutates) until the task reaches a terminal ``completed`` status or
    the dispatch timeout elapses. A timeout is reported as a specific, non-fabricated failure
    (``remote_worker_timeout``) rather than silently falling back to local execution.
    """
    task_id = common["task_id"]
    context_pack = dict(item.get("context_pack") or {})
    payload = {
        "run_id": common["run_id"], "worker_id": common["worker_id"],
        "task_index": common["task_index"], "goal": context_pack.get("goal", ""),
        "acs": list(context_pack.get("acs") or ()),
        "depends_on": list(context_pack.get("depends_on") or ()),
        "allowed_paths": list(context_pack.get("allowed_paths") or ()),
        "issue_ref": context_pack.get("issue_ref", ""), "issue_url": context_pack.get("issue_url", ""),
        "context_pack": context_pack,
        "worktree_context": dict(item.get("worktree_context") or {}),
    }
    common["dispatch_mode"] = "remote_worker_pull"
    try:
        queue.enqueue(task_id, payload)
    except (QueueConflict, QueueUnavailable, ValueError) as exc:
        return {**common, "status": "failed", "phase": "blocked", "execution_state": "error",
                "receipt": "", "operator_receipt": "", "evidence_receipt": "",
                "receipt_status": "UNVERIFIED", "attempt": 0,
                "reason_code": "remote_enqueue_failed", "error": str(exc), "dead_letter": True,
                "started_at": started, "finished_at": _now()}

    timeout = float(os.environ.get("SIMPLICIO_REMOTE_DISPATCH_TIMEOUT_SECONDS", "3600"))
    poll_interval = float(os.environ.get("SIMPLICIO_REMOTE_DISPATCH_POLL_INTERVAL_SECONDS", "2"))
    deadline = time.monotonic() + max(0.0, timeout)
    task_state: Dict[str, Any] = {}
    while True:
        try:
            task_state = queue.task(task_id)
        except (QueueUnavailable, KeyError) as exc:
            return {**common, "status": "failed", "phase": "blocked", "execution_state": "paused",
                    "receipt": "", "operator_receipt": "", "evidence_receipt": "",
                    "receipt_status": "UNVERIFIED", "attempt": 0,
                    "reason_code": "network_paused", "error": str(exc), "dead_letter": True,
                    "started_at": started, "finished_at": _now()}
        if str(task_state.get("status") or "") == "completed":
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))

    if str(task_state.get("status") or "") != "completed":
        return {**common, "status": "failed", "phase": "blocked", "execution_state": "timeout",
                "receipt": "", "operator_receipt": "", "evidence_receipt": "",
                "receipt_status": "UNVERIFIED", "attempt": 0,
                "reason_code": "remote_worker_timeout",
                "error": "no remote worker completed task %s within %.0fs" % (task_id, timeout),
                "dead_letter": True, "started_at": started, "finished_at": _now()}

    lease_info = dict(task_state.get("lease") or {})
    receipt = str(lease_info.get("receipt_ref") or "")
    # The remote worker's evidence receipt lives on its own device; the coordinator only
    # treats it as readable when both files genuinely exist on this filesystem (true, for
    # instance, of the same-host loopback proxy this repo's E2E uses). A cross-device
    # deployment without a receipt-fetch endpoint honestly reports UNVERIFIED here rather
    # than fabricating a VERIFIED pair it cannot see.
    evidence_receipt = str(Path(receipt).parent / "evidence-receipt.json") if receipt else ""
    if not (evidence_receipt and Path(evidence_receipt).is_file()):
        evidence_receipt = ""
    receipt_verdict = _verify_worker_receipt_pair(receipt, evidence_receipt)
    merge: Optional[Dict[str, Any]] = None
    if receipt_verdict["status"] == ReceiptStatus.VERIFIED and _auto_merge_enabled():
        merge = _dispatch_merge_pr(item, receipt=receipt, run_id=common["run_id"])
    return {
        **common,
        "status": "succeeded",
        "phase": "delivered",
        "execution_state": "applied",
        "receipt": receipt,
        "operator_receipt": receipt,
        "evidence_receipt": evidence_receipt,
        "watcher_receipt": "",
        "receipt_status": receipt_verdict["status"],
        "receipt_verdict_reason": receipt_verdict["reason"],
        "attempt": 1,
        "failure_fingerprint": "",
        "merge": merge,
        "remote_task": {"task_id": task_id, "lease": lease_info},
        "started_at": started,
        "finished_at": _now(),
    }


def _operator_dispatch_attempt(item: Mapping[str, Any]) -> Dict[str, Any]:
    """Call the production operator and reduce its status to a durable worker record."""
    started = _now()
    context = dict(item.get("worktree_context") or item.get("operator_context") or {})
    common = {
        "schema": "simplicio.operator-worker/v1",
        "worker_id": item["worker_id"],
        "repo": item["repo"],
        "source_repo": str(item.get("source_repo") or item["repo"]),
        "run_id": item["run_id"],
        "task_index": item["task_index"],
        "task_id": str(item.get("task_id") or ""),
        "worktree_context": context,
        # These are deliberately worker-scoped aliases.  A fan-out consumer must never
        # infer a shared receipt from the coordinator's run-level state; every lane has
        # its own operator and evidence proof (or an explicit UNVERIFIED state).
        "operator_receipt": "",
        "evidence_receipt": "",
        "receipt_status": "UNVERIFIED",
        "agent": dict(item.get("agent_identity") or {}),
        "context_pack": dict(item.get("context_pack") or {}),
    }
    if _model_routed_dispatch_enabled():
        # #287: route this dispatch attempt through the real model registry/router
        # instead of a hardcoded runtime, and -- when a real driver is wired for the
        # selection -- genuinely invoke it. Additive audit evidence only: a routing
        # block or driver failure here never blocks the dev-cli operator mutation
        # below, which remains this repo's actual apply/verify contract.
        try:
            run_dir = Path(read_status(item["repo"], item["run_id"])["run_dir"])
            common["model_routing"] = _execute_routed_runtime(item, run_dir)
        except Exception as exc:  # routing/execution evidence must never crash dispatch
            common["model_routing"] = {"routed": False, "executed": False, "error": f"{type(exc).__name__}: {exc}"}
    queue = item.get("distributed_queue")
    if queue is not None and isinstance(queue, HTTPRemoteQueue) and _remote_worker_dispatch_enabled():
        # #286: a genuine network queue means genuine remote workers -- the coordinator
        # enqueues and waits, it never claims/executes the operator itself. See
        # `_remote_worker_dispatch_enabled` for the opt-out and rationale.
        return _operator_dispatch_attempt_remote_worker(item, common, queue, started)
    lease = None
    guarded = _guarded_dispatch_enabled()
    attempt_coordinator: Optional[AttemptCoordinator] = None
    attempt_obj: Any = None
    if queue is not None:
        identity = item.get("agent_identity")
        try:
            if guarded and identity:
                # #288/#183: real dispatch attempts get a heartbeat-guarded, fenced attempt
                # object instead of a bare lease -- the same lease/fencing contract, plus the
                # ability to run the mutating subprocess through ``run_guarded`` below.
                attempt_coordinator = AttemptCoordinator(queue, run_id=common["run_id"])
                context_pack = item.get("context_pack") if isinstance(item.get("context_pack"), Mapping) else {}
                attempt_obj = attempt_coordinator.claim(
                    work_item_id=common["task_id"],
                    identity=identity,
                    goal=str(context_pack.get("goal") or common["task_id"]),
                    acs=tuple(context_pack.get("acs") or ()),
                    depends_on=tuple(context_pack.get("depends_on") or ()),
                    source_refs=tuple(context_pack.get("source_refs") or ()),
                    allowed_paths=tuple(context_pack.get("allowed_paths") or ()),
                    issue_ref=str(context_pack.get("issue_ref") or ""),
                    issue_url=str(context_pack.get("issue_url") or ""),
                    ttl=float(os.environ.get("SIMPLICIO_REMOTE_QUEUE_TTL", "3600")),
                )
                lease = attempt_obj.lease
            else:
                lease = queue.claim(
                    common["task_id"], common["worker_id"],
                    idempotency_key=f"{common['run_id']}:{common['task_id']}:{common['worker_id']}",
                    ttl=float(os.environ.get("SIMPLICIO_REMOTE_QUEUE_TTL", "3600")),
                    identity=identity,
                    capabilities=(identity or {}).get("capabilities", ()),
                )
            common["lease"] = {
                "lease_id": lease.lease_id,
                "fencing_token": lease.fencing_token,
                "expires_at": lease.expires_at,
            }
            common["guarded_dispatch"] = attempt_obj is not None
        except QueueConflict as exc:
            return {**common, "status": "failed", "phase": "blocked", "execution_state": "paused",
                    "reason_code": "claim_conflict", "error": str(exc), "dead_letter": True,
                    "started_at": started, "finished_at": _now()}
        except (QueueUnavailable, OSError, ValueError) as exc:
            return {**common, "status": "failed", "phase": "blocked", "execution_state": "paused",
                    "reason_code": "network_paused", "error": str(exc), "dead_letter": True,
                    "started_at": started, "finished_at": _now()}
    if lease is not None:
        # Only stall a task that is genuinely claimed/leased -- this is what lets the
        # cross-process recovery test kill the orchestrator mid-attempt with a real,
        # in-flight (not merely queued) lease abandoned behind it.
        _test_only_stall_before_dispatch(str(common.get("task_id") or ""))
    if item.get("worktree_error"):
        return {
            **common,
            "status": "failed",
            "phase": "blocked",
            "execution_state": "error",
            "reason_code": "worktree_context_unpersisted",
            "receipt": "",
            "attempt": 0,
            "error": str(item["worktree_error"]),
            "failure_fingerprint": hashlib.sha256(
                str(item["worktree_error"]).encode("utf-8", "replace")
            ).hexdigest()[:16],
            "started_at": started,
            "finished_at": _now(),
        }
    try:
        payload = execute_operator(
            item["repo"], item["run_id"], task_index=item["task_index"],
            attempt_coordinator=attempt_coordinator, guarded_attempt=attempt_obj,
        )
        state = payload.get("state") or {}
        operator = state.get("operator") or {}
        execution_state = str(operator.get("execution_state") or "")
        success = execution_state == "applied"
        receipt = str(operator.get("receipt") or "")
        evidence = state.get("evidence") or {}
        evidence_receipt = str(evidence.get("receipt") or "")
        run_dir = str(payload.get("run_dir") or "")
        watcher_receipt = str(Path(run_dir) / "loop" / "watcher_state.json") if run_dir else ""
        failure_fingerprint = ""
        if receipt:
            try:
                failure_fingerprint = str(_load_json(Path(receipt)).get("failure_fingerprint") or "")
            except (OSError, ValueError, TypeError):
                # The worker result remains useful even when a crashed operator did not leave
                # a readable receipt; the scheduler will use the bounded exception path.
                failure_fingerprint = ""
        if lease is not None and success:
            receipt_ref_value = receipt or f"{run_dir}/operator-receipt.json"
            if attempt_coordinator is not None and attempt_obj is not None:
                attempt_coordinator.complete(attempt_obj, receipt_ref=receipt_ref_value)
            else:
                # #286 step 9: present a wire receipt the queue server itself independently
                # verifies (schema/hash/task-agent-fence binding), not just an opaque
                # ``receipt_ref`` path it has no way to open or trust.
                queue.complete(lease, receipt_ref=receipt_ref_value, receipt=build_completion_receipt(
                    task_id=lease.task_id, agent_id=lease.agent_id, fencing_token=lease.fencing_token,
                    receipt_ref=receipt_ref_value,
                ))
        if item.get("agent_identity") and receipt:
            # Keep the worker result itself immutable and independently attributable.
            common["receipt_binding"] = bind_receipt(
                {"receipt_ref": receipt}, item["agent_identity"],
                context_pack=item.get("context_pack"),
            )
        receipt_verdict = _verify_worker_receipt_pair(receipt, evidence_receipt)
        merge: Optional[Dict[str, Any]] = None
        if success and receipt_verdict["status"] == ReceiptStatus.VERIFIED and _auto_merge_enabled():
            # #288: once the receipt pair is genuinely VERIFIED, create/poll/merge the real
            # PR and reconcile against the remote before this dispatch attempt is reported
            # as done -- replaces the ad-hoc, hand-run "gh pr create / gh pr merge" pattern
            # this project's own delivery process previously left as prose only.
            merge = _dispatch_merge_pr(item, receipt=receipt, run_id=common["run_id"])
        verified_delivery: Optional[Dict[str, Any]] = None
        if success and _verified_delivery_gate_enabled():
            # #288: route the completion decision through the real LoopRuntimeAdapter ->
            # VerifiedAgentDelivery -> ExecutionBoard chain instead of trusting
            # execution_state == "applied" alone -- see `_verified_delivery_gate_enabled`.
            identity = dict(item.get("agent_identity") or {})
            verified_delivery = _run_verified_delivery_gate(
                run_id=common["run_id"], task_id=common["task_id"],
                actor=str(identity.get("actor") or identity.get("agent_id") or "loop"),
                attempt_id="%s-attempt-%d" % (common["worker_id"], int(state.get("attempts") or 0) or 1),
                receipt_verdict=receipt_verdict, evidence_receipt=evidence_receipt,
                watcher_receipt=watcher_receipt, merge=merge, worktree_context=context,
            )
            if not verified_delivery.get("verified"):
                success = False
        return {
            **common,
            "status": "succeeded" if success else "failed",
            "phase": str(state.get("phase") or "blocked"),
            "execution_state": execution_state or "unknown",
            "receipt": receipt,
            "operator_receipt": receipt,
            "evidence_receipt": evidence_receipt,
            "watcher_receipt": watcher_receipt if Path(watcher_receipt).exists() else "",
            "receipt_status": receipt_verdict["status"],
            "receipt_verdict_reason": receipt_verdict["reason"],
            "attempt": int(state.get("attempts") or 0),
            "failure_fingerprint": failure_fingerprint,
            "merge": merge,
            "verified_delivery": verified_delivery,
            "started_at": started,
            "finished_at": _now(),
        }
    except LeaseLostDuringExecution as exc:
        # #183/#288: the guarded subprocess was killed the instant the lease was no longer
        # current -- report this distinctly from a generic operator exception so a scheduler
        # can tell "lost the fence mid-mutation" apart from an ordinary tool crash.
        return {
            **common,
            "status": "failed",
            "phase": "blocked",
            "execution_state": "error",
            "receipt": "",
            "operator_receipt": "",
            "evidence_receipt": "",
            "receipt_status": "UNVERIFIED",
            "attempt": 0,
            "error": str(exc),
            "reason_code": "lease_lost_during_execution",
            "dead_letter": True,
            "failure_fingerprint": hashlib.sha256(str(exc).encode("utf-8", "replace")).hexdigest()[:16],
            "started_at": started,
            "finished_at": _now(),
        }
    except Exception as exc:  # worker failures are receipts, not scheduler crashes
        return {
            **common,
            "status": "failed",
            "phase": "blocked",
            "execution_state": "error",
            "receipt": "",
            "operator_receipt": "",
            "evidence_receipt": "",
            "receipt_status": "UNVERIFIED",
            "attempt": 0,
            "error": f"{type(exc).__name__}: {exc}",
            "reason_code": "operator_exception",
            "failure_fingerprint": hashlib.sha256(
                f"{type(exc).__name__}: {exc}".encode("utf-8", "replace")
            ).hexdigest()[:16],
            "started_at": started,
            "finished_at": _now(),
        }


def dispatch_operator_batch(
    items: Iterable[Mapping[str, Any]],
    *,
    max_workers: Optional[int] = None,
    retry_budget: int = 3,
    journal_dir: Optional[str] = None,
    worktree_queue: Any = None,
) -> Dict[str, Any]:
    """Continuously dispatch real operator workers and refill freed slots.

    ``items`` is the typed bridge between a scheduler (DAG/leases/worktrees) and the
    existing mapper → plan → ``execute_operator`` boundary.  It is intentionally agnostic
    about claiming: callers pass only ready, atomically claimed nodes.  Items with the same
    ``isolation_key`` are forced onto one lane so a shared run state cannot be corrupted;
    distinct worktree/run contexts overlap in the pool.  A JSONL journal records each attempt
    before the next slot is refilled, so a process restart can safely resubmit only work that
    has no successful receipt.
    """
    normalized = [_operator_dispatch_item(item) for item in items]
    keys = {(item["repo"], item["run_id"], item["task_index"]) for item in normalized}
    if len(keys) != len(normalized):
        raise ValueError("operator dispatch contains duplicate repo/run/task items")

    # Issue #288 cross-process recovery: load the journal *before* preflight so a resumed
    # batch can tell "already durably succeeded" items apart from ones still needing a fresh
    # dry-run preflight. A succeeded item's operator-receipt.json has already been
    # overwritten by `execute_operator` with a *post-execution* receipt (no `run_id` field,
    # a different shape than the pre-execution dry-run receipt `_validate_run_receipts`
    # expects) -- re-validating it as if it were still a pending dry-run would always fail
    # and permanently block every resumed batch that contains even one completed item.
    journal_path: Optional[Path] = None
    if journal_dir:
        journal_path = Path(journal_dir).resolve() / "operator-batch.jsonl"
        journal_path.parent.mkdir(parents=True, exist_ok=True)
    prior: Dict[Tuple[str, str, int], Dict[str, Any]] = {}
    if journal_path and journal_path.exists():
        for line in journal_path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                key = (str(rec.get("repo")), str(rec.get("run_id")), int(rec.get("task_index")))
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
            prior[key] = rec

    # A persisted run is a privileged execution boundary.  Keep synthetic scheduler
    # contexts supported, but fail every run-backed dispatch globally before worktree
    # preparation, journal creation, or worker submission unless its receipt chain is
    # fresh and bound to this exact run -- except an item already durably journaled as
    # succeeded, which is never re-dispatched and so must never be re-validated as if it
    # still needed a fresh dry run.
    for item in normalized:
        key = (item["repo"], item["run_id"], item["task_index"])
        if prior.get(key, {}).get("status") == "succeeded":
            continue
        repo_path = Path(item["repo"]).resolve()
        run_dir = repo_path / ".simplicio" / "loop-runs" / item["run_id"]
        if not run_dir.is_dir():
            continue
        try:
            contract = _require_json_receipt(run_dir / "task-contract.json", "task contract")
            _validate_run_receipts(
                repo_path,
                run_dir,
                contract,
                state=_load_json(run_dir / "state.json") if (run_dir / "state.json").is_file() else None,
                manifest=_load_json(run_dir / "manifest.json") if (run_dir / "manifest.json").is_file() else None,
                require_dry_run=True,
            )
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            state_path = run_dir / "state.json"
            if state_path.is_file():
                _persist_batch_preflight_block(
                    run_dir,
                    _load_json(state_path),
                    repo_path,
                    str(exc),
                    task_indices=[item["task_index"]],
                )
            raise
    _prepare_worktree_contexts(normalized, worktree_queue)
    requested_workers = max_workers
    effective_workers = _operator_worker_limit(max_workers, len(normalized))
    isolation_keys = {item["isolation_key"] for item in normalized}
    serial_fallback_reason = ""
    if effective_workers > 1 and len(isolation_keys) < len(normalized):
        effective_workers = 1
        serial_fallback_reason = "shared_run_state"
    retry_budget = max(0, int(retry_budget))

    pending = deque(
        item for item in normalized
        if prior.get((item["repo"], item["run_id"], item["task_index"]), {}).get("status") != "succeeded"
    )
    skipped = len(normalized) - len(pending)
    started = _now()
    records: Dict[Tuple[str, str, int], Dict[str, Any]] = dict(prior)
    completed: List[Dict[str, Any]] = []
    refill_count = 0

    def _persist_attempt(record: Dict[str, Any]) -> None:
        if journal_path:
            _append_jsonl(journal_path, record)
        records[(record["repo"], record["run_id"], record["task_index"])] = record

    def _run_item(item: Dict[str, Any]) -> List[Dict[str, Any]]:
        attempts: List[Dict[str, Any]] = []
        previous_fingerprint = ""
        _ensure_deferred_worktree_context(item, worktree_queue)
        try:
            for attempt_no in range(1, retry_budget + 2):
                record = _operator_dispatch_attempt(item)
                record["dispatch_attempt"] = attempt_no
                if previous_fingerprint and record.get("failure_fingerprint") == previous_fingerprint:
                    record["retry_strategy"] = "same_fingerprint_bounded"
                elif attempt_no > 1:
                    record["retry_strategy"] = "alternate_strategy"
                else:
                    record["retry_strategy"] = "initial"
                attempts.append(record)
                if record["status"] == "succeeded":
                    break
                previous_fingerprint = str(record.get("failure_fingerprint") or "")
        finally:
            _release_shared_context(item, worktree_queue)
        attempts[-1]["dead_letter"] = attempts[-1]["status"] != "succeeded"
        # Keep compact per-lane history on the final record while the JSONL journal
        # retains the complete receipts.  This proves a retry belongs to one worker and
        # did not restart sibling lanes.
        attempts[-1]["attempt_count"] = len(attempts)
        attempts[-1]["retry_scope"] = "worker"
        attempts[-1]["attempt_history"] = [
            {
                "dispatch_attempt": int(record.get("dispatch_attempt") or index),
                "status": record.get("status", "UNVERIFIED"),
                "failure_fingerprint": record.get("failure_fingerprint", ""),
            }
            for index, record in enumerate(attempts, start=1)
        ]
        return attempts

    if pending and effective_workers:
        with ThreadPoolExecutor(max_workers=effective_workers, thread_name_prefix="simplicio-operator") as pool:
            active = {}
            while pending and len(active) < effective_workers:
                item = pending.popleft()
                active[pool.submit(_run_item, item)] = item
            while active:
                done, _ = wait(tuple(active), return_when=FIRST_COMPLETED)
                for future in done:
                    item = active.pop(future)
                    try:
                        attempts = future.result()
                    except Exception as exc:  # defensive: _run_item already receipts exceptions
                        attempts = [{
                            "schema": "simplicio.operator-worker/v1",
                            "worker_id": item["worker_id"], "repo": item["repo"],
                            "source_repo": item.get("source_repo", item["repo"]),
                            "run_id": item["run_id"], "task_index": item["task_index"],
                            "task_id": item.get("task_id", ""),
                            "worktree_context": item.get("worktree_context", {}),
                            "status": "failed", "phase": "blocked", "execution_state": "error",
                            "error": f"{type(exc).__name__}: {exc}", "dead_letter": True,
                            "started_at": _now(), "finished_at": _now(),
                        }]
                    for record in attempts:
                        _persist_attempt(record)
                    final = attempts[-1]
                    completed.append(final)
                    # Refill as soon as this worker exits; there is no frozen wave barrier.
                    if pending:
                        next_item = pending.popleft()
                        active[pool.submit(_run_item, next_item)] = next_item
                        refill_count += 1

    final_records = []
    for item in normalized:
        key = (item["repo"], item["run_id"], item["task_index"])
        final_records.append(records.get(key, {
            "schema": "simplicio.operator-worker/v1", "worker_id": item["worker_id"],
            "repo": item["repo"], "run_id": item["run_id"], "task_index": item["task_index"],
            "task_id": item.get("task_id", ""),
            "source_repo": item.get("source_repo", item["repo"]),
            "worktree_context": item.get("worktree_context", {}),
            "status": "pending", "phase": "queued", "execution_state": "pending",
        }))
    result = {
        "schema": BATCH_SCHEMA,
        "run_id": normalized[0]["run_id"] if normalized and len({i["run_id"] for i in normalized}) == 1 else "",
        "requested_tasks": [item["task_index"] for item in normalized],
        "skipped_completed": skipped,
        "max_workers_requested": requested_workers,
        "max_workers": effective_workers,
        "active_workers": 0,
        "worker_count": len(final_records),
        "queue_depth": 0,
        "refill_count": refill_count,
        "serial_fallback_reason": serial_fallback_reason,
        "leases": [],
        "blockers": [
            {
                "task_index": record["task_index"],
                "reason_code": "operator_failed",
                "error": record.get("error", ""),
                "failure_fingerprint": record.get("failure_fingerprint", ""),
            }
            for record in final_records
            if record.get("status") == "failed"
        ],
        "attempts": {
            str(record["task_index"]): int(record.get("dispatch_attempt") or 0)
            for record in final_records
        },
        "started_at": started,
        "finished_at": _now(),
        "workers": final_records,
        "completed_task_indices": sorted(r["task_index"] for r in final_records if r.get("status") == "succeeded"),
        "failed_task_indices": sorted(r["task_index"] for r in final_records if r.get("status") == "failed"),
        "dead_letter_task_indices": sorted(r["task_index"] for r in final_records if r.get("dead_letter")),
        "receipt_contract": {
            "scope": "worker",
            "required": ["operator_receipt", "evidence_receipt"],
            "ready": all(
                r.get("receipt_status") == "VERIFIED"
                for r in final_records
            ),
            "missing_task_indices": sorted(
                r["task_index"] for r in final_records
                if r.get("receipt_status") != "VERIFIED"
            ),
        },
        "retry_contract": {
            "scope": "worker",
            "independent": True,
            "attempts_by_task": {
                str(r["task_index"]): int(r.get("attempt_count") or 0)
                for r in final_records
            },
        },
        "journal": str(journal_path) if journal_path else "",
    }
    if journal_path:
        _write_json(journal_path.with_suffix(".json"), result)
    return result


def execute_operator_batch(
    repo: str,
    run_id: str,
    task_indices: Optional[Sequence[int]] = None,
    *,
    max_workers: Optional[int] = None,
    retry_budget: int = 3,
    isolated_contexts: Optional[Mapping[int, Mapping[str, Any]]] = None,
    worktree_queue: Any = None,
    auto_fan_out: Optional[bool] = None,
) -> Dict[str, Any]:
    """Dispatch all (or selected) tasks from one run through the real operator bridge.

    Independent tasks fan out into owned worktrees by default.  Set ``auto_fan_out=False`` or
    ``SIMPLICIO_LOOP_AUTO_FAN_OUT=0`` to opt out.  If impact metadata, Git, or the worktree
    adapter is unavailable, the shared-run serial guard remains the safe fallback.
    """
    status = read_status(repo, run_id)
    if (status["state"].get("maintenance") or {}).get("disposition") == "backlog_only":
        raise RuntimeError("maintenance deferred: operator batch is blocked until explicit resume")
    run_dir = Path(status["run_dir"])
    try:
        contract = _require_json_receipt(run_dir / "task-contract.json", "task contract")
        receipts = _validate_run_receipts(
            Path(status["manifest"].get("repo") or repo).resolve(),
            run_dir,
            contract,
            state=status["state"],
            manifest=status["manifest"],
            require_dry_run=True,
        )
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        _persist_batch_preflight_block(
            run_dir,
            status["state"],
            Path(status["manifest"].get("repo") or repo).resolve(),
            str(exc),
            task_indices=task_indices or (),
        )
        raise
    plan = receipts["plan"]
    # #284: mutation-authority gate, mandatory by default -- same as execute_operator()
    # (single-task tick), extended to the batch boundary. "execute_operator() e batch
    # recusam execução sem mutation authority válida" now applies unconditionally
    # (opt out only via an explicit falsy SIMPLICIO_REQUIRE_MUTATION_AUTHORITY; see
    # planning_gate.mutation_authority_required()).
    if mutation_authority_required():
        batch_attempt = int((status["state"] or {}).get("attempts", 0)) + 1
        current_source_hash = ""
        current_snapshot_path = run_dir / "source-snapshot-current.json"
        if current_snapshot_path.exists():
            try:
                current_source_hash = str((_load_json(current_snapshot_path).get("source") or {}).get("snapshot_hash") or "")
            except Exception:
                current_source_hash = ""
        authority_verdict = evaluate_mutation_authority(
            run_dir, run_id=run_id, attempt=batch_attempt,
            task_contract_hash=str(contract.get("collection_hash") or _planning_content_hash(contract)),
            plan_hash=_planning_content_hash(plan),
            source_snapshot_hash=current_source_hash,
        )
        if not authority_verdict["ok"]:
            _persist_batch_preflight_block(
                run_dir,
                status["state"],
                Path(status["manifest"].get("repo") or repo).resolve(),
                f"mutation authority required (SIMPLICIO_REQUIRE_MUTATION_AUTHORITY) but "
                f"{authority_verdict['reason_code']}: {authority_verdict['reason']}",
                task_indices=task_indices or (),
            )
            raise RuntimeError(
                "mutation authority required (SIMPLICIO_REQUIRE_MUTATION_AUTHORITY) but "
                f"{authority_verdict['reason_code']}: {authority_verdict['reason']}"
            )
    task_count = len(contract.get("tasks") or [])
    if task_indices is None:
        indices = list(range(1, task_count + 1))
    else:
        indices = [int(index) for index in task_indices]
    if any(index < 1 or index > task_count for index in indices):
        raise ValueError("task index out of range")
    contexts = dict(isolated_contexts or {})
    distributed_queue = None
    agent_identity = None
    if not isolated_contexts:
        distributed_queue, agent_identity = _distributed_configuration(repo)
    auto_reason = "explicit_contexts" if isolated_contexts else ""
    if not isolated_contexts and worktree_queue is None and (auto_fan_out is not False):
        previous = os.environ.get("SIMPLICIO_LOOP_AUTO_FAN_OUT")
        if auto_fan_out is True:
            os.environ["SIMPLICIO_LOOP_AUTO_FAN_OUT"] = "1"
        try:
            worktree_queue, auto_contexts, auto_reason = _auto_worktree_dispatch(
                repo, run_id, contract, plan, indices,
            )
        finally:
            if auto_fan_out is True:
                if previous is None:
                    os.environ.pop("SIMPLICIO_LOOP_AUTO_FAN_OUT", None)
                else:
                    os.environ["SIMPLICIO_LOOP_AUTO_FAN_OUT"] = previous
        contexts.update(auto_contexts)
    items = []
    for index in indices:
        context = dict(contexts.get(index) or {})
        item = {
            "repo": context.get("repo", repo),
            "run_id": context.get("run_id", run_id),
            "task_index": index,
            "worker_id": context.get("worker_id", f"operator-{index}"),
            "isolation_key": context.get("isolation_key"),
            "task_id": context.get("task_id", f"{run_id}-task-{index}"),
            "task_spec": context.get("task_spec") or {
                "id": context.get("task_id", f"{run_id}-task-{index}"),
                "goal": _task_goal((contract.get("tasks") or [])[index - 1]),
            },
            "isolation": context.get("isolation", "worktree"),
        }
        if distributed_queue is not None:
            item["distributed_queue"] = distributed_queue
            item["agent_identity"] = agent_identity
            task = (contract.get("tasks") or [])[index - 1]
            target_paths = (plan.get("steps") or [])[index - 1].get("candidate_targets") or []
            issue_ref = task.get("issue_ref") or contract.get("issue_ref") or ""
            issue_url = task.get("issue_url") or contract.get("issue_url") or ""
            item["context_pack"] = build_context_pack(
                task_id=item["task_id"], goal=_task_goal(task), identity=agent_identity,
                acs=[*[(s.get("title") or s.get("id") or "") for s in (task.get("scenarios") or [])]],
                depends_on=list((task.get("dependencies") or {}).get("items") or []),
                allowed_paths=target_paths, source_refs=target_paths,
                issue_ref=issue_ref, issue_url=issue_url,
            )
        items.append(item)
    result = dispatch_operator_batch(
        items,
        max_workers=max_workers,
        retry_budget=retry_budget,
        journal_dir=str(Path(status["run_dir"])),
        worktree_queue=worktree_queue,
    )
    technical_debts: List[Dict[str, Any]] = []
    # Fan-out is an optimization. A safe serial lane is still useful work, so
    # capability loss is recorded as advisory debt instead of a global blocker.
    if auto_reason and auto_reason not in {"explicit_contexts", "single_task"} and len(items) > 1:
        technical_debts.append(_record_technical_debt(
            status["run_dir"],
            run_id=run_id,
            reason_code=auto_reason if auto_reason in {
                "fanout_disabled", "not_git_checkout", "missing_plan_targets",
                "overlapping_task_impacts", "worktree_adapter_unavailable",
                "worktree_preflight_failed",
            } else "fanout_serial_fallback",
            stage="dispatch",
            source="simplicio_loop.runner._auto_worktree_dispatch",
            message="automatic fan-out was not available; continuing with the safe serial lane",
            next_action="install/configure the worktree adapter or split overlapping targets",
        ))
    result["distributed"] = {
        "enabled": distributed_queue is not None,
        "queue": os.environ.get("SIMPLICIO_REMOTE_QUEUE_URL", "") if distributed_queue is not None else "",
        "agent": agent_identity or {},
        "fail_closed": distributed_queue is not None,
    }
    if not contexts and len(items) > 1:
        # dispatch_operator_batch derives this from the shared isolation key; retain a clear
        # contract-level marker for callers inspecting the convenience API.
        result["serial_fallback_reason"] = result.get("serial_fallback_reason") or "shared_run_state"
        if not technical_debts:
            technical_debts.append(_record_technical_debt(
                status["run_dir"],
                run_id=run_id,
                reason_code="fanout_serial_fallback",
                stage="dispatch",
                source="simplicio_loop.runner.dispatch_operator_batch",
                message="tasks share run state or could not be isolated; serial execution preserved safety",
                next_action="provide distinct worktree contexts for independent tasks",
            ))
    result["technical_debts"] = technical_debts
    result["fan_out"] = {
        "enabled": bool(worktree_queue is not None and len(contexts) > 1),
        "default": auto_fan_out is not False,
        "reason": auto_reason or ("isolated_contexts" if contexts else "serial_fallback"),
        "contexts": len(contexts),
    }
    return result


def defer_maintenance_backlog_only(
    repo: str,
    run_id: str,
    *,
    correction_summary: str,
    deferral_reason: str,
    resume_instructions: Sequence[str] | str,
    evidence_status: str = "UNVERIFIED",
) -> Dict[str, Any]:
    status = read_status(repo, run_id)
    if status["state"].get("phase") in {"done", "cancelled"}:
        raise ValueError(f"run already terminal: {status['state'].get('phase')}")
    run_dir = Path(status["run_dir"])
    state = status["state"]
    receipt = _write_maintenance_deferred_receipt(
        run_dir,
        correction_summary=correction_summary,
        deferral_reason=deferral_reason,
        resume_instructions=resume_instructions,
        evidence_status=evidence_status,
    )
    state["maintenance"] = {
        "mode": receipt["mode"],
        "disposition": receipt["disposition"],
        "receipt": str(run_dir / "maintenance-receipt.json"),
        "correction_summary": receipt["correction_summary"],
        "deferral_reason": receipt["deferral_reason"],
        "evidence_status": receipt["evidence_status"],
    }
    completion = _completion_state(run_dir, state.get("completion"))
    completion["ready"] = False
    completion["tag"] = "UNVERIFIED"
    completion["verdict"] = "DELIVERY_PENDING"
    completion["reason_code"] = "maintenance_deferred"
    if (run_dir / "completion-receipt.json").exists():
        persisted = _load_json(run_dir / "completion-receipt.json")
        persisted.update({"ready": False, "verdict": completion["verdict"],
                          "reason_code": completion["reason_code"], "tag": "UNVERIFIED"})
        _write_json(run_dir / "completion-receipt.json", persisted)
    state["completion"] = completion
    state["operator"] = {
        **(state.get("operator") or {}),
        "ready": False,
        "execution_state": "backlog_only",
    }
    state["current_action"] = "maintenance_deferred_to_backlog"
    state["next_action"] = "resume_from_maintenance_receipt"
    state["evidence"] = {
        **(state.get("evidence") or {}),
        "ready": False,
        "status": receipt["evidence_status"],
    }
    _write_json(run_dir / "state.json", state)
    _transition(
        run_dir,
        state,
        "partial",
        "maintenance correction deferred to backlog-only mode",
        receipt=str(run_dir / "maintenance-receipt.json"),
        extra={"mode": receipt["mode"], "disposition": receipt["disposition"]},
    )
    return read_status(repo, run_id)


def read_status(repo: str, run_id: str = "") -> Dict[str, Any]:
    repo_path = Path(repo).resolve()
    runs_root = repo_path / ".simplicio" / "loop-runs"
    if not runs_root.exists():
        return {
            "run_dir": None,
            "manifest": None,
            "state": {
                "phase": "no_runs",
                "completion": {"ready": False, "verdict": "NO_RUNS", "tag": "UNVERIFIED"},
                "operator": {"ready": False, "execution_state": "idle"},
                "evidence": {"ready": False, "status": "NO_RUNS"},
                "current_action": "none",
                "next_action": "none",
                "message": "no runs directory found; run simplicio-loop to start",
            },
            "execution_route": None,
            "route_receipt_status": "UNVERIFIED",
        }
    chosen = None
    if run_id:
        chosen = runs_root / run_id
    else:
        candidates = sorted([p for p in runs_root.iterdir() if p.is_dir()], key=lambda p: p.name)
        if not candidates:
            return {
                "run_dir": None,
                "manifest": None,
                "state": {
                    "phase": "no_runs",
                    "completion": {"ready": False, "verdict": "NO_RUNS", "tag": "UNVERIFIED"},
                    "operator": {"ready": False, "execution_state": "idle"},
                    "evidence": {"ready": False, "status": "NO_RUNS"},
                    "current_action": "none",
                    "next_action": "none",
                    "message": "no runs found; run simplicio-loop to start",
                },
                "execution_route": None,
                "route_receipt_status": "UNVERIFIED",
            }
        chosen = candidates[-1]
    manifest = _load_json(chosen / "manifest.json")
    state = _load_json(chosen / "state.json")
    state["completion"] = _completion_state(chosen, state.get("completion"))
    execution_route = None
    route_path = chosen / "execution-route.json"
    if route_path.is_file():
        try:
            candidate = _load_json(route_path)
            if verify_route_hash(candidate):
                execution_route = candidate
                state.setdefault("operator", {})["execution_route"] = candidate
                state["execution_route"] = candidate
        except (OSError, ValueError, TypeError):
            execution_route = None
    return {
        "run_dir": str(chosen),
        "manifest": manifest,
        "state": state,
        "execution_route": execution_route,
        "route_receipt_status": "MEASURED" if execution_route else "UNVERIFIED",
    }


def change_phase(repo: str, run_id: str, to_phase: str, reason: str) -> Dict[str, Any]:
    status = read_status(repo, run_id)
    run_dir = Path(status["run_dir"])
    state = status["state"]
    if state.get("phase") in {"done", "cancelled"}:
        raise ValueError(f"run already terminal: {state.get('phase')}")
    if to_phase == "awaiting_decision":
        maintenance = state.get("maintenance") or {}
        if maintenance.get("mode") == "maintenance_deferred" or maintenance.get("disposition") == "backlog_only":
            state["maintenance"] = _active_maintenance_state(maintenance)
            state["operator"] = {
                **(state.get("operator") or {}),
                "ready": False,
                "execution_state": "invalidated",
            }
            state["evidence"] = {
                **(state.get("evidence") or {}),
                "ready": False,
                "status": "INVALIDATED",
            }
        state["next_action"] = "mapper_scan_required"
    elif to_phase == "cancelled":
        state["next_action"] = "none"
    _transition(run_dir, state, to_phase, reason, receipt=str(run_dir / "state.json"))
    return read_status(repo, run_id)


def reconcile_delivery(repo: str, run_id: str, current_state: str, source_kind: str = "local",
                       source_payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    status = read_status(repo, run_id)
    run_dir = Path(status["run_dir"])
    manifest = status["manifest"]
    state = status["state"]
    previous_receipt = None
    previous_path = run_dir / "delivery-receipt.json"
    if previous_path.exists():
        try:
            previous_receipt = _load_json(previous_path)
        except (OSError, ValueError, TypeError):
            previous_receipt = None
    execution_route = None
    route_path = run_dir / "execution-route.json"
    if route_path.is_file():
        try:
            candidate = _load_json(route_path)
            if verify_route_hash(candidate):
                execution_route = candidate
        except (OSError, ValueError, TypeError):
            execution_route = None
    delivery_payload = dict(source_payload or {})
    if execution_route:
        delivery_payload.setdefault("execution_route", execution_route)
    receipt = build_delivery_receipt(str(run_dir), manifest.get("delivery_target") or "verified",
                                     current_state=current_state, source_kind=source_kind,
                                     source_payload=delivery_payload)
    if execution_route:
        receipt["execution_route"] = execution_route
        receipt["route_receipt_sha"] = execution_route.get("receipt_sha", "")
    receipt["reconciliation"] = reconcile_delivery_observation(previous_receipt, receipt)
    write_delivery_receipt(str(run_dir), receipt)
    state["delivery"] = {
        "target": receipt["target"],
        "current_state": receipt["current_state"],
        "ready": receipt["ready"],
        "receipt": str(run_dir / "delivery-receipt.json"),
        "source_checked_at": receipt["source_checked_at"],
        "source_kind": source_kind,
        "execution_route": execution_route,
        "route_receipt_sha": receipt.get("route_receipt_sha", ""),
    }
    reconciliation = receipt.get("reconciliation") or {}
    if reconciliation.get("status") == "reopened":
        state["current_action"] = "delivery_reopened"
        state["next_action"] = "requery_source"
        next_phase = "partial"
        state.setdefault("blockers", [])
        failed_gate = next((gate for gate in receipt.get("gates", [])
                            if gate.get("status") == "fail"), {})
        state["blockers"] = [
            "delivery reopened: " + str(failed_gate.get("detail") or
                                         reconciliation.get("reason_code") or
                                         "delivery_target_regressed")
        ]
    elif receipt["ready"]:
        state["current_action"] = "delivery_reconciled"
        state["next_action"] = "completion_oracle"
        next_phase = "delivering" if current_state not in {"verified", "done"} else "validating"
    else:
        state["current_action"] = "delivery_reconciliation_failed"
        state["next_action"] = "collect_missing_delivery_evidence"
        next_phase = "partial"
        state.setdefault("blockers", [])
        fail_gate = next((gate for gate in receipt.get("gates", []) if gate.get("status") == "fail"), None)
        if fail_gate:
            state["blockers"] = [fail_gate.get("detail", "delivery reconciliation failed")]
    _write_json(run_dir / "state.json", state)
    _emit_event(run_dir, state, "delivery_reconciled", receipt=str(run_dir / "delivery-receipt.json"),
                blocker="" if receipt["ready"] else "delivery_reconciliation_failed",
                message="delivery state reconciled", current_state=receipt["current_state"],
                reconciliation=reconciliation, execution_route=execution_route,
                route_receipt_sha=receipt.get("route_receipt_sha", ""))
    if reconciliation.get("status") == "reopened":
        _emit_event(run_dir, state, "rollback", receipt=str(run_dir / "delivery-receipt.json"),
                    blocker=str(reconciliation.get("reason_code") or "delivery_reopened"),
                    message="delivery regression reopened the run")
    if receipt["ready"]:
        completion = _completion_state(run_dir, state.get("completion"))
        _emit_event(run_dir, state, "oracle_verdict", receipt=(
            str(run_dir / "completion-receipt.json")
            if (run_dir / "completion-receipt.json").exists()
            else str(run_dir / "delivery-receipt.json")),
            blocker="" if completion.get("ready") else "oracle_incomplete",
            message=str(completion.get("verdict") or "DELIVERY_PENDING"),
            verdict=str(completion.get("verdict") or "DELIVERY_PENDING"))
    _transition(run_dir, state, next_phase, "delivery state reconciled", receipt=str(run_dir / "delivery-receipt.json"))
    return read_status(repo, run_id)


def apply_human_decision(repo: str, run_id: str, decision_id: str, answer: str,
                         impact: str = "behavior-change") -> Dict[str, Any]:
    status = read_status(repo, run_id)
    run_dir = Path(status["run_dir"])
    state = status["state"]
    contract_payload = _load_json(_contract_path(run_dir))
    tasks = contract_payload.get("tasks") or []
    if not tasks:
        raise ValueError("task contract collection is empty")
    changed = False
    for task in tasks:
        ledger = task.setdefault("decision_ledger", [])
        for item in ledger:
            if item.get("id") == decision_id:
                item["resolved"] = True
                item["answer"] = answer
                item["resolved_at"] = _now()
                item["resolution_impact"] = impact
                changed = True
        for bucket_name in ("questions", "assumptions", "blockers"):
            for item in task.get(bucket_name) or []:
                if item.get("id") == decision_id:
                    item["resolved"] = True
                    item["answer"] = answer
                    item["resolved_at"] = _now()
                    item["resolution_impact"] = impact
                    changed = True
    if not changed:
        raise ValueError(f"decision id not found: {decision_id}")
    contract_payload["revision"] = int(contract_payload.get("revision", 1)) + 1
    contract_payload["updated_at"] = _now()
    _write_json(_contract_path(run_dir), contract_payload)
    _emit_event(run_dir, state, "handoff", receipt=str(_contract_path(run_dir)),
                task_id=str(tasks[0].get("id") or ""), ac_ids=_task_ac_ids(tasks[0]),
                message="human decision handed off to replanning", decision_id=decision_id,
                execution_route=state.get("execution_route") or {},
                route_receipt_sha=str((state.get("execution_route") or {}).get("receipt_sha") or ""))
    invalidated = []
    for name in ("plan.json", "operator-receipt.json", "evidence-receipt.json", "delivery-receipt.json"):
        path = run_dir / name
        if path.exists():
            path.unlink()
            invalidated.append(name)
    state["phase"] = "awaiting_decision"
    state["updated_at"] = _now()
    state["current_action"] = "human_decision_applied"
    state["next_action"] = "rebuild_plan_from_updated_contract"
    state["operator"] = {"ready": False, "receipt": "", "target": "", "execution_state": "invalidated"}
    state["evidence"] = {"ready": False, "receipt": "", "status": "INVALIDATED"}
    state["delivery"] = {"target": state.get("delivery_target"), "current_state": "planned", "ready": False, "receipt": ""}
    state["completion"] = _default_completion_state()
    state["blockers"] = []
    _write_json(run_dir / "state.json", state)
    _transition(run_dir, state, "awaiting_decision", "human decision applied; dependent artifacts invalidated",
                receipt=str(_contract_path(run_dir)), extra={"decision_id": decision_id, "invalidated": invalidated})
    return read_status(repo, run_id)


def sync_source_state(repo: str, run_id: str, source: str, external_repo: str = "",
                      pr: int | None = None, tag: str = "") -> Dict[str, Any]:
    status = read_status(repo, run_id)
    manifest = status["manifest"]
    target = manifest.get("delivery_target") or "verified"
    if source != "github":
        raise ValueError(f"unsupported source: {source!r}")
    payload = github_delivery_payload(external_repo, pr=pr, tag=tag, target_state=target)
    current_state = infer_github_delivery_state(payload)
    return reconcile_delivery(repo, run_id, current_state, source_kind="github", source_payload=payload)
