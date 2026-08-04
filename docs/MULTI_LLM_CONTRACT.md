# Multi-LLM contract — same floor on every host

## Why

Without a shared mechanical floor, each LLM (Claude, Codex, Grok, Cursor, VS Code, …)
improvises: hand-edits, skips preflight, serial drain, or host side-channels. This
contract + `host_rule_sync` + STRICT env make that a **contract violation**, not a style choice.

## Three layers

| Layer | Content | Enforcement |
|-------|---------|-------------|
| Safety floor | evidence before done; MEASURED/UNVERIFIED; no theater closes | protocol + journal |
| Operator path | mapper · fast · dev-cli | `preflight --strict` |
| Host surface | always-on rule file | `scripts/host_rule_sync.py` + entry blocks |

## Invariants

1. **Operators do; models decide.**
2. **STRICT forbids host hand-edit** as primary mutation path.
3. **Fast required when operational** at preflight.
4. **Loop lives inside Runtime.** Runtime owns activation (`simplicio loop decide`).
   Preferred profile is `runtime-backed` when `simplicio` is on PATH.
   `SIMPLICIO_LOOP_REQUIRE_RUNTIME=auto` (or `1` for hard require). Hosts **must not**
   start loop as a peer product path outside Runtime; they honor Runtime's decision receipt.
5. **When Runtime is present, force MCP tools for economy** — `SIMPLICIO_REQUIRE_MCP=1`
   prefers `simplicio_map` / `search` / `memory` / `gate` / `edit` over host bulk reads;
   action_gate only enforces this if `simplicio` is on PATH. Without Runtime: report
   `UNVERIFIED|runtime_unavailable` and degraded mode — not preferred architecture.
6. **Prism parallelism** for queue drain when armed (`arm_drain_prism.py`).
7. **Self-paced hosts are first-class** — no hooks ≠ optional protocol.
8. **Client integrations opt-in only** — Orca is never default (`docs/CLIENT_INTEGRATIONS.md`).

## Armada (before iteration 1)

```bash
# or: source ~/.simplicio/loop-env.sh  (after host_rule_sync --global)
export SIMPLICIO_LOOP=1 SIMPLICIO_LOOP_STRICT=1
export SIMPLICIO_LOOP_REQUIRE_RUNTIME=auto
export SIMPLICIO_REQUIRE_MUTATION_AUTHORITY=1
export SIMPLICIO_LOOP_AUTO_PLANNING_RECEIPT=1
export SIMPLICIO_LOOP_FORBID_HAND_EDIT=1
export SIMPLICIO_FAST_MODE=required
python3 scripts/host_rule_sync.py --global --json
python3 scripts/mcp_force_sync.py --global --json    # FORCE Runtime MCP into hosts + env
simplicio-loop preflight --strict --json
python3 scripts/arm_drain_prism.py --repo . --slots 4 --json   # when draining issues
```

## Install per host

```bash
python3 scripts/install_lib.py claude --global
python3 scripts/install_lib.py codex --global
python3 scripts/install_lib.py grok --global
python3 scripts/install_lib.py cursor --global
python3 scripts/install_lib.py vscode --global
python3 scripts/install_lib.py antigravity --global
python3 scripts/install_lib.py kiro --global
python3 scripts/install_lib.py hermes --global   # alias → simplicio_agent
# Orca ONLY if client contracted it:
# python3 scripts/install_lib.py orca
# export SIMPLICIO_LOOP_CLIENT_INTEGRATIONS=orca
```

## Related

- `packaging/host-rules/simplicio-loop-operator-flow.md`
- `scripts/host_rule_sync.py`
- `scripts/arm_drain_prism.py`
- `docs/CLIENT_INTEGRATIONS.md`
- `hooks/action_gate.py` (PreToolUse hand-edit block)
