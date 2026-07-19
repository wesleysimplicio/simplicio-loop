"""GitHub stage-report envelope (#433 "Portable Stage Agents" + #442 identity/idempotency spec).

#301 shipped ONE progress comment per issue, fail-open, percentage-oriented
(`scripts/pr_evidence.py::cmd_progress_comment`). #433 (epic #422, "Portable Stage Agents")
asks for a living, per-work-item comment covering the FULL lifecycle
(discovered -> claimed -> intake/planning -> implementation -> safety -> review A/B/C ->
delivery/PR/checks/merge -> feedback/retry/recovery -> final audit ->
COMPLETE|PARTIAL|BLOCKED|REGRESSED), posted on both the source issue and the PR, projecting
canonical stage-agent events/receipts rather than being the authority itself.

#442 (born from running #423) narrows the envelope/identity/idempotency-key details that #433
left open and this module implements BOTH in one place rather than as two overlapping
mechanisms:

  * agent identity string ``Name/Role - #XXXX - Model``, where ``#XXXX`` is the local
    hostname abbreviated to 4 characters (`format_agent_identity`/`hostname_abbrev`) --
    NOT an issue number, despite the `#` sigil; this lets the SAME model/role show up
    distinctly per machine (#442 requirement 6: "mesmo modelo aparece em máquinas
    diferentes");
  * an idempotency key stable across retries: ``run_id + item + stage + attempt +
    transition`` (`idempotency_key`) -- a retry that re-sends the identical transition
    must not create a second comment nor even re-PATCH identical content;
  * explicit status tags ``PASS`` / ``REGRESSED`` / ``BLOCKED`` / ``NEEDS-HUMAN``
    (`STATUS_TAGS`), rejected at render time if the caller passes anything else;
  * cross-links between issue/PR/commit/evidence in every rendered report;
  * a per-work-item HTML marker, ``<!-- simplicio-loop:stage-report:v1 run=<run_id>
    item=<item> -->`` (`build_marker`), scoped to `run_id` + `item` -- stable across the
    WHOLE lifecycle of that item so every stage/attempt/transition updates the SAME
    comment (not one marker per stage/attempt; the idempotency key inside the body is what
    changes turn to turn, the marker used for find-and-update never does).

This module deliberately does NOT talk to `gh` directly. Exactly like
`simplicio_loop/github_lifecycle.py::publish_lifecycle_state` takes
`publish_comment_fn` as an injected callable (its own docstring: "kept as a parameter, not
a hard import, so this module has no import cycle with `scripts/`"), `publish_stage_report`
below takes the SAME `scripts.pr_evidence.publish_comment` primitive injected by the caller.
That primitive already IS the fail-closed, no-shell-interpolation, JSON-payload-on-stdin,
marker-based create-or-update the "#295 audit" hardened -- this module reuses it rather than
re-implementing a second one, and `find_existing_comment`'s query-before-decide behavior is
the mechanism (not a side unit-tested helper) that keeps the real publish path duplicate-free:
`publish_stage_report` always calls `publish_comment_fn(..., marker=<this item's marker>)`,
which itself always calls `find_existing_comment` first.

CLI: `scripts/stage_report.py` (publish/preview/status, dry-run/mock friendly).
"""
from __future__ import annotations

import hashlib
import re
import socket
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

STAGE_REPORT_SCHEMA = "simplicio.github-stage-report/v1"

# #442: "registrar explicitamente PASS, REGRESSED, BLOCKED ou NEEDS-HUMAN" -- the issue is
# explicit that no quarantine state is ever introduced (#442 functional requirement 7:
# "Não criar, usar ou sugerir qualquer estado de quarentena"); BLOCKED/NEEDS-HUMAN cover that
# ground instead.
STATUS_TAGS = ("PASS", "REGRESSED", "BLOCKED", "NEEDS-HUMAN")

# #433's lifecycle timeline (discovered -> ... -> COMPLETE|PARTIAL|BLOCKED|REGRESSED) — used
# only for validation/documentation of the `stage` field, never enforced as a strict FSM here
# (the stage-agent contract modules, e.g. `stage_agent_coordinator.py`, own the real state
# machine; this module only PROJECTS it).
KNOWN_STAGES = (
    "discovered", "claimed", "intake", "planning", "implementation", "safety",
    "review", "delivery", "checks", "merge", "recovery", "audit",
)

DEFAULT_MAX_BODY_CHARS = 60000  # GitHub's real comment cap is 65536; leave headroom for the marker


def hostname_abbrev(hostname: Optional[str] = None) -> str:
    """4-char abbreviation of the local computer name (#442: "computador abreviado em quatro
    caracteres"). Strips any domain suffix, keeps only alnum characters, uppercases, and pads
    a too-short name with `X` so the result is always exactly 4 characters -- a stable,
    length-predictable token for the identity string, never empty."""
    host = (hostname if hostname is not None else socket.gethostname()) or ""
    host = host.split(".")[0]
    letters = "".join(ch for ch in host if ch.isalnum()) or "HOST"
    letters = letters.upper()
    if len(letters) < 4:
        letters = (letters + "XXXX")[:4]
    return letters[:4]


