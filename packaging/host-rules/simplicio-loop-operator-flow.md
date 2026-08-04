# Simplicio loop + Fast — mandatory operator flow (all hosts)

**Applies to:** Claude Code, Codex, Grok, Cursor, VS Code / Copilot, Antigravity,
Kiro, Hermes / Simplicio Agent, OpenCode, Gemini, Aider, and any host that loads
Simplicio skills. **Orca is not default** — enable only when the client requests it
(`docs/CLIENT_INTEGRATIONS.md`).

Installers copy this file into each host's always-on surface via
`python3 scripts/host_rule_sync.py --global`.

## MUST

0. **Loop lives inside Runtime. Runtime decides when to use it.**
   - Entry: `simplicio loop decide --task "..." --json` (or action-bridge / run spine).
   - Honor `.simplicio/runtime/loop-decision.json` (`use_loop`, `verdict`, `host_may_override=false`).
   - Do **not** start `/simplicio-loop` as a peer path that bypasses Runtime activation.
   - **Operators alone OK:** mapper, dev-cli, fast work without Runtime (ADR 0009 / 04b).
   - **Metrics mandatory:** every run writes `simplicio.execution-report/v1`
     (per task/issue + consolidated: speed, latency, CPU/RAM when MEASURED, tokens in/out).
     CLI: `simplicio execution-report …` or `python -m simplicio_loop.execution_report …`.
     Never invent numbers (ADR 0010 / ADR-2026-08-05).
   - ADRs: runtime `ADR-2026-08-04`, `04b`, `05`; loop `docs/adr/0009`, `0010`.

1. **Economy-parallel env** before autonomous work (fastest tokens + parallel):
   ```bash
   simplicio-loop economy apply --json   # or: source ~/.simplicio/economy-parallel-env.sh
   ```
   - `SIMPLICIO_LOOP=1` · `SIMPLICIO_LOOP_STRICT=1`
   - `SIMPLICIO_LOOP_REQUIRE_RUNTIME=auto` (**preferred: Runtime present**; degraded only if missing)
   - `SIMPLICIO_EXECUTION_PROFILE=auto` → **`runtime-backed` when Runtime is up** (canonical)
   - `SIMPLICIO_FAST_MODE=required` (when Fast operational)
   - `SIMPLICIO_LOOP_AUTO_FAN_OUT=1` (parallel worktrees on `batch`)
   - `SIMPLICIO_LOOP_OPERATOR_WORKERS` / `SIMPLICIO_PRISM_SLOTS` / `SIMPLICIO_ASYNC_IO_MAX_CONCURRENCY` (CPU-bounded)
   - `SIMPLICIO_OPERATOR_ALWAYS_LATEST=1`
   - `SIMPLICIO_REQUIRE_MCP=1` / `SIMPLICIO_MCP_FORCE=1` (**when** Runtime present)
   - Safety: mutation authority + planning receipt + forbid hand-edit
   - Opt out of economy defaults: `SIMPLICIO_ECONOMY_PARALLEL=0`

2. **Preflight (blocking):** `simplicio-loop preflight --strict --json`  
   Core operators = **Runtime (owns loop) + mapper + dev-cli** (+ Fast when up).
   When Runtime MCP is registered: prefer `simplicio_map` / `search` / `memory` / `gate` / `edit`
   and **`simplicio loop decide`** over host bulk Read/Grep/cat.
   Without Runtime: report `UNVERIFIED|runtime_unavailable` — degraded, not preferred.
   Wire MCP: `python3 scripts/mcp_force_sync.py --global` · `simplicio mcp register`.

3. **Survey:** `simplicio-mapper` (scan / inspect / handoff) — not ad-hoc full-tree LLM walks.

4. **Hot path:** `simplicio-fast` when operational (understand / plan / apply / mmap).

5. **Mutate:** `simplicio-dev-cli` / `simplicio-py task` (or Fast apply) under STRICT.  
   Host Write / Edit / StrReplace / ApplyPatch are **forbidden** as the primary mutation path
   when STRICT is on (`hooks/action_gate.py` PreToolUse on Claude/Cursor; instruction law on
   self-paced hosts).

6. **GitHub** = default coordination SoT for Issues/PRs when the remote is GitHub.

7. **Drain:** claim → real ACs → PR to main with `Closes #N` → merge. Prefer Prism waves
   (`python3 scripts/arm_drain_prism.py --repo . --slots 4`) when draining a queue.

8. **Evidence-gated exit:** MEASURED tags; no theater AC stubs; no false completion.

9. **Parallelism** only with lease/claim + isolation + reducer — no double-writers without coordination.

10. **Integrations** (Orca, Linear, …) only if the **client requested** them
    (`SIMPLICIO_LOOP_CLIENT_INTEGRATIONS` or `.simplicio/client-integrations.json`).

## MUST NOT

- Mass-close issues with hardcoded `status=implemented` stubs
- Invent `open=0` without live issue re-query
- Reinstall operators every turn (TTL pin + `operator_check maybe-upgrade`)
- Hand-edit under STRICT to "go faster"
- Auto-enable Orca or other host side-channels

## Loop drive by host

| Host | Drive | Hand-edit gate |
|------|--------|----------------|
| Claude Code | Stop + PreToolUse | `action_gate` under STRICT |
| Cursor | stop + afterAgentResponse | hooks + this rule |
| Codex / Grok / VS Code / Antigravity / Kiro / Hermes | **self-paced** | this rule + env STRICT as hard law |
| Simplicio Agent | native tick | native + this rule |

Self-paced: re-read `.simplicio/orchestrator/loop/scratchpad.md` every turn;
triage → decide → operate → verify → journal.

## Invoke

```text
/simplicio-loop <body of work>
python3 scripts/arm_drain_prism.py --repo . --slots 4 --json
```

Protocol: `.claude/skills/simplicio-loop/SKILL.md`  
Contract: `docs/MULTI_LLM_CONTRACT.md`

<!-- simplicio-global-llm-architecture-rules:start -->
## Regras arquiteturais globais (obrigatórias)

- Delete diretamente o obsoleto; não preserve compatibilidade retroativa, não crie
  migrações e não deixe fallbacks legados.
- Escolha a solução mais simples para a necessidade atual, sem abstrações
  preventivas ou configuração desnecessária.
- Faça o mínimo end-to-end funcionar primeiro e evolua por camadas longas, sem
  desmontar o que funciona por complexidade inacabada.
- Mantenha modularidade e separação de responsabilidades.
- Prefira bibliotecas maduras e mantidas; reescreva do zero apenas com motivo
  técnico explícito.
- Inspecione dependências existentes antes de adicionar pacotes ou reimplementar.
- Tome decisões para o longo prazo; não deixe soluções temporárias.
- Reutilize padrões validados por produtos maduros; não reinvente a roda.

<!-- simplicio-global-llm-architecture-rules:end -->

