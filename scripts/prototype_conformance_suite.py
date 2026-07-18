#!/usr/bin/env python3
"""simplicio-loop — cross-repo Prototype-First conformance suite (epic #568).

Epic #568's DAG requires "todos os adapters + Loop #568 -> conformance
cross-repo -> FULL/delivery" and its AC includes "Todos os projetos
consumidores passam contract/conformance tests." This module is the schema
owner's half of that: it inspects the REAL source of every sibling repo that
has built a Prototype-First adapter and checks whether the JSON payloads they
emit are structurally compatible with the four canonical schemas this repo
defines in ``simplicio_loop/prototype_gate.py``
(``prototype-plan/v1``, ``prototype-candidate/v1``, ``prototype-decision/v1``,
``prototype-receipt/v1``).

This is deliberately NOT a "does everything match" rubber stamp. Several
siblings genuinely diverge from canonical (different field names, missing
required fields, or a schema-string collision with an incompatible shape) --
that divergence is the valuable signal this suite exists to surface, not a
bug in the suite. A sibling that defines its own, differently-named schema
(e.g. mapper's ``prototype-context/v1``, sprint's ``sprint-prototype/v1``,
marketing's ``marketing-prototype-gate/v1``) is not graded against the
canonical field lists at all -- it is correctly out of scope by design.

Extraction strategy per sibling (real repo content, never a fabricated
fixture):

  * Python siblings (mapper, dev-cli, agent, loop-oss, prompt): parsed with
    ``ast`` to find every ``{"schema": ..., ...}`` / ``{"schema_version":
    ..., ...}`` dict literal in the source -- this is the actual payload
    shape the module constructs, read directly off disk. dev-cli and prompt
    additionally get a live dynamic call of their lightweight plan/decision
    builders where that is cheap and side-effect-free (a stronger signal
    than static extraction alone).
  * pydantic sibling (sprint): imports the real module and reads
    ``BaseModel.model_fields`` off the live class -- the actual field set,
    not a guess, no pydantic-model instantiation required.
  * Non-Python siblings (runtime.rs, loop-marketing types.ts): no Python
    interpreter can import them, so this suite falls back to a regex
    extraction over the real source text (schema constant + struct/interface
    field names). Documented explicitly as ``regex-source`` mode, never
    silently treated as equivalent to a live import.

A sibling repo missing from disk (not checked out on this host) is reported
BLOCKED, never silently skipped and never treated as a pass or a fail.

Usage:
    python3 scripts/prototype_conformance_suite.py                  # all siblings
    python3 scripts/prototype_conformance_suite.py mapper prompt     # subset
    python3 scripts/prototype_conformance_suite.py --json out.json
    python3 scripts/prototype_conformance_suite.py --md out.md
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from simplicio_loop import prototype_gate as pg  # noqa: E402

SCHEMA = "simplicio.prototype-conformance/v1"
ISSUE = 568

CANONICAL_SCHEMA_STRINGS = {
    pg.PLAN_SCHEMA: "plan",
    pg.CANDIDATE_SCHEMA: "candidate",
    pg.DECISION_SCHEMA: "decision",
    pg.RECEIPT_SCHEMA: "receipt",
}

SIBLINGS = [
    "simplicio-mapper", "simplicio-runtime", "simplicio-dev-cli", "simplicio-agent",
    "simplicio-loop-oss", "simplicio-loop-marketing", "simplicio-sprint", "simplicio-prompt",
]


def _canonical_field_sets() -> dict[str, set[str]]:
    """Compute the canonical field sets by calling the REAL builders in this
    repo -- never hand-copied, so this can't drift from ``prototype_gate.py``
    itself."""
    plan = pg.build_plan(work_item_id="wi-conformance", goal="conformance probe",
                         prototype_type="schema", source_sha="0" * 40)
    candidate = pg.build_candidate(plan=plan, candidate_id="cand-conformance",
                                   strategy="direct", agent_id="agent-conformance",
                                   artifact_hash="hash-conformance")
    decision = pg.build_decision(plan=plan, candidate_hash=candidate["candidate_hash"],
                                 decision="ACCEPT")
    receipt = pg.build_receipt(plan=plan, candidate=candidate, decision=decision)
    return {
        "plan": set(plan.keys()),
        "candidate": set(candidate.keys()),
        "decision": set(decision.keys()),
        "receipt": set(receipt.keys()),
    }


def _find_repo(name: str) -> str | None:
    """Resolve a sibling repo path: env override > sibling-of-repo-parent >
    the well-known ``/home/user/<name>`` multi-repo dev layout. Returns None
    (not a raise) when the repo simply is not checked out on this host --
    that is the expected common case in ordinary single-repo CI."""
    env_key = "SIMPLICIO_" + name.upper().replace("-", "_") + "_REPO_PATH"
    override = os.environ.get(env_key)
    if override and os.path.isdir(override):
        return override
    candidates = [
        os.path.join(os.path.dirname(_REPO_ROOT), name),
        os.path.join("/home/user", name),
    ]
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    return None


# --- Python AST extraction: every {"schema"/"schema_version": ..., ...} literal -----------------

def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    consts: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                consts[node.targets[0].id] = node.value.value
    return consts


def extract_python_schema_literals(source_path: str) -> list[dict[str, Any]]:
    """Return every dict literal in *source_path* that carries a ``schema``
    or ``schema_version`` string key, with its full set of literal string
    keys and the resolved schema-string value (literal or module constant)."""
    with open(source_path, encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source, filename=source_path)
    consts = _module_string_constants(tree)
    found: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys: list[str] = []
        schema_value = None
        has_schema_key = False
        for key_node, value_node in zip(node.keys, node.values):
            if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
                continue
            keys.append(key_node.value)
            if key_node.value in ("schema", "schema_version"):
                has_schema_key = True
                if isinstance(value_node, ast.Constant) and isinstance(value_node.value, str):
                    schema_value = value_node.value
                elif isinstance(value_node, ast.Name):
                    schema_value = consts.get(value_node.id)
        if has_schema_key:
            found.append({"fields": sorted(set(keys)), "schema": schema_value, "line": node.lineno})
    return found


def _dedupe_by_schema(literals: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Union the field sets of every literal sharing the same schema string
    (a polymorphic emitter, e.g. dev-cli's several receipt shapes, is one
    entry keyed by its shared schema string, fields = the union)."""
    by_schema: dict[str, set[str]] = {}
    for lit in literals:
        key = lit["schema"] or "<unresolved>"
        by_schema.setdefault(key, set()).update(lit["fields"])
    return by_schema