def format_agent_identity(name: str, role: str, model: str, *, hostname: Optional[str] = None) -> str:
    """``Name/Role - #XXXX - Model`` (#442) -- `#XXXX` is the 4-char hostname abbreviation,
    never an issue number, so the SAME model/role reads distinctly per machine."""
    host4 = hostname_abbrev(hostname)
    name = (name or "agent").strip()
    role = (role or "worker").strip()
    model = (model or "unknown-model").strip()
    return "%s/%s - #%s - %s" % (name, role, host4, model)


def build_marker(run_id: str, item: str) -> str:
    """Stable per-work-item marker: same comment updated across the FULL lifecycle of one
    `run_id` + `item` pair, on whichever target (issue or PR) it is published to."""
    return "<!-- simplicio-loop:stage-report:v1 run=%s item=%s -->" % (run_id, item)


def idempotency_key(run_id: str, item: str, stage: str, attempt: Any, transition: str) -> str:
    """#442: "chave de idempotência estável por run_id + item + stage + attempt + transition,
    sem duplicar comentários em retry". A deterministic, short (24-hex-char) digest so the SAME
    (run, item, stage, attempt, transition) tuple ALWAYS reduces to the SAME key -- callers can
    compare keys to detect "this is a pure retry of a transition I already reported" without
    needing to diff full comment bodies."""
    raw = "|".join(str(part) for part in (run_id, item, stage, attempt, transition))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


