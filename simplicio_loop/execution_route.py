"""``simplicio.execution-route/v1`` (issue #555): the agent-vs-worker decision receipt.

Issue #555 ("Contrato de decisao agent vs worker") asks for a deterministic router that
every job passes through *before* an LLM/agent is ever invoked, plus a receipt schema
(``route``, ``reason``, ``confidence``, ``cache_hit``, ``tokens_saved``, ``tokens_spent``,
``backend``, ``evidence``) proving the decision. This module is a small, real, in-repo
foundation for that contract -- not the cross-repo integration the epic describes (that
spans simplicio-runtime/mapper/dev-cli/agent/code, none of which are reachable from this
repo). It follows this codebase's existing receipt conventions
(``simplicio_loop/runtime_execution_receipt.py``, ``simplicio_loop/receipt_verifier.py``):
a frozen dataclass, a stable content hash, and an append-only JSONL journal
(``scripts/loop_journal.py``'s ``record`` pattern) rather than inventing a parallel shape.

The issue's own decision rule (Portuguese original, kept verbatim for traceability):

    worker sem LLM: mapear, buscar, deduplicar, aplicar edicao mecanica, testar, coletar
    CI, validar schema, calcular diff, publicar receipt;
    agent/LLM: interpretar objetivo ambiguo, sintetizar plano, produzir codigo/copy nao
    mecanico, investigar falha nova, revisao semantica e escolher recuperacao;
    hibrido: gates deterministicos primeiro; agent somente quando o resultado for
    inconclusivo ou exigir julgamento.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

SCHEMA = "simplicio.execution-route/v1"

ROUTES = frozenset(("worker", "agent", "hybrid"))

WORKER_KEYWORDS = (
    "map", "mapear", "mapping",
    "search", "buscar", "busca",
    "dedup", "deduplicar", "deduplicate",
    "mechanical-edit", "mechanical edit", "edicao mecanica", "edição mecânica",
    "test", "testar", "teste",
    "ci", "coletar ci",
    "schema-validate", "validar schema", "schema validation",
    "diff", "calcular diff",
    "receipt-publish", "publicar receipt", "publish receipt",
)

AGENT_KEYWORDS = (
    "ambiguous-goal", "ambiguous goal", "objetivo ambiguo", "objetivo ambíguo",
    "plan-synthesis", "sintetizar plano", "plan synthesis",
    "non-mechanical-code", "non-mechanical code", "codigo nao mecanico",
    "código não mecânico", "copy nao mecanico",
    "new-failure-investigation", "investigar falha nova", "new failure investigation",
    "semantic-review", "revisao semantica", "semantic review", "revisão semântica",
    "recovery-choice", "escolher recuperacao", "recovery choice", "escolher recuperação",
)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _stable_hash(data: Any) -> str:
    blob = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()

CAPABILITY_SCHEMA = "simplicio.execution-capabilities/v1"


def normalize_capability_manifest(value: Any) -> Any:
    """Return a stable, order-insensitive representation of capabilities."""
    if isinstance(value, Mapping):
        return {str(key): normalize_capability_manifest(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        normalized = [normalize_capability_manifest(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ))
    return value


def capability_fingerprint(value: Any) -> str:
    """Hash the effective capability manifest used to select an execution route."""
    return _stable_hash({
        "schema": CAPABILITY_SCHEMA,
        "capabilities": normalize_capability_manifest(value),
    })


def route_receipt_is_current(record: Mapping[str, Any], capability_manifest: Any) -> bool:
    """Return whether a verified receipt matches the current capability manifest."""
    return bool(record) and verify_route_hash(record) and (
        str(record.get("capability_fingerprint") or "") == capability_fingerprint(capability_manifest)
    )


class ExecutionRouteError(ValueError):
    """Raised for malformed route-building input."""


@dataclass(frozen=True)
class ExecutionRoute:
    """One ``simplicio.execution-route/v1`` decision receipt.

    ``route``: ``"worker"`` | ``"agent"`` | ``"hybrid"``.
    ``reason``: short human-readable justification for the decision.
    ``confidence``: ``0.0``..``1.0`` -- how confident the deterministic rule was.
    ``cache_hit``: whether an equivalent prior decision/result was reused instead of
        recomputed (single-flight / dedupe, per the issue's SLO list).
    ``tokens_saved`` / ``tokens_spent``: integers, ``0`` when genuinely zero (never a
        stand-in for "unmeasured" -- callers that cannot measure tokens should not build
        a receipt claiming ``0``).
    ``backend``: which concrete worker/agent backend executed (or would execute) the
        route, e.g. ``"simplicio-dev-cli"``, ``"llm"``, ``"unassigned"``.
    ``evidence``: paths/ids/descriptions of what backs the decision (matched keywords,
        prior receipt ids, gate results).
    """

    schema: str
    route: str
    reason: str
    confidence: float
    cache_hit: bool
    tokens_saved: int
    tokens_spent: int
    backend: str
    evidence: tuple
    measured_at: str
    receipt_sha: str = field(default="")

    def to_dict(self) -> Dict[str, Any]:
        return dict(asdict(self))


def _validate_common(route: str, confidence: float, tokens_saved: int, tokens_spent: int) -> None:
    if route not in ROUTES:
        raise ExecutionRouteError(f"route must be one of {sorted(ROUTES)}, got {route!r}")
    if not (0.0 <= confidence <= 1.0):
        raise ExecutionRouteError(f"confidence must be within [0.0, 1.0], got {confidence!r}")
    if tokens_saved < 0 or tokens_spent < 0:
        raise ExecutionRouteError("tokens_saved and tokens_spent must be >= 0")


def build_execution_route(
    *,
    route: str,
    reason: str,
    confidence: float,
    cache_hit: bool = False,
    tokens_saved: int = 0,
    tokens_spent: int = 0,
    backend: str = "unassigned",
    evidence: Sequence[str] = (),
) -> ExecutionRoute:
    """Build one execution-route receipt (never fabricates fields the caller omitted)."""
    _validate_common(route, confidence, tokens_saved, tokens_spent)
    reason = str(reason or "").strip()
    if not reason:
        raise ExecutionRouteError("reason is required")

    payload: Dict[str, Any] = {
        "schema": SCHEMA,
        "route": route,
        "reason": reason,
        "confidence": float(confidence),
        "cache_hit": bool(cache_hit),
        "tokens_saved": int(tokens_saved),
        "tokens_spent": int(tokens_spent),
        "backend": str(backend or "unassigned"),
        "evidence": tuple(str(e) for e in evidence),
        "measured_at": _now(),
    }
    receipt_sha = _stable_hash(payload)
    return ExecutionRoute(receipt_sha=receipt_sha, **payload)


def _match_keywords(task_description: str, keywords: Sequence[str]) -> list:
    lowered = task_description.lower()
    return [kw for kw in keywords if kw in lowered]


def decide_route(
    task_description: str,
    has_deterministic_worker: bool,
    is_ambiguous: bool,
) -> ExecutionRoute:
    """Deterministic agent-vs-worker router, per issue #555's own decision rule.

    Order of decision (deterministic, no LLM call):
      1. ``is_ambiguous=True`` always forces ``agent`` -- an ambiguous goal, plan
         synthesis, non-mechanical code/copy, a new failure investigation, semantic
         review, or a recovery choice can never be safely routed to a worker, even if
         the task description also happens to contain a worker keyword.
      2. Otherwise, if the description matches a known mechanical-task keyword AND a
         deterministic worker is actually available (``has_deterministic_worker``),
         route to ``worker``.
      3. Otherwise, if the description matches a known mechanical-task keyword but no
         deterministic worker is available, route to ``hybrid`` -- gates run first,
         agent only steps in because the worker path is inconclusive (missing tool),
         not because the task itself required judgment.
      4. Otherwise (no confident match either way) route to ``agent`` with low
         confidence -- an unrecognized task is judgment-shaped by default, never
         silently assumed mechanical.
    """
    task_description = str(task_description or "")
    agent_hits = _match_keywords(task_description, AGENT_KEYWORDS)
    worker_hits = _match_keywords(task_description, WORKER_KEYWORDS)

    if is_ambiguous:
        evidence = ["is_ambiguous=True"] + [f"matched:{kw}" for kw in agent_hits]
        return build_execution_route(
            route="agent",
            reason="goal marked ambiguous -- agent/LLM required per contract",
            confidence=0.95 if agent_hits else 0.7,
            backend="llm",
            evidence=evidence,
        )

    if worker_hits and has_deterministic_worker:
        evidence = [f"matched:{kw}" for kw in worker_hits] + ["has_deterministic_worker=True"]
        return build_execution_route(
            route="worker",
            reason="mechanical task with a deterministic worker available",
            confidence=0.9,
            backend="deterministic-worker",
            evidence=evidence,
        )

    if worker_hits and not has_deterministic_worker:
        evidence = [f"matched:{kw}" for kw in worker_hits] + ["has_deterministic_worker=False"]
        return build_execution_route(
            route="hybrid",
            reason="mechanical task but no deterministic worker available -- gate first, agent as fallback",
            confidence=0.6,
            backend="unassigned",
            evidence=evidence,
        )

    evidence = ["no keyword match"] + [f"matched:{kw}" for kw in agent_hits]
    return build_execution_route(
        route="agent",
        reason="no confident mechanical-task match -- default to agent judgment",
        confidence=0.4,
        backend="llm",
        evidence=evidence,
    )


def record_route(receipt: ExecutionRoute, path: str) -> Dict[str, Any]:
    """Append one execution-route receipt to a local JSONL journal.

    Mirrors ``scripts/loop_journal.py``'s append-only ``record`` convention: one JSON
    object per line, parent directories created on demand, never truncates or rewrites
    prior entries. Returns the exact dict written.
    """
    if not isinstance(receipt, ExecutionRoute):
        raise ExecutionRouteError("receipt must be an ExecutionRoute")
    record = receipt.to_dict()
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
        fh.write("\n")
    return record


def read_routes(path: str) -> list:
    """Read back all execution-route receipts from a journal path (empty list if absent)."""
    out_path = Path(path)
    if not out_path.is_file():
        return []
    records = []
    for line in out_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        records.append(json.loads(line))
    return records


def verify_route_hash(record: Mapping[str, Any]) -> bool:
    """Recompute the content hash of a read-back record and compare to its declared value."""
    declared = record.get("receipt_sha")
    if not declared:
        return False
    payload = {k: v for k, v in record.items() if k != "receipt_sha"}
    return _stable_hash(payload) == declared


__all__ = [
    "SCHEMA",
    "ROUTES",
    "WORKER_KEYWORDS",
    "AGENT_KEYWORDS",
    "ExecutionRoute",
    "ExecutionRouteError",
    "build_execution_route",
    "decide_route",
    "capability_fingerprint",
    "normalize_capability_manifest",
    "route_receipt_is_current",
    "record_route",
    "read_routes",
    "verify_route_hash",
]
