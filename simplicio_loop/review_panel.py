"""Portable four-reviewer independent review panel (issue #427, epic #422).

Materializes the four distinct per-item reviewer roles the "Independent Review
Panel" (``review_panel``) stage abstractly names in the #423 contract
(:mod:`simplicio_loop.stage_agents`, ``contracts/stage-agents/v1/stages.json``):

* ``security_correctness_reviewer``  — ACs, logic, race, authz, injection,
  secrets, unsafe I/O, broken callers, placeholders, debug leaks.
* ``maintainability_reviewer``       — structural simplicity, boundaries,
  duplication, architecture boundaries, naming, dead code, empty tests.
* ``runtime_reproduction_verifier``  — executes the real path, target tests,
  web evidence for frontend, flow audit for cross-surface, schema/db proof,
  zero console/runtime errors.
* ``blast_radius_reviewer``          — recomputes impact/flow independently:
  planned surface vs actual diff vs reverse dependents vs sibling patterns.

This module is the portable, stdlib-only core (mirrors :mod:`stage_agents`'
validate-first design): rubric texts are hash-pinned and versioned, the
context bundle each reviewer receives is sanitized + content-addressed, findings
are deduped/voted by a pure reducer, and same-actor-identity (the implementer
signing its own review, or two reviewers sharing an instance) is mechanically
rejected — never left to prompt discipline.

Runtimes without four independent slots run in waves
(:func:`plan_reviewer_waves`); a runtime with zero independent actors available
must resolve to ``BLOCKED(independent_reviewer_unavailable)`` rather than
degrade to self-review — enforced by :func:`reject_same_actor` /
:func:`enforce_panel_independence`.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence

REASON_INDEPENDENT_REVIEWER_UNAVAILABLE = "independent_reviewer_unavailable"
REASON_SAME_ACTOR = "same_actor_identity_rejected"
REASON_WRONG_RUBRIC_HASH = "wrong_rubric_hash"
REASON_STALE_PANEL = "stale_panel_receipt"
REASON_PANEL_INCOMPLETE = "panel_incomplete"
REASON_FORGED_RECEIPT = "forged_receipt_rejected"

VERDICT_PASS = "pass"
VERDICT_FIX_REQUIRED = "fix-required"
VERDICT_BLOCKED = "blocked"

_ROLE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


class ReviewPanelError(ValueError):
    """Raised when a panel/finding/receipt violates the independence contract."""

    def __init__(self, message: str, *, reason_code: str = "review_panel_error"):
        super().__init__(message)
        self.reason_code = reason_code


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_json(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# --------------------------------------------------------------------------- #
# 1. Four roles, registered with a separate-actor requirement.
# --------------------------------------------------------------------------- #

REVIEWER_ROLE_IDS: tuple[str, ...] = (
    "security_correctness_reviewer",
    "maintainability_reviewer",
    "runtime_reproduction_verifier",
    "blast_radius_reviewer",
)

# Roles that MUST NOT be the same agent_instance_id as any reviewer (the
# implementer's own roles from the #423 graph, plus every other reviewer).
_NON_REVIEWER_INDEPENDENT_OF = ("implementation_agent", "safety_gate", "delivery_agent", "completion_auditor")


def _independent_of_for(role_id: str) -> list[str]:
    others = [r for r in REVIEWER_ROLE_IDS if r != role_id]
    return sorted(list(_NON_REVIEWER_INDEPENDENT_OF) + others)


# --------------------------------------------------------------------------- #
# 2. Rubrics — versioned, hash-pinned. Converted from simplicio-review's
#    3-rubric rubric set (.claude/skills/simplicio-review/SKILL.md) split into
#    4 exclusive rubrics per issue #427.
# --------------------------------------------------------------------------- #

RUBRIC_VERSION = "1.0.0"

RUBRICS: dict[str, str] = {
    "security_correctness_reviewer": (
        "security_correctness_reviewer/v1.0.0\n"
        "Refute this change on security & correctness. Scope: added/modified lines only.\n"
        "- Every acceptance criterion: find any AC NOT met.\n"
        "- Logic bugs: off-by-one, null/None, race conditions, resource leaks.\n"
        "- Authorization: missing/incorrect authz checks on new or changed paths.\n"
        "- Injection: SQL/command/template/log injection, unsafe deserialization, SSRF, path traversal.\n"
        "- Secrets: any credential, token or key literal newly present in the diff.\n"
        "- Unsafe I/O: unchecked file/network operations, missing validation on external input.\n"
        "- Broken callers: changed signatures/behavior that break existing callers (grep them).\n"
        "- Placeholders: fake/stubbed success (`return None`, `Ok(fake)`) where real behavior was required.\n"
        "- Debug leaks: left-on flags, commented-out guards, stray console.log/dbg!/print.\n"
        "Cite every finding as file:line with a one-line why. Default to 'not done' if uncertain."
    ),
    "maintainability_reviewer": (
        "maintainability_reviewer/v1.0.0\n"
        "Refute this change on maintainability & structure. Scope: added/modified lines only.\n"
        "- Structural simplicity: is there a markedly simpler shape for this change?\n"
        "- Boundaries: leaky abstractions, logic that belongs in an adjacent module.\n"
        "- Duplication: logic duplicated instead of reused from an existing module.\n"
        "- Architecture boundaries: layering violations, forbidden imports/deps.\n"
        "- Naming: misleading or inconsistent names for new symbols.\n"
        "- Dead code: unreachable branches, unused symbols left behind.\n"
        "- Empty/weak tests: tests that assert nothing meaningful or always pass.\n"
        "- Comments that lie or are inconsistent with the code they describe.\n"
        "Cite every finding as file:line with a one-line why. Default to 'not done' if uncertain."
    ),
    "runtime_reproduction_verifier": (
        "runtime_reproduction_verifier/v1.0.0\n"
        "Prove or refute that this change actually WORKS, not just compiles. Scope: the changed path.\n"
        "- Execute the real path (target tests, or a direct run) for every changed behavior.\n"
        "- Front-end change -> REQUIRE web evidence: a web_verify screenshot + trace path, 0 console errors.\n"
        "- Cross-surface change -> REQUIRE flow evidence (flow_audit) covering the UI->API/service path.\n"
        "- Schema/DB change -> REQUIRE schema/db proof (migration ran, query executed, row observed).\n"
        "- Zero console/runtime errors during the observed run.\n"
        "- A test that compiles but never actually exercises the change is a fail, not a pass.\n"
        "Cite every finding as file:line + the command/evidence path that proves or refutes it."
    ),
    "blast_radius_reviewer": (
        "blast_radius_reviewer/v1.0.0\n"
        "Independently recompute this change's blast radius; never trust the implementer's own claim.\n"
        "- Recompute impact/flow from the diff yourself (impact_audit/flow_audit or manual grep of callers).\n"
        "- Compare the PLANNED surface (task anchor/AC) vs the ACTUAL diff: anything touched but not planned?\n"
        "- Reverse dependents: any caller/importer of a changed symbol left unreviewed/untested?\n"
        "- Sibling patterns: does a related module/test follow the same pattern and was it missed?\n"
        "Cite every finding as file:line with a one-line why. Default to 'not done' if uncertain."
    ),
}


def rubric_hash(role_id: str) -> str:
    """Return the hash-pinned rubric hash for a reviewer role."""
    text = RUBRICS.get(role_id)
    if text is None:
        raise ReviewPanelError(f"unknown reviewer role_id: {role_id}", reason_code="unknown_role")
    return _sha256_text(text)


def verify_rubric_hash(role_id: str, claimed_hash: str) -> bool:
    return claimed_hash == rubric_hash(role_id)


def build_role_definitions() -> list[dict[str, Any]]:
    """Materialize the four concrete reviewer roles (simplicio.agent-role/v1 shape)."""
    titles = {
        "security_correctness_reviewer": "Security & Correctness Reviewer",
        "maintainability_reviewer": "Maintainability Reviewer",
        "runtime_reproduction_verifier": "Runtime/Reproduction Verifier",
        "blast_radius_reviewer": "Blast-Radius Reviewer",
    }
    descriptions = {
        "security_correctness_reviewer": "Independent reviewer for ACs, logic, race, authz, injection, secrets, unsafe I/O, broken callers, placeholders and debug leaks.",
        "maintainability_reviewer": "Independent reviewer for structural simplicity, boundaries, duplication, architecture boundaries, naming, dead code and empty tests.",
        "runtime_reproduction_verifier": "Independent verifier that executes the real path, target tests, web/flow/schema evidence, and checks for zero runtime errors.",
        "blast_radius_reviewer": "Independent reviewer that recomputes impact/flow: planned surface vs actual diff vs reverse dependents vs sibling patterns.",
    }
    return [
        {
            "schema": "simplicio.agent-role/v1",
            "role_id": role_id,
            "version": RUBRIC_VERSION,
            "title": titles[role_id],
            "description": descriptions[role_id],
            "required_capabilities": ["claim", "fencing", "receipts", "evidence"],
            "forbidden_to_self_sign": ["implementation", "security_signoff", "delivery", "completion"],
            "independent_of_roles": _independent_of_for(role_id),
        }
        for role_id in REVIEWER_ROLE_IDS
    ]


# --------------------------------------------------------------------------- #
# 3. Single sanitized, content-addressed context bundle.
# --------------------------------------------------------------------------- #

# Never handed to a reviewer: the implementer's private reasoning/transcript.
FORBIDDEN_CONTEXT_KEYS = frozenset((
    "transcript", "private_reasoning", "implementer_notes", "chain_of_thought",
    "scratchpad", "internal_notes", "agent_thoughts",
))


def build_context_bundle(*, diff: str, acceptance_criteria: Sequence[str],
                          evidence_refs: Sequence[str], base_hash: str,
                          raw_extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build the single sanitized, content-addressed bundle shared by all four reviewers.

    Any key in ``raw_extra`` that could carry the implementer's private
    reasoning/transcript is stripped before hashing — the bundle handed to
    reviewers can never leak it, and the hash proves the bundle wasn't altered
    per-reviewer.
    """
    extra = dict(raw_extra or {})
    stripped = [k for k in extra if k in FORBIDDEN_CONTEXT_KEYS]
    for key in stripped:
        del extra[key]
    bundle = {
        "schema": "simplicio.review-context-bundle/v1",
        "diff": diff,
        "acceptance_criteria": list(acceptance_criteria),
        "evidence_refs": list(evidence_refs),
        "base_hash": base_hash,
        "extra": extra,
    }
    bundle["context_hash"] = _sha256_json({k: v for k, v in bundle.items() if k != "context_hash"})
    if stripped:
        bundle["sanitized_keys_removed"] = sorted(stripped)
    return bundle