_SECRET_PATTERNS = (
    re.compile(r"(gh[pousr]_[A-Za-z0-9]{20,})"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+"),
    re.compile(r"https://[^/\s]+/[^\s]*[?&](?:X-Amz-Signature|sig|signature|token)=[^\s]+", re.IGNORECASE),
)


def sanitize(text: str) -> str:
    """Best-effort redaction before any content reaches a public GitHub comment: bearer-style
    tokens, `key: value`/`key=value` secret-shaped fields, and signed URLs (`?sig=`/`X-Amz-
    Signature=`/...). Mirrors `github_lifecycle._redact` in spirit (kept as a separate, small
    function here rather than importing a private helper across modules) plus the extra
    signed-URL rule #442's security test matrix calls for."""
    if not text:
        return ""
    out = text
    out = _SECRET_PATTERNS[0].sub("[REDACTED-TOKEN]", out)
    out = _SECRET_PATTERNS[1].sub(lambda m: "%s: [REDACTED]" % m.group(1), out)
    out = _SECRET_PATTERNS[2].sub("[REDACTED-SIGNED-URL]", out)
    return out


def truncate_body(body: str, max_chars: int = DEFAULT_MAX_BODY_CHARS) -> str:
    """Cap total comment size (#433 "aplicar max body size, truncamento por prioridade"). A
    body under the cap passes through byte-identical; an oversized one is cut with a visible
    truncation notice rather than silently dropped or rejected by the GitHub API."""
    if len(body) <= max_chars:
        return body
    notice = "\n\n_...truncated: body exceeded %d chars..._\n" % max_chars
    return body[: max_chars - len(notice)] + notice


def render_stage_report(
    *,
    run_id: str,
    item: str,
    stage: str,
    agent_identity: str,
    status: str,
    attempt: Any = 1,
    fence: str = "",
    transition: str = "update",
    reason_code: str = "",
    receipt_id: str = "",
    issue: Optional[str] = None,
    pr: Optional[str] = None,
    commit: str = "",
    evidence: Optional[Sequence[str]] = None,
    next_gate: str = "",
    blockers: Optional[Sequence[str]] = None,
    stages_table: Optional[Sequence[Mapping[str, Any]]] = None,
    ac_table: Optional[Sequence[Mapping[str, Any]]] = None,
    updated_at: Optional[str] = None,
    idem_key: Optional[str] = None,
    max_chars: int = DEFAULT_MAX_BODY_CHARS,
) -> str:
    """Deterministic, sanitized renderer for the stage-report envelope
    (`simplicio.github-stage-report/v1`). Raises `ValueError` on an unrecognized `status` tag
    (#442: only PASS/REGRESSED/BLOCKED/NEEDS-HUMAN are legal) -- never silently accepts a typo'd
    status into a public comment."""
    if status not in STATUS_TAGS:
        raise ValueError("invalid status tag %r; must be one of %s" % (status, STATUS_TAGS))
    updated_at = updated_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    idem_key = idem_key or idempotency_key(run_id, item, stage, attempt, transition)

    lines: List[str] = [build_marker(run_id, item), ""]
    lines.append("## Simplicio Loop — stage report")
    lines.append("")
    lines.append("| Campo | Valor |")
    lines.append("|---|---|")
    lines.append("| Run / Item | %s / %s |" % (run_id, item))
    lines.append("| Stage / Transition | %s / %s |" % (stage, transition))
    lines.append("| Agente | %s |" % agent_identity)
    lines.append("| Status | **%s** |" % status)
    lines.append("| Attempt / Fence | %s / %s |" % (attempt, fence or "-"))
    if reason_code:
        lines.append("| Reason code | %s |" % reason_code)
    if receipt_id:
        lines.append("| Receipt | %s |" % receipt_id)
    lines.append("| Atualizado | %s |" % updated_at)
    lines.append("| Idempotency key | `%s` |" % idem_key)
    lines.append("")

    links: List[str] = []
    if issue:
        links.append("Issue #%s" % str(issue).lstrip("#"))
    if pr:
        links.append("PR #%s" % str(pr).lstrip("#"))
    if commit:
        links.append("Commit `%s`" % commit)
    if links:
        lines.append("### Links")
        lines.append(" · ".join(links))
        lines.append("")

    if stages_table:
        lines.append("### Stages")
        lines.append("| Stage | Agent | Status | Attempt | Evidence | Updated |")
        lines.append("|---|---|---|---:|---|---|")
        for row in stages_table:
            lines.append("| %s | %s | %s | %s | %s | %s |" % (
                row.get("stage", ""), row.get("agent", ""), row.get("status", ""),
                row.get("attempt", ""), row.get("evidence", ""), row.get("updated", "")))
        lines.append("")

    if ac_table:
        lines.append("### Acceptance criteria")
        lines.append("| AC | Status | Verified by | Receipt/evidence |")
        lines.append("|---|---|---|---|")
        for row in ac_table:
            lines.append("| %s | %s | %s | %s |" % (
                row.get("id", ""), row.get("status", ""), row.get("verified_by", ""),
                row.get("evidence", "")))
        lines.append("")

    if blockers:
        lines.append("### Blockers")
        for b in blockers:
            lines.append("- %s" % b)
        lines.append("")

    if evidence:
        lines.append("### Evidence")
        for e in evidence:
            lines.append("- %s" % e)
        lines.append("")

    if next_gate:
        lines.append("### Next gate")
        lines.append(next_gate)
        lines.append("")

    body = "\n".join(lines).rstrip() + "\n"
    body = sanitize(body)
    return truncate_body(body, max_chars)


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def publish_stage_report(
    *,
    owner: str,
    repo: str,
    target_number: str,
    run_id: str,
    item: str,
    stage: str,
    agent_identity: str,
    status: str,
    publish_comment_fn: Callable[..., Dict[str, Any]],
    get_comment_body_fn: Optional[Callable[..., Optional[str]]] = None,
    runner: Optional[Callable] = None,
    timeout: int = 20,
    **render_kwargs: Any,
) -> Dict[str, Any]:
    """Publish (create-or-update, idempotent) one stage-report comment, then re-query and
    confirm the observed body hash -- same discipline as
    `github_lifecycle.publish_lifecycle_state`.

    `publish_comment_fn` is injected (typically `scripts.pr_evidence.publish_comment`) so this
    module never hard-imports `scripts/` and so tests can supply a fully in-memory fake. It is
    called with `marker=build_marker(run_id, item)` -- the CRITICAL wiring point: that marker
    is what makes `publish_comment_fn`'s own `find_existing_comment` query-before-decide
    (create vs. update) actually govern this specific work item's comment, not a
    unit-tested-only side helper. `get_comment_body_fn`, when given, re-fetches the comment by
    id after publish (typically `github_lifecycle._get_comment_body`) to confirm the observed
    hash matches what was sent; without it the receipt is still returned but `verified` stays
    `False` (never fabricated as verified without a re-query).

    Returns a `simplicio.github-stage-report/v1` receipt (never raises for an ordinary
    publish/verify mismatch — reports `verified: False` instead — but a transport-level
    error from `publish_comment_fn`, e.g. `PublishError`, propagates fail-closed).
    """
    marker = build_marker(run_id, item)
    body = render_stage_report(
        run_id=run_id, item=item, stage=stage, agent_identity=agent_identity, status=status,
        **render_kwargs,
    )
    expected_hash = content_hash(body)

    kwargs: Dict[str, Any] = {"marker": marker, "timeout": timeout}
    if runner is not None:
        kwargs["runner"] = runner
    result = publish_comment_fn(owner, repo, str(target_number), body, **kwargs)
    comment_id = result.get("id")

    observed_hash = ""
    if get_comment_body_fn is not None and comment_id is not None:
        gb_kwargs: Dict[str, Any] = {}
        observed_body = get_comment_body_fn(owner, repo, comment_id, runner, timeout) \
            if runner is not None else get_comment_body_fn(owner, repo, comment_id, None, timeout)
        observed_hash = content_hash(observed_body) if observed_body is not None else ""
    verified = comment_id is not None and bool(observed_hash) and observed_hash == expected_hash

    return {
        "schema": STAGE_REPORT_SCHEMA,
        "run_id": run_id,
        "item": item,
        "stage": stage,
        "status": status,
        "marker": marker,
        "target_repo": "%s/%s" % (owner, repo),
        "target_number": str(target_number),
        "comment_id": comment_id,
        "action": result.get("action"),
        "expected_body_hash": expected_hash,
        "observed_body_hash": observed_hash,
        "verified": verified,
        "outcome": (result.get("action") or "blocked") if (comment_id is not None) else "blocked",
    }