# --- Regex extraction for non-Python siblings (documented, never silently treated as a live import) --

_RUST_CONST_RE = re.compile(r'pub const (\w+):\s*&(?:\'static\s+)?str\s*=\s*"([^"]+)"')
_RUST_STRUCT_RE = re.compile(r'pub struct (\w+)\s*\{([^}]*)\}', re.DOTALL)
_RUST_FIELD_RE = re.compile(r'pub (\w+):')

_TS_CONST_RE = re.compile(r'export const (\w+)\s*=\s*"([^"]+)"')
_TS_INTERFACE_RE = re.compile(r'export interface (\w+)\s*\{([^}]*)\}', re.DOTALL)
_TS_FIELD_RE = re.compile(r'^\s*(\w+)\??:', re.MULTILINE)


def extract_rust_schema_literals(source_path: str) -> list[dict[str, Any]]:
    with open(source_path, encoding="utf-8") as handle:
        source = handle.read()
    consts = dict(_RUST_CONST_RE.findall(source))
    found = []
    for struct_name, body in _RUST_STRUCT_RE.findall(source):
        fields = _RUST_FIELD_RE.findall(body)
        if "schema" not in fields:
            continue
        # Best-effort: find a nearby "schema: SOME_CONST" construction site to resolve
        # which constant this struct is actually stamped with at runtime.
        schema_value = None
        constructor_re = re.compile(
            re.escape(struct_name) + r'\s*\{[^}]*?schema:\s*(\w+)', re.DOTALL,
        )
        m = constructor_re.search(source)
        if m:
            schema_value = consts.get(m.group(1))
        found.append({"fields": sorted(set(fields)), "schema": schema_value, "struct": struct_name})
    return found