# --------------------------------------------------------------------------- #
# 4. Panel signature — invalidates every receipt on diff/head/plan change.
# --------------------------------------------------------------------------- #

def panel_signature(*, base_hash: str, head_sha: str, plan_revision: int) -> str:
    return _sha256_json({"base_hash": base_hash, "head_sha": head_sha, "plan_revision": plan_revision})


def is_stale(receipt: Mapping[str, Any], *, current_signature: str) -> bool:
    return str(receipt.get("panel_signature", "")) != str(current_signature)


# --------------------------------------------------------------------------- #
# 5. Same-actor-identity rejection (mechanically impossible to fake).
# --------------------------------------------------------------------------- #

def reject_same_actor(*, implementer_instance_id: str,
                       reviewer_instance_ids: Mapping[str, str]) -> None:
    """Raise ReviewPanelError if any reviewer shares the implementer's instance,
    or if two reviewers share an instance with each other."""
    seen: dict[str, str] = {}
    for role_id, instance_id in reviewer_instance_ids.items():
        if instance_id and instance_id == implementer_instance_id:
            raise ReviewPanelError(
                f"reviewer {role_id} shares agent_instance_id with the implementer",
                reason_code=REASON_SAME_ACTOR,
            )
        if instance_id in seen:
            raise ReviewPanelError(
                f"reviewers {seen[instance_id]} and {role_id} share agent_instance_id {instance_id}",
                reason_code=REASON_SAME_ACTOR,
            )
        seen[instance_id] = role_id


