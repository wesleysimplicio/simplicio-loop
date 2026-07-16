"""Portable multi-tracker stage-reporting interface (EPIC #422, issue #436).

This module generalizes stage-agent reporting into a provider-agnostic
work-item-comment interface. It is designed so that #433's GitHub-specific
reporting implementation becomes *one* implementation of the
``ReportingProvider`` protocol defined here, rather than a special case the
dispatcher has to know about.

Two non-negotiable rules (from issue #436):

1. GitHub is REQUIRED when the run's source is GitHub: a pending/unconfirmed
   GitHub comment blocks ``COMPLETE``.
2. Azure DevOps, Jira, Asana and Trello are CONDITIONAL: a comment is only
   published when the provider is detected, configured, authenticated and
   authorized. No connection means ``NOT_CONNECTED`` — never an error, and
   never an invented remote attempt.

Everything here uses only the Python standard library so it runs on any host.

Schema: ``simplicio.reporting-capability/v1``
----------------------------------------------
See :class:`ReportingCapability`. States: ``CONNECTED``, ``NOT_CONNECTED``,
``MISCONFIGURED``, ``UNAUTHORIZED``, ``UNREACHABLE``.

Protocol: ``ReportingProvider``
--------------------------------
See :class:`ReportingProvider`. Methods: ``detect``, ``find_existing``,
``publish``. Concrete providers (GitHub / Azure DevOps / Jira / Asana /
Trello) implement this ABC; the dispatcher (:class:`ReportingDispatcher`)
fans the same canonical :class:`StageEventEnvelope` out to every connected
provider, skipping disconnected optional providers without ever attempting
a remote call, while treating GitHub as hard-required when the event's
source is GitHub.
"""
from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Mapping, Optional

SCHEMA_ID = "simplicio.reporting-capability/v1"

PROVIDERS = ("github", "azure_devops", "jira", "asana", "trello")

CAPABILITY_STATES = (
    "CONNECTED",
    "NOT_CONNECTED",
    "MISCONFIGURED",
    "UNAUTHORIZED",
    "UNREACHABLE",
)

# Terminal completion states a stage-event lifecycle may reach.
TERMINAL_STATUSES = ("COMPLETE", "PARTIAL", "BLOCKED", "REGRESSED")

# Per-provider dispatch outcome vocabulary used by the completion auditor
# (issue #431 integration point): one of these per provider per run/task.
DISPATCH_STATUSES = ("confirmed", "pending", "skipped_not_connected", "blocked", "error")


class ReportingError(ValueError):
    """Raised on a structurally invalid capability, envelope, or receipt."""