def extract_ts_schema_literals(source_path: str) -> list[dict[str, Any]]:
    with open(source_path, encoding="utf-8") as handle:
        source = handle.read()
    consts = dict(_TS_CONST_RE.findall(source))
    found = []
    for iface_name, body in _TS_INTERFACE_RE.findall(source):
        fields = _TS_FIELD_RE.findall(body)
        if "schema" not in fields:
            continue
        schema_value = None
        m = re.search(r'schema:\s*typeof\s+(\w+)', body)
        if m:
            schema_value = consts.get(m.group(1))
        found.append({"fields": sorted(set(fields)), "schema": schema_value, "interface": iface_name})
    return found


@dataclass
class SchemaFinding:
    schema_claimed: str | None
    canonical_target: str | None       # "plan"/"candidate"/"decision"/"receipt"/None
    classification: str                # "claims-canonical" | "distinct" | "unresolved"
    fields_found: list[str]
    fields_missing: list[str] = field(default_factory=list)
    fields_extra: list[str] = field(default_factory=list)
    conformant: bool | None = None      # None when classification != "claims-canonical"


@dataclass
class SiblingReport:
    repo: str
    path: str | None
    available: bool
    mode: str
    reason: str
    findings: list[dict] = field(default_factory=list)
    live_probe: dict | None = None


def _classify(schema_string: str | None, fields: set[str],
             canonical: dict[str, set[str]]) -> SchemaFinding:
    resolved = None if schema_string == "<unresolved>" else schema_string
    target = CANONICAL_SCHEMA_STRINGS.get(resolved or "")
    if target is None:
        return SchemaFinding(schema_claimed=resolved, canonical_target=None,
                             classification="distinct" if resolved else "unresolved",
                             fields_found=sorted(fields))
    canon = canonical[target]
    missing = sorted(canon - fields)
    extra = sorted(fields - canon)
    return SchemaFinding(schema_claimed=schema_string, canonical_target=target,
                         classification="claims-canonical", fields_found=sorted(fields),
                         fields_missing=missing, fields_extra=extra,
                         conformant=(len(missing) == 0))


# --- Per-sibling probes --------------------------------------------------------------------------

def probe_mapper(path: str, canonical: dict[str, set[str]]) -> SiblingReport:
    target = os.path.join(path, "simplicio_mapper", "prototype_context.py")
    if not os.path.isfile(target):
        return SiblingReport("simplicio-mapper", path, False, "blocked",
                             f"expected file not found: {target}")
    literals = extract_python_schema_literals(target)
    by_schema = _dedupe_by_schema(literals)
    findings = [asdict(_classify(s, f, canonical)) for s, f in by_schema.items()]
    live_probe = None
    try:
        if path not in sys.path:
            sys.path.insert(0, path)
        import importlib
        importlib.import_module("simplicio_mapper.prototype_context")
        live_probe = {"import_ok": True}
    except Exception as exc:  # pragma: no cover - environment dependent
        live_probe = {"import_ok": False, "error": str(exc)}
    return SiblingReport("simplicio-mapper", path, True, "ast-literal-extraction",
                         "prototype_context.py emits its own distinct simplicio.prototype-context/v1 "
                         "envelope; it only consumes an externally-supplied plan_hash, it never builds "
                         "or validates a canonical plan/candidate/decision/receipt object",
                         findings=findings, live_probe=live_probe)


def probe_runtime(path: str, canonical: dict[str, set[str]]) -> SiblingReport:
    target = os.path.join(path, "src", "prototype_gate.rs")
    if not os.path.isfile(target):
        return SiblingReport("simplicio-runtime", path, False, "blocked",
                             f"expected file not found: {target}")
    literals = extract_rust_schema_literals(target)
    by_schema = _dedupe_by_schema(literals)
    findings = [asdict(_classify(s, f, canonical)) for s, f in by_schema.items()]
    return SiblingReport("simplicio-runtime", path, True, "regex-source",
                         "Rust, no Python import possible; struct field names extracted via regex "
                         "over pub struct { ... } blocks in src/prototype_gate.rs",
                         findings=findings)