def enforce_panel_independence(*, implementer_instance_id: str,
                                reviewer_instance_ids: Mapping[str, str]) -> tuple[bool, list[str]]:
    """Non-raising variant of :func:`reject_same_actor`. Returns (ok, errors)."""
    try:
        reject_same_actor(
            implementer_instance_id=implementer_instance_id,
            reviewer_instance_ids=reviewer_instance_ids,
        )
        return True, []
    except ReviewPanelError as exc:
        return False, [str(exc)]


# --------------------------------------------------------------------------- #
# 6. Finding schema + normalization + dedup/vote reducer (pure).
# --------------------------------------------------------------------------- #

FINDING_CLASSES = frozenset((
    "security", "correctness", "maintainability", "runtime", "blast_radius",
))

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")


def normalize_claim(claim: str) -> str:
    """Deterministic normalization used as (file, line, normalized_claim) dedup key."""
    text = claim.strip().lower()
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def make_finding(*, role_id: str, file: str, line: int, claim: str, finding_class: str,
                  confidence: str = "medium", evidence_refs: Sequence[str] = ()) -> dict[str, Any]:
    if finding_class not in FINDING_CLASSES:
        raise ReviewPanelError(f"invalid finding class: {finding_class}", reason_code="invalid_finding_class")
    if confidence not in ("low", "medium", "high"):
        raise ReviewPanelError(f"invalid confidence: {confidence}", reason_code="invalid_finding_confidence")
    if not str(file).strip() or not isinstance(line, int) or line < 0:
        raise ReviewPanelError("finding requires a non-empty file and a non-negative line", reason_code="invalid_finding")
    return {
        "schema": "simplicio.review-finding/v1",
        "role_id": role_id,
        "file": str(file),
        "line": int(line),
        "claim": str(claim),
        "normalized_claim": normalize_claim(claim),
        "class": finding_class,
        "confidence": confidence,
        "evidence_refs": list(evidence_refs),
    }


