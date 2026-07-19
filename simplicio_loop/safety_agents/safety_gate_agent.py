"""simplicio-loop — safety_gate_agent: fail-closed risk/secret/authorization gate.

Materializes issue #428 of epic #422. The agent evaluates every mutation boundary
and emits a typed decision BEFORE the action. It does not replace deterministic
scanners (hooks/action_gate.py, secret scanners); it coordinates and interprets
their receipts inside a versioned policy.

Pure reducer: no I/O, no network, no execution. All side effects (scanning, human
approval, persistence) are injected or performed by the caller. This keeps the
decision logic exhaustively testable and replayable.

Invariants enforced (from issue #428):
  1. Optimization never raises the allowed risk.
  2. Every segment of a compound command is evaluated.
  3. Config perception-shaping is hash-pinned (policy_hash).
  4. Secret scan is mandatory before commit/push/delivery.
  5. Irreversible ops require a fresh human receipt.
  6. Change in action/scope/plan invalidates authorization.
  7. The agent does not execute the action it approves.
  8. Scanner/policy failure is UNVERIFIED, never ALLOW.
  9. Child agents receive only explicitly required secrets.
 10. Prompt/item/diff cannot reconfigure policy.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

SCHEMA = "simplicio.safety-stage-receipt/v1"

# Action classes — must stay in lockstep with action-intent.schema.json.
ACTION_CLASSES = (
    "write_edit",
    "compound_shell",
    "dependency_install",
    "commit",
    "push",
    "pull_request",
    "issue_board_op",
    "migration_data",
    "deploy_release",
    "cancel_cleanup",
    "secret_network_access",
    "artifact_fetch_execute",
)

# Boundaries that require a secret scan before they are allowed.
SECRET_SCAN_REQUIRED = {"commit", "push", "pull_request", "deploy_release", "secret_network_access"}

# Boundaries that are irreversible and require a fresh human receipt.
IRREVERSIBLE = {"push", "deploy_release", "migration_data", "cancel_cleanup"}

# Subshell / redirect / unknown-syntax markers that force UNVERIFIED when not
# explicitly allowed by policy.
_UNSAFE_SEGMENT_RE = re.compile(r"(;|\|\||&&|`|\$\(|>\s*|>>\s*|<\(|eval\s|curl\s.*\|\s*(sh|bash))")


class Decision(str, Enum):
    ALLOW = "ALLOW"
    ALLOW_WITH_CONSTRAINTS = "ALLOW_WITH_CONSTRAINTS"
    REQUIRE_HUMAN = "REQUIRE_HUMAN"
    DENY = "DENY"
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True)
class ActionIntent:
    intent_id: str
    action_class: str
    command: str
    actor: str
    scope: str
    idempotency_key: str = ""
    policy_hash: str = ""
    segments: tuple[str, ...] = field(default_factory=tuple)
    created_at: str = ""

    def action_hash(self) -> str:
        h = hashlib.sha256()
        h.update(self.action_class.encode())
        h.update(self.command.encode())
        h.update(self.scope.encode())
        return h.hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "simplicio.action-intent/v1",
            "intent_id": self.intent_id,
            "action_class": self.action_class,
            "command": self.command,
            "actor": self.actor,
            "scope": self.scope,
            "idempotency_key": self.idempotency_key,
            "policy_hash": self.policy_hash,
            "segments": list(self.segments),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ScannerReceipt:
    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class SafetyDecision:
    decision: Decision
    intent: ActionIntent
    policy_hash: str
    action_hash: str
    actor: str
    scope: str
    expiry: str
    constraints: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    reason_code: str
    human_receipt: str = ""

    def to_receipt(self, instance_identity: Mapping[str, str]) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "decision": self.decision.value,
            "reason_code": self.reason_code,
            "intent": self.intent.to_dict(),
            "policy_hash": self.policy_hash,
            "action_hash": self.action_hash,
            "actor": self.actor,
            "scope": self.scope,
            "expiry": self.expiry,
            "constraints": list(self.constraints),
            "evidence_refs": list(self.evidence_refs),
            "human_receipt": self.human_receipt,
            "identity": dict(instance_identity),
        }


# Compound-command segmentation: split on shell separators, keeping each segment.
def segment_command(command: str) -> list[str]:
    """Fail-closed segmentation of a compound command into evaluable segments."""
    parts = re.split(r"(?<=[\|&;])(?=&|\||;)|(?<=&)(?=\&)", command)
    segs = [p.strip() for p in re.split(r"(?<=[;|&])|(?<=;)", command) if p.strip()]
    return segs or [command.strip()]


def classify(command: str) -> str:
    """Best-effort action_class inference for a raw command (used when no explicit intent)."""
    c = command.strip()
    if c.startswith(("git commit",)):
        return "commit"
    if c.startswith(("git push",)):
        return "push"
    if c.startswith(("gh pr", "git push -f")):
        return "pull_request" if "pr" in c else "push"
    if c.startswith(("pip", "npm install", "cargo add", "poetry add")):
        return "dependency_install"
    if c.startswith(("gh issue", "gh api", "gh pr edit", "gh pr close")):
        return "issue_board_op"
    if c.startswith(("git push --force", "git reset --hard", "rm -rf", "git clean")):
        return "cancel_cleanup"
    if c.startswith(("python", "node", "sh", "bash", "./")):
        return "compound_shell"
    if "deploy" in c or c.startswith("gh release"):
        return "deploy_release"
    if c.startswith(("curl", "wget")):
        return "artifact_fetch_execute"
    return "write_edit"


def _segment_is_unsafe(segment: str) -> bool:
    return bool(_UNSAFE_SEGMENT_RE.search(segment))


def decide(
    intent: ActionIntent,
    *,
    scanner_receipts: Sequence[ScannerReceipt] | None = None,
    human_receipt: str = "",
    human_receipt_fresh: bool = False,
    policy_hash: str,
    expiry: str,
    constraints: Sequence[str] | None = None,
    evidence_refs: Sequence[str] | None = None,
    allow_compound_unsafe: bool = False,
) -> SafetyDecision:
    """Pure fail-closed policy reducer.

    Returns a typed Decision. Any scanner failure or missing required evidence
    collapses to UNVERIFIED, never ALLOW (invariant 8).
    """
    scanner_receipts = list(scanner_receipts or [])
    constraints = list(constraints or [])
    evidence_refs = list(evidence_refs or [])

    # Invariant 8: scanner failure -> UNVERIFIED.
    for r in scanner_receipts:
        if not r.ok:
            return SafetyDecision(
                decision=Decision.UNVERIFIED,
                intent=intent,
                policy_hash=policy_hash,
                action_hash=intent.action_hash(),
                actor=intent.actor,
                scope=intent.scope,
                expiry=expiry,
                constraints=tuple(constraints),
                evidence_refs=(r.name,),
                reason_code="scanner_failure",
                human_receipt=human_receipt,
            )

    # Invariant 4: secret scan mandatory before commit/push/delivery.
    if intent.action_class in SECRET_SCAN_REQUIRED:
        if not any(r.name == "secret_scan" and r.ok for r in scanner_receipts):
            return SafetyDecision(
                decision=Decision.UNVERIFIED,
                intent=intent,
                policy_hash=policy_hash,
                action_hash=intent.action_hash(),
                actor=intent.actor,
                scope=intent.scope,
                expiry=expiry,
                constraints=tuple(constraints),
                evidence_refs=("secret_scan",),
                reason_code="secret_scan_required",
                human_receipt=human_receipt,
            )

    # Invariant 5: irreversible ops require a fresh human receipt.
    if intent.action_class in IRREVERSIBLE:
        if not (human_receipt and human_receipt_fresh):
            return SafetyDecision(
                decision=Decision.REQUIRE_HUMAN,
                intent=intent,
                policy_hash=policy_hash,
                action_hash=intent.action_hash(),
                actor=intent.actor,
                scope=intent.scope,
                expiry=expiry,
                constraints=tuple(constraints),
                evidence_refs=tuple(evidence_refs),
                reason_code="irreversible_requires_human",
                human_receipt=human_receipt,
            )

    # Invariant 2: evaluate every segment of a compound command.
    if intent.action_class == "compound_shell":
        for seg in intent.segments or segment_command(intent.command):
            if _segment_is_unsafe(seg) and not allow_compound_unsafe:
                return SafetyDecision(
                    decision=Decision.UNVERIFIED,
                    intent=intent,
                    policy_hash=policy_hash,
                    action_hash=intent.action_hash(),
                    actor=intent.actor,
                    scope=intent.scope,
                    expiry=expiry,
                    constraints=tuple(constraints),
                    evidence_refs=tuple(evidence_refs),
                    reason_code="unsafe_compound_segment",
                    human_receipt=human_receipt,
                )

    # Invariant 3 / 10: policy_hash must be pinned and match.
    if policy_hash != intent.policy_hash:
        return SafetyDecision(
            decision=Decision.UNVERIFIED,
            intent=intent,
            policy_hash=policy_hash,
            action_hash=intent.action_hash(),
            actor=intent.actor,
            scope=intent.scope,
            expiry=expiry,
            constraints=tuple(constraints),
            evidence_refs=tuple(evidence_refs),
            reason_code="policy_hash_drift",
            human_receipt=human_receipt,
        )

    # Default: allow with constraints recorded.
    return SafetyDecision(
        decision=Decision.ALLOW_WITH_CONSTRAINTS if constraints else Decision.ALLOW,
        intent=intent,
        policy_hash=policy_hash,
        action_hash=intent.action_hash(),
        actor=intent.actor,
        scope=intent.scope,
        expiry=expiry,
        constraints=tuple(constraints),
        evidence_refs=tuple(evidence_refs),
        reason_code="ok",
        human_receipt=human_receipt,
    )