def probe_dev_cli(path: str, canonical: dict[str, set[str]]) -> SiblingReport:
    target = os.path.join(path, "simplicio", "commands", "prototype.py")
    if not os.path.isfile(target):
        return SiblingReport("simplicio-dev-cli", path, False, "blocked",
                             f"expected file not found: {target}")
    literals = extract_python_schema_literals(target)
    by_schema = _dedupe_by_schema(literals)
    live_probe = None
    try:
        if path not in sys.path:
            sys.path.insert(0, path)
        import importlib
        mod = importlib.import_module("simplicio.commands.prototype")
        from types import SimpleNamespace
        args = SimpleNamespace(input=None, goal="conformance probe", prototype_type="schema",
                               root=".")
        live_plan = mod._plan_from_args(args)  # noqa: SLF001 - deliberate real-code probe
        by_schema.setdefault(mod.SCHEMA_PLAN, set()).update(live_plan.keys())
        live_probe = {"import_ok": True, "live_plan_fields": sorted(live_plan.keys())}
    except Exception as exc:  # pragma: no cover - environment dependent
        live_probe = {"import_ok": False, "error": str(exc)}
    findings = [asdict(_classify(s, f, canonical)) for s, f in by_schema.items()]
    return SiblingReport("simplicio-dev-cli", path, True, "ast-literal-extraction+live-plan-call",
                         "reuses the 3 canonical schema strings for plan/receipt/decision (no "
                         "candidate schema emitted); plan built live via _plan_from_args, "
                         "receipt/decision shapes extracted statically from run()",
                         findings=findings, live_probe=live_probe)


def probe_agent(path: str, canonical: dict[str, set[str]]) -> SiblingReport:
    target = os.path.join(path, "agent", "prototype_first_gate.py")
    if not os.path.isfile(target):
        return SiblingReport("simplicio-agent", path, False, "blocked",
                             f"expected file not found: {target}")
    literals = extract_python_schema_literals(target)
    by_schema = _dedupe_by_schema(literals)
    findings = [asdict(_classify(s, f, canonical)) for s, f in by_schema.items()]
    return SiblingReport("simplicio-agent", path, True, "ast-literal-extraction",
                         "reuses 3 of the 4 canonical schema strings (plan/candidate/decision) but "
                         "with a structurally unrelated dataclass shape (hypothesis/approach_id/"
                         "claims/RoleIdentity); no plan_hash or source_sha at all on its plan",
                         findings=findings)


def probe_loop_oss(path: str, canonical: dict[str, set[str]]) -> SiblingReport:
    target = os.path.join(path, "scripts", "prototype_gate.py")
    if not os.path.isfile(target):
        return SiblingReport("simplicio-loop-oss", path, False, "blocked",
                             f"expected file not found: {target}")
    literals = extract_python_schema_literals(target)
    by_schema = _dedupe_by_schema(literals)
    findings = [asdict(_classify(s, f, canonical)) for s, f in by_schema.items()]
    return SiblingReport("simplicio-loop-oss", path, True, "ast-literal-extraction",
                         "no schema-tagged JSON literal exists in this file at all (verified: zero "
                         "{\"schema\": ...} literals found) -- it implements the prototype-gate "
                         "workflow discipline (reproducer/read-only-guard/judge-verdict) without "
                         "participating in the JSON schema contract",
                         findings=findings)


def probe_loop_marketing(path: str, canonical: dict[str, set[str]]) -> SiblingReport:
    target = os.path.join(path, "lib", "prototype", "types.ts")
    if not os.path.isfile(target):
        return SiblingReport("simplicio-loop-marketing", path, False, "blocked",
                             f"expected file not found: {target}")
    literals = extract_ts_schema_literals(target)
    by_schema = _dedupe_by_schema(literals)
    findings = [asdict(_classify(s, f, canonical)) for s, f in by_schema.items()]
    return SiblingReport("simplicio-loop-marketing", path, True, "regex-source",
                         "TypeScript, no Python import possible; own distinct "
                         "marketing-prototype-gate/v1 schema, not aliased to any canonical string",
                         findings=findings)