def dedup_findings(findings: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Dedup by (file, line, normalized_claim); overlap RAISES confidence via vote count.

    Never discards a high-confidence security finding, even a lone one — each
    deduped group keeps ``max_confidence`` and the full list of contributing
    ``roles`` so the synthesizer can apply the security-single-vote-block rule.
    """
    groups: dict[tuple[str, int, str], dict[str, Any]] = {}
    order: list[tuple[str, int, str]] = []
    _rank = {"low": 0, "medium": 1, "high": 2}
    for finding in findings:
        key = (finding["file"], finding["line"], finding["normalized_claim"])
        if key not in groups:
            groups[key] = {
                "file": finding["file"], "line": finding["line"],
                "normalized_claim": finding["normalized_claim"], "class": finding["class"],
                "claims": [finding["claim"]], "roles": [finding["role_id"]],
                "votes": 1, "max_confidence": finding["confidence"],
                "evidence_refs": list(finding.get("evidence_refs", ())),
            }
            order.append(key)
        else:
            group = groups[key]
            if finding["role_id"] not in group["roles"]:
                group["roles"].append(finding["role_id"])
                group["votes"] += 1
            group["claims"].append(finding["claim"])
            if _rank[finding["confidence"]] > _rank[group["max_confidence"]]:
                group["max_confidence"] = finding["confidence"]
            group["evidence_refs"].extend(finding.get("evidence_refs", ()))
    return [groups[k] for k in order]


# --------------------------------------------------------------------------- #
# 7. Review receipt schema + validation (extends simplicio.stage-receipt/v1).
# --------------------------------------------------------------------------- #

_REQUIRED_RECEIPT_FIELDS = (
    "receipt_id", "agent_instance_id", "role_id", "run_id", "task_id",
    "attempt_id", "fence", "plan_revision", "context_hash", "rubric_hash",
    "panel_signature", "verdict", "findings", "evidence_refs",
)


def make_review_receipt(*, receipt_id: str, agent_instance_id: str, role_id: str,
                         run_id: str, task_id: str, attempt_id: str, fence: str,
                         plan_revision: int, context_hash: str,
                         panel_sig: str, verdict: str, findings: Sequence[Mapping[str, Any]],
                         evidence_refs: Sequence[str], accepted: bool,
                         created_at: str) -> dict[str, Any]:
    if role_id not in REVIEWER_ROLE_IDS:
        raise ReviewPanelError(f"unknown reviewer role_id: {role_id}", reason_code="unknown_role")
    return {
        "schema": "simplicio.review-receipt/v1",
        "receipt_id": receipt_id,
        "agent_instance_id": agent_instance_id,
        "role_id": role_id,
        "run_id": run_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "fence": fence,
        "plan_revision": plan_revision,
        "context_hash": context_hash,
        "rubric_hash": rubric_hash(role_id),
        "panel_signature": panel_sig,
        "verdict": verdict,
        "findings": [dict(f) for f in findings],
        "evidence_refs": list(evidence_refs),
        "accepted": accepted,
        "created_at": created_at,
    }


def validate_review_receipt(receipt: Mapping[str, Any], *, expected_context_hash: str,
                             expected_panel_signature: str,
                             implementer_instance_id: str | None = None) -> tuple[bool, list[str]]:
    """Validate a review receipt: identity, hash pinning, freshness, non-forgery."""
    errors: list[str] = []
    if not isinstance(receipt, Mapping):
        return False, ["receipt must be an object"]

    for field_name in _REQUIRED_RECEIPT_FIELDS:
        if field_name not in receipt or (isinstance(receipt.get(field_name), str) and not receipt[field_name].strip()):
            errors.append(f"receipt.{field_name} is required")

    role_id = receipt.get("role_id")
    if role_id not in REVIEWER_ROLE_IDS:
        errors.append(f"receipt.role_id {role_id!r} is not a registered reviewer role")
    elif str(receipt.get("rubric_hash", "")) != rubric_hash(role_id):
        errors.append(f"receipt.rubric_hash does not match the hash-pinned rubric for {role_id}")

    if str(receipt.get("context_hash", "")) != str(expected_context_hash):
        errors.append("receipt.context_hash does not match the panel's context bundle (forged/cross-head artifact)")

    if is_stale(receipt, current_signature=expected_panel_signature):
        errors.append("receipt.panel_signature is stale (diff/head/plan_revision changed)")

    if implementer_instance_id is not None and receipt.get("agent_instance_id") == implementer_instance_id:
        errors.append("receipt.agent_instance_id matches the implementer instance (same-actor rejected)")

    if receipt.get("verdict") not in (VERDICT_PASS, "fail", VERDICT_BLOCKED, "skip"):
        errors.append("receipt.verdict has an invalid value")

    findings = receipt.get("findings")
    if not isinstance(findings, list):
        errors.append("receipt.findings must be a list")
    else:
        for finding in findings:
            if not isinstance(finding, Mapping) or finding.get("role_id") != role_id:
                errors.append("receipt.findings contains a finding not authored by this reviewer's role_id")
                break

    return (len(errors) == 0), errors


# --------------------------------------------------------------------------- #
# 8. Pure dedup/synthesis reducer.
# --------------------------------------------------------------------------- #


def synthesize(receipts: Sequence[Mapping[str, Any]], *, quorum_roles: Sequence[str] = REVIEWER_ROLE_IDS) -> dict[str, Any]:
    """Synthesize the four reviewer receipts into one gate verdict.

    Rules (issue #427 "Síntese"):
    - dedup by file:line + normalized claim; record votes;
    - never discard a high-confidence security finding, even with 1 vote;
    - majority-refute (>= half of present reviewers) on any claim sends the
      panel back to implementation (``fix-required``);
    - any reviewer BLOCKED (or missing) keeps the gate non-terminal
      (``blocked``, reason ``independent_reviewer_unavailable``/``panel_incomplete``);
    - PASS only when all required receipts are present, valid and accepted.
    """
    by_role = {r.get("role_id"): r for r in receipts}
    missing = [role_id for role_id in quorum_roles if role_id not in by_role]
    if missing:
        return {
            "verdict": VERDICT_BLOCKED,
            "reason_code": REASON_INDEPENDENT_REVIEWER_UNAVAILABLE if len(missing) == len(quorum_roles)
            else REASON_PANEL_INCOMPLETE,
            "missing_roles": missing,
            "findings": [],
        }

    blocked_roles = [role_id for role_id, r in by_role.items() if r.get("verdict") == VERDICT_BLOCKED]
    if blocked_roles:
        return {
            "verdict": VERDICT_BLOCKED,
            "reason_code": REASON_INDEPENDENT_REVIEWER_UNAVAILABLE,
            "missing_roles": [],
            "blocked_roles": sorted(blocked_roles),
            "findings": [],
        }

    all_findings: list[dict[str, Any]] = []
    for r in receipts:
        all_findings.extend(r.get("findings", ()))
    deduped = dedup_findings(all_findings)

    n_reviewers = len(quorum_roles)
    majority_threshold = (n_reviewers // 2) + 1

    high_confidence_security = [f for f in deduped if f["class"] == "security" and f["max_confidence"] == "high"]
    majority_refuted = [f for f in deduped if f["votes"] >= majority_threshold]

    accepted_ok = all(r.get("accepted") for r in receipts)
    pass_ok = all(r.get("verdict") == VERDICT_PASS for r in receipts)

    if high_confidence_security:
        return {
            "verdict": VERDICT_FIX_REQUIRED,
            "reason_code": "high_confidence_security_finding",
            "findings": deduped,
            "blocking": [f["normalized_claim"] for f in high_confidence_security],
        }
    if majority_refuted:
        return {
            "verdict": VERDICT_FIX_REQUIRED,
            "reason_code": "majority_refute",
            "findings": deduped,
            "blocking": [f["normalized_claim"] for f in majority_refuted],
        }
    if pass_ok and accepted_ok:
        return {"verdict": VERDICT_PASS, "reason_code": "ok", "findings": deduped}
    return {
        "verdict": VERDICT_FIX_REQUIRED,
        "reason_code": "not_all_pass",
        "findings": deduped,
        "blocking": [role for role, r in by_role.items() if r.get("verdict") != VERDICT_PASS or not r.get("accepted")],
    }


# --------------------------------------------------------------------------- #
# 9. Waves — runtimes without four independent slots run in waves.
# --------------------------------------------------------------------------- #


def plan_reviewer_waves(available_slots: int, *, role_ids: Sequence[str] = REVIEWER_ROLE_IDS) -> list[list[str]]:
    """Split the four independent reviewer roles into capacity-bounded waves.

    Every role is independent of every other (no depends_on among reviewers),
    so waves are pure capacity chunks, in stable role order.
    """
    if available_slots <= 0:
        raise ReviewPanelError(
            "zero independent slots available for the review panel",
            reason_code=REASON_INDEPENDENT_REVIEWER_UNAVAILABLE,
        )
    roles = list(role_ids)
    return [roles[i:i + available_slots] for i in range(0, len(roles), available_slots)]


__all__ = [
    "FINDING_CLASSES",
    "FORBIDDEN_CONTEXT_KEYS",
    "REASON_FORGED_RECEIPT",
    "REASON_INDEPENDENT_REVIEWER_UNAVAILABLE",
    "REASON_PANEL_INCOMPLETE",
    "REASON_SAME_ACTOR",
    "REASON_STALE_PANEL",
    "REASON_WRONG_RUBRIC_HASH",
    "REVIEWER_ROLE_IDS",
    "RUBRICS",
    "RUBRIC_VERSION",
    "ReviewPanelError",
    "VERDICT_BLOCKED",
    "VERDICT_FIX_REQUIRED",
    "VERDICT_PASS",
    "build_context_bundle",
    "build_role_definitions",
    "dedup_findings",
    "enforce_panel_independence",
    "is_stale",
    "make_finding",
    "make_review_receipt",
    "normalize_claim",
    "panel_signature",
    "plan_reviewer_waves",
    "reject_same_actor",
    "rubric_hash",
    "synthesize",
    "validate_review_receipt",
    "verify_rubric_hash",
]