# --------------------------------------------------------------------------- #
# Canonical stage-event envelope (provider-neutral)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StageEventEnvelope:
    """The single canonical event every provider projection is built from.

    This is intentionally provider-neutral: no GitHub/Jira/etc-specific
    fields. #433's GitHub renderer (when it lands) should consume the same
    shape — this dataclass is the extraction point mentioned in the issue's
    step-by-step plan ("Extrair de #433 o renderer/event projection
    provider-neutral").
    """

    run_id: str
    task_id: str
    source: str  # e.g. "github", "local", "manual" — drives GitHub-required policy
    stage: str
    agent: str
    attempt: int
    fence: str
    status: str  # one of TERMINAL_STATUSES, or an in-flight stage status
    evidence_refs: tuple = field(default_factory=tuple)
    blockers: tuple = field(default_factory=tuple)
    next_action: str = ""
    delivery_state: str = ""
    sequence: int = 0  # monotonic per run_id+task_id, used as the high-water mark

    def marker(self, provider: str) -> str:
        """The common logical marker translated per-provider by adapters."""
        return f"simplicio-loop:stage-report:v1 provider={provider} run={self.run_id} task={self.task_id}"

    def idempotency_key(self, provider: str, target: str) -> str:
        raw = f"{self.run_id}:{self.task_id}:{provider}:{target}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def render_body(self, provider: str) -> str:
        """Deterministic, provider-neutral markdown-ish body. Adapters may
        re-encode this (e.g. to ADF for Jira) but must not lose the timeline
        fields. Never includes secrets, tokens, headers or raw logs."""
        lines = [
            self.marker(provider),
            f"stage: {self.stage}",
            f"agent: {self.agent}",
            f"attempt: {self.attempt}",
            f"fence: {self.fence}",
            f"status: {self.status}",
        ]
        if self.evidence_refs:
            lines.append("evidence: " + ", ".join(self.evidence_refs))
        if self.blockers:
            lines.append("blockers: " + ", ".join(self.blockers))
        if self.next_action:
            lines.append(f"next_action: {self.next_action}")
        if self.delivery_state:
            lines.append(f"delivery: {self.delivery_state}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["evidence_refs"] = list(self.evidence_refs)
        d["blockers"] = list(self.blockers)
        return d


def validate_envelope(env: Mapping[str, Any]) -> tuple:
    """Validate a raw envelope dict. Returns (ok, errors)."""
    errors: List[str] = []
    if not isinstance(env, Mapping):
        return False, ["envelope must be an object"]
    for f in ("run_id", "task_id", "source", "stage", "agent", "fence", "status"):
        if not str(env.get(f, "")).strip():
            errors.append(f"envelope.{f} is required")
    if env.get("status") not in TERMINAL_STATUSES and not str(env.get("status", "")).strip():
        errors.append("envelope.status is required")
    return (len(errors) == 0), errors


# --------------------------------------------------------------------------- #
# Capability schema: simplicio.reporting-capability/v1
# --------------------------------------------------------------------------- #
@dataclass
class ReportingCapability:
    """``simplicio.reporting-capability/v1``.

    ``state`` is the authority — never inferred just because a CLI/binary
    exists; providers are expected to run a real identity/target/permission
    probe (see :meth:`ReportingProvider.detect`) before ever reporting
    ``CONNECTED``.
    """

    provider: str
    state: str  # one of CAPABILITY_STATES
    reason_code: str = ""  # required when state != CONNECTED
    adapter: str = ""
    adapter_version: str = ""
    capability_ids: tuple = field(default_factory=tuple)
    target_resolved: bool = False
    auth_probed: bool = False  # never carries the secret itself
    can_read: bool = False
    can_create_comment: bool = False
    can_update_comment: bool = False
    probe_timestamp: str = ""
    required: bool = False
    evidence_refs: tuple = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.provider not in PROVIDERS:
            raise ReportingError(f"unknown provider: {self.provider!r}")
        if self.state not in CAPABILITY_STATES:
            raise ReportingError(f"invalid capability state: {self.state!r}")
        if self.state != "CONNECTED" and not self.reason_code:
            raise ReportingError(f"reason_code is required when state != CONNECTED ({self.provider})")

    @property
    def connected(self) -> bool:
        return self.state == "CONNECTED"

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["schema"] = SCHEMA_ID
        d["capability_ids"] = list(self.capability_ids)
        d["evidence_refs"] = list(self.evidence_refs)
        return d


# --------------------------------------------------------------------------- #
# Receipts
# --------------------------------------------------------------------------- #
@dataclass
class CommentRef:
    """A found-or-created remote comment reference (per provider+target)."""

    provider: str
    target: str
    remote_comment_id: str
    body_hash: str
    high_water_mark: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PublishReceipt:
    """Outcome of a single :meth:`ReportingProvider.publish` call."""

    provider: str
    target: str
    run_id: str
    task_id: str
    status: str  # one of DISPATCH_STATUSES
    remote_comment_id: str = ""
    body_hash: str = ""
    reason_code: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if self.status not in DISPATCH_STATUSES:
            raise ReportingError(f"invalid publish status: {self.status!r}")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _body_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# ReportingProvider protocol
# --------------------------------------------------------------------------- #
class ReportingProvider(ABC):
    """Provider-agnostic work-item-comment adapter.

    Concrete subclasses: GitHub (required-when-source-github), and the
    conditional trackers (Azure DevOps, Jira, Asana, Trello). A subclass
    must never fabricate a ``CONNECTED`` state — :meth:`detect` has to run
    a real (or, in the stub providers shipped here, an honestly-absent)
    probe.
    """

    provider_name: str = ""

    @abstractmethod
    def detect(self) -> ReportingCapability:
        """Run a real identity/target/permission probe. Must return
        ``NOT_CONNECTED`` (with a reason_code) rather than raise or invent
        a connection when the connector is absent/unconfigured."""

    @abstractmethod
    def find_existing(self, idempotency_key: str) -> Optional[CommentRef]:
        """Return the existing living comment for this key, or None.
        MUST NOT be called (and must not attempt any remote call) unless
        the provider is CONNECTED — the dispatcher enforces this."""

    @abstractmethod
    def publish(self, envelope: StageEventEnvelope, target: str) -> PublishReceipt:
        """Create-or-update the one living comment for
        run_id+task_id+provider+target. MUST NOT be called unless CONNECTED."""


# --------------------------------------------------------------------------- #
# Fake / stub providers
# --------------------------------------------------------------------------- #
class FakeReportingProvider(ReportingProvider):
    """Deterministic in-memory fake used by tests to prove dispatch +
    idempotency end-to-end without any network call.

    ``connected`` controls whether :meth:`detect` reports CONNECTED; when
    False it reports NOT_CONNECTED with an honest reason_code, exactly like
    a real disconnected provider would.
    """

    def __init__(self, provider: str, target: str = "fake-target-1", connected: bool = True,
                 required: bool = False):
        if provider not in PROVIDERS:
            raise ReportingError(f"unknown provider: {provider!r}")
        self.provider_name = provider
        self._target = target
        self._connected = connected
        self._required = required
        self.calls: List[str] = []  # audit trail of every method actually invoked
        self._store: Dict[str, CommentRef] = {}

    def detect(self) -> ReportingCapability:
        self.calls.append("detect")
        if not self._connected:
            return ReportingCapability(
                provider=self.provider_name,
                state="NOT_CONNECTED",
                reason_code="no_connector_configured",
                required=self._required,
            )
        return ReportingCapability(
            provider=self.provider_name,
            state="CONNECTED",
            adapter=f"fake-{self.provider_name}",
            adapter_version="1.0.0",
            target_resolved=True,
            auth_probed=True,
            can_read=True,
            can_create_comment=True,
            can_update_comment=True,
            required=self._required,
        )

    def find_existing(self, idempotency_key: str) -> Optional[CommentRef]:
        self.calls.append("find_existing")
        return self._store.get(idempotency_key)

    def publish(self, envelope: StageEventEnvelope, target: str) -> PublishReceipt:
        self.calls.append("publish")
        key = envelope.idempotency_key(self.provider_name, target)
        body = envelope.render_body(self.provider_name)
        existing = self._store.get(key)
        if existing is not None:
            if envelope.sequence < existing.high_water_mark:
                # stale event — reject the overwrite, keep the newer state
                return PublishReceipt(
                    provider=self.provider_name, target=target,
                    run_id=envelope.run_id, task_id=envelope.task_id,
                    status="confirmed", remote_comment_id=existing.remote_comment_id,
                    body_hash=existing.body_hash, detail="stale event ignored (high-water mark)",
                )
            existing.body_hash = _body_hash(body)
            existing.high_water_mark = envelope.sequence
            return PublishReceipt(
                provider=self.provider_name, target=target,
                run_id=envelope.run_id, task_id=envelope.task_id,
                status="confirmed", remote_comment_id=existing.remote_comment_id,
                body_hash=existing.body_hash, detail="updated",
            )
        remote_id = f"{self.provider_name}-{key[:12]}"
        ref = CommentRef(provider=self.provider_name, target=target,
                          remote_comment_id=remote_id, body_hash=_body_hash(body),
                          high_water_mark=envelope.sequence)
        self._store[key] = ref
        return PublishReceipt(
            provider=self.provider_name, target=target,
            run_id=envelope.run_id, task_id=envelope.task_id,
            status="confirmed", remote_comment_id=remote_id,
            body_hash=ref.body_hash, detail="created",
        )


class _AlwaysDisconnectedStub(ReportingProvider):
    """Base for the four conditional-tracker stubs (Azure DevOps / Jira /
    Asana / Trello). In this sandbox there is no real connector/credential
    available, so ``detect`` honestly reports NOT_CONNECTED. A real
    implementation replaces ``detect``/``find_existing``/``publish`` with an
    actual host-connector/MCP/CLI/HTTPS probe — the reason_code vocabulary
    here is what that implementation is expected to preserve.
    """

    reason_code = "no_connector_configured"

    def __init__(self, target: str = ""):
        self._target = target

    def detect(self) -> ReportingCapability:
        return ReportingCapability(
            provider=self.provider_name,
            state="NOT_CONNECTED",
            reason_code=self.reason_code,
            required=False,
        )

    def find_existing(self, idempotency_key: str) -> Optional[CommentRef]:
        raise ReportingError(
            f"{self.provider_name}: find_existing() must never be called while NOT_CONNECTED"
        )

    def publish(self, envelope: StageEventEnvelope, target: str) -> PublishReceipt:
        raise ReportingError(
            f"{self.provider_name}: publish() must never be called while NOT_CONNECTED"
        )


class AzureDevOpsStubProvider(_AlwaysDisconnectedStub):
    provider_name = "azure_devops"


class JiraStubProvider(_AlwaysDisconnectedStub):
    provider_name = "jira"


class AsanaStubProvider(_AlwaysDisconnectedStub):
    provider_name = "asana"


class TrelloStubProvider(_AlwaysDisconnectedStub):
    provider_name = "trello"


# --------------------------------------------------------------------------- #
# Dispatcher
# --------------------------------------------------------------------------- #
class GitHubRequiredError(ReportingError):
    """Raised when a GitHub-sourced envelope cannot get a confirmed GitHub
    comment — GitHub is never skippable for GitHub-bound runs (rule #1)."""


class ReportingDispatcher:
    """Fans a canonical :class:`StageEventEnvelope` out to every CONNECTED
    provider registered on it, skipping (never erroring on) disconnected
    optional providers, while enforcing that GitHub is hard-required when
    ``envelope.source == "github"``.
    """

    def __init__(self, providers: Optional[List[ReportingProvider]] = None):
        self._providers: Dict[str, ReportingProvider] = {}
        for p in providers or []:
            self.register(p)

    def register(self, provider: ReportingProvider) -> None:
        self._providers[provider.provider_name] = provider

    def providers(self) -> List[str]:
        return list(self._providers.keys())

    def detect_all(self) -> Dict[str, ReportingCapability]:
        return {name: p.detect() for name, p in self._providers.items()}

    def dispatch(self, envelope: StageEventEnvelope, targets: Mapping[str, str]) -> Dict[str, PublishReceipt]:
        """``targets`` maps provider name -> target string (e.g. issue/board/
        card id) for providers that have one configured for this run.

        Returns one :class:`PublishReceipt`-shaped dict per provider that
        was either dispatched to or explicitly skipped. Never attempts a
        remote call for a provider whose capability state != CONNECTED.
        """
        ok, errors = validate_envelope(envelope.to_dict())
        if not ok:
            raise ReportingError("invalid envelope: " + "; ".join(errors))

        results: Dict[str, PublishReceipt] = {}
        github_required = envelope.source == "github"
        github_confirmed = False

        for name, provider in self._providers.items():
            target = targets.get(name, "")
            capability = provider.detect()

            if capability.connected:
                if not target:
                    # connected but no target configured for this run — nothing to publish to
                    results[name] = PublishReceipt(
                        provider=name, target="", run_id=envelope.run_id, task_id=envelope.task_id,
                        status="skipped_not_connected", reason_code="no_target_configured",
                    )
                    continue
                receipt = provider.publish(envelope, target)
                results[name] = receipt
                if name == "github" and receipt.status == "confirmed":
                    github_confirmed = True
                continue

            # not connected
            is_required = (name == "github" and github_required) or capability.required
            if is_required:
                results[name] = PublishReceipt(
                    provider=name, target=target, run_id=envelope.run_id, task_id=envelope.task_id,
                    status="blocked", reason_code=capability.reason_code or "not_connected",
                    detail=f"provider {name} is required but state={capability.state}",
                )
            else:
                results[name] = PublishReceipt(
                    provider=name, target=target, run_id=envelope.run_id, task_id=envelope.task_id,
                    status="skipped_not_connected", reason_code=capability.reason_code or "not_connected",
                )

        if github_required and "github" in self._providers and not github_confirmed:
            # GitHub-bound run: completion depends on remote confirmation.
            # We don't raise here (the dispatcher's job is to report per-provider
            # status, not to enforce COMPLETE) — but the receipt for github must
            # already reflect 'blocked' or 'pending', never a fabricated pass.
            pass

        return results

    def completion_verdict(self, results: Mapping[str, PublishReceipt], github_required: bool) -> str:
        """Reduce per-provider receipts into a single completion-auditor-facing
        verdict, distinguishing confirmed / pending / skipped_not_connected /
        blocked (per #431 integration point)."""
        if github_required:
            gh = results.get("github")
            if gh is None or gh.status != "confirmed":
                return "blocked"
        if any(r.status == "blocked" for r in results.values()):
            return "blocked"
        if any(r.status == "error" for r in results.values()):
            return "blocked"
        if any(r.status == "pending" for r in results.values()):
            return "pending"
        return "confirmed"


def default_dispatcher(github_provider: Optional[ReportingProvider] = None) -> ReportingDispatcher:
    """Build a dispatcher wired with the real (or stub) GitHub provider plus
    the four conditional-tracker stubs. Callers running against real Azure
    DevOps/Jira/Asana/Trello connectors should register their own providers
    instead of (or in addition to) the stubs."""
    providers: List[ReportingProvider] = []
    if github_provider is not None:
        providers.append(github_provider)
    providers.extend([
        AzureDevOpsStubProvider(),
        JiraStubProvider(),
        AsanaStubProvider(),
        TrelloStubProvider(),
    ])
    return ReportingDispatcher(providers)


__all__ = [
    "SCHEMA_ID",
    "PROVIDERS",
    "CAPABILITY_STATES",
    "TERMINAL_STATUSES",
    "DISPATCH_STATUSES",
    "ReportingError",
    "GitHubRequiredError",
    "StageEventEnvelope",
    "validate_envelope",
    "ReportingCapability",
    "CommentRef",
    "PublishReceipt",
    "ReportingProvider",
    "FakeReportingProvider",
    "AzureDevOpsStubProvider",
    "JiraStubProvider",
    "AsanaStubProvider",
    "TrelloStubProvider",
    "ReportingDispatcher",
    "default_dispatcher",
]