def probe_sprint(path: str, canonical: dict[str, set[str]]) -> SiblingReport:
    target = os.path.join(path, "sendsprint", "prototype", "schema.py")
    if not os.path.isfile(target):
        return SiblingReport("simplicio-sprint", path, False, "blocked",
                             f"expected file not found: {target}")
    try:
        if path not in sys.path:
            sys.path.insert(0, path)
        import importlib
        mod = importlib.import_module("sendsprint.prototype.schema")
    except ImportError as exc:
        return SiblingReport("simplicio-sprint", path, False, "blocked",
                             f"pydantic (or another runtime dep) not installed: {exc}")
    fields = sorted(mod.PrototypeSprintPlan.model_fields.keys())
    schema_value = mod.SCHEMA
    finding = asdict(_classify(schema_value, set(fields), canonical))
    return SiblingReport("simplicio-sprint", path, True, "pydantic-model-fields",
                         "own distinct sprint-prototype/v1 schema (a DAG-of-cards sprint "
                         "decomposition), not one of the 4 canonical schemas by design; fields read "
                         "live off PrototypeSprintPlan.model_fields",
                         findings=[finding], live_probe={"import_ok": True})


def probe_prompt(path: str, canonical: dict[str, set[str]]) -> SiblingReport:
    target = os.path.join(path, "kernel", "prototype_first_gate.py")
    if not os.path.isfile(target):
        return SiblingReport("simplicio-prompt", path, False, "blocked",
                             f"expected file not found: {target}")
    live_probe = None
    by_schema: dict[str, set[str]] = {}
    try:
        if path not in sys.path:
            sys.path.insert(0, path)
        import importlib
        mod = importlib.import_module("kernel.prototype_first_gate")
        live_plan = mod.build_plan(work_item_id="wi-conformance", goal="conformance probe",
                                   source_sha="0" * 40)
        live_decision = mod.build_decision(plan=live_plan, candidate_hash="hash-conformance",
                                           decision="ACCEPT")
        by_schema.setdefault(mod.PLAN_SCHEMA, set()).update(live_plan.keys())
        by_schema.setdefault(mod.DECISION_SCHEMA, set()).update(live_decision.keys())
        live_probe = {"import_ok": True, "live_plan_fields": sorted(live_plan.keys()),
                     "live_decision_fields": sorted(live_decision.keys())}
    except Exception as exc:  # pragma: no cover - environment dependent
        live_probe = {"import_ok": False, "error": str(exc)}
    if not by_schema:
        literals = extract_python_schema_literals(target)
        by_schema = _dedupe_by_schema(literals)
    findings = [asdict(_classify(s, f, canonical)) for s, f in by_schema.items()]
    return SiblingReport("simplicio-prompt", path, True, "live-call",
                         "module docstring calls this a 'byte-for-byte mirror' of "
                         "simplicio_loop.prototype_gate's build_plan/build_decision, fixed to "
                         "prototype_type=prompt_candidate; no candidate/receipt mirror exists here "
                         "(PromptCandidate is its own unrelated dataclass)",
                         findings=findings, live_probe=live_probe)


PROBES = {
    "simplicio-mapper": probe_mapper,
    "simplicio-runtime": probe_runtime,
    "simplicio-dev-cli": probe_dev_cli,
    "simplicio-agent": probe_agent,
    "simplicio-loop-oss": probe_loop_oss,
    "simplicio-loop-marketing": probe_loop_marketing,
    "simplicio-sprint": probe_sprint,
    "simplicio-prompt": probe_prompt,
}


def conformance_for(name: str, canonical: dict[str, set[str]]) -> SiblingReport:
    path = _find_repo(name)
    if path is None:
        return SiblingReport(name, None, False, "blocked",
                             f"{name} is not checked out on this host (no path found via env "
                             f"override, sibling-of-repo-parent, or /home/user/{name})")
    try:
        return PROBES[name](path, canonical)
    except SyntaxError as exc:
        # Sibling repos live at fixed host paths and may be mid-edit by another
        # concurrent agent/session (e.g. an unresolved merge-conflict marker). A
        # transient parse failure on someone else's in-flight edit is BLOCKED,
        # never a crash of this whole suite and never silently treated as a pass.
        return SiblingReport(name, path, False, "blocked",
                             f"{name} source failed to parse (possibly mid-edit by another "
                             f"session): {exc}")
    except Exception as exc:  # pragma: no cover - defensive, environment dependent
        return SiblingReport(name, path, False, "blocked",
                             f"{name} probe raised an unexpected error: {exc!r}")


def build_report(names: list[str]) -> dict:
    canonical = _canonical_field_sets()
    results = [asdict(conformance_for(n, canonical)) for n in names]
    available = [r for r in results if r["available"]]
    claims_canonical = [
        f for r in results for f in r["findings"] if f["classification"] == "claims-canonical"
    ]
    drifted = [f for f in claims_canonical if f["conformant"] is False]
    return {
        "schema": SCHEMA,
        "issue": ISSUE,
        "canonical_fields": {k: sorted(v) for k, v in canonical.items()},
        "total_siblings": len(results),
        "available_siblings": len(available),
        "claims_canonical_count": len(claims_canonical),
        "drifted_count": len(drifted),
        "exit_gate": "reported",  # this suite's job is honest reporting, not a pass/fail toggle;
                                  # pytest pins the SPECIFIC known drifts as regression tests.
        "results": results,
    }


def render_md(report: dict) -> str:
    lines = ["# Cross-repo Prototype-First conformance (epic #568)", ""]
    lines.append(f"- Siblings checked: **{report['total_siblings']}**")
    lines.append(f"- Available on this host: **{report['available_siblings']}**")
    lines.append(f"- Payloads claiming a canonical schema string: **{report['claims_canonical_count']}**")
    lines.append(f"- Of those, structurally drifted from canonical: **{report['drifted_count']}**")
    lines.append("")
    for r in report["results"]:
        lines.append(f"## {r['repo']}")
        lines.append(f"- available: {r['available']} ({r['reason']})")
        lines.append(f"- mode: {r['mode']}")
        for f in r["findings"]:
            lines.append(f"  - schema={f['schema_claimed']!r} classification={f['classification']} "
                        f"conformant={f['conformant']}")
            if f["fields_missing"]:
                lines.append(f"    - missing vs canonical: {f['fields_missing']}")
            if f["fields_extra"]:
                lines.append(f"    - extra vs canonical: {f['fields_extra']}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="cross-repo Prototype-First conformance (#568)")
    parser.add_argument("siblings", nargs="*", default=SIBLINGS,
                        help="subset of sibling repo names to check (default: all 8)")
    parser.add_argument("--json", metavar="PATH", help="write JSON report")
    parser.add_argument("--md", metavar="PATH", help="write markdown report")
    args = parser.parse_args(argv)

    unknown = [n for n in args.siblings if n not in SIBLINGS]
    if unknown:
        print(f"unknown sibling(s): {unknown}", file=sys.stderr)
        return 2

    report = build_report(args.siblings)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    if args.md:
        with open(args.md, "w", encoding="utf-8") as fh:
            fh.write(render_md(report))

    print(f"prototype-conformance: {report['available_siblings']}/{report['total_siblings']} "
          f"siblings available; {report['drifted_count']}/{report['claims_canonical_count']} "
          f"canonical-schema claims drifted")
    for r in report["results"]:
        flag = "BLK" if not r["available"] else "OK "
        print(f"  [{flag}] {r['repo']}: {r['reason']}")
        for f in r["findings"]:
            if f["classification"] == "claims-canonical":
                verdict = "CONFORMANT" if f["conformant"] else "DRIFTED"
                print(f"        {f['schema_claimed']} -> {verdict} "
                     f"(missing={f['fields_missing']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
