# FORCE Simplicio Runtime MCP — all LLMs, all hosts

**Goal:** token economy + full tool surface. The Simplicio Runtime MCP
(`simplicio serve --mcp --stdio`, tools `simplicio_*` / `simplicio__*`) is the
**governed substrate**. Host-native raw file dumps and unscoped greps are the
expensive path — **do not use them as default**.

Applies to: Claude, Codex, Grok, Cursor, VS Code, Antigravity, Kiro, Hermes,
and any host with this rule installed.

## Env (mechanical floor)

```bash
export SIMPLICIO_REQUIRE_MCP=1          # MCP required when Runtime is operational
export SIMPLICIO_MCP_FORCE=1            # same: force MCP-first tool use
export SIMPLICIO_LOOP=1
export SIMPLICIO_LOOP_STRICT=1
export SIMPLICIO_LOOP_FORBID_HAND_EDIT=1
export SIMPLICIO_LOOP_REQUIRE_RUNTIME=auto
export SIMPLICIO_FAST_MODE=required
```

When `SIMPLICIO_REQUIRE_MCP=1` **and** Runtime MCP is healthy, the agent **must**
prefer Runtime MCP tools before host Read/Grep/Bash cat of source trees.

## MUST — tool order (economy)

| Intent | Use first (MCP) | Avoid as primary |
|--------|-----------------|------------------|
| Orient repo | `simplicio_map` | reading dozens of full files |
| Find symbols | `simplicio_search` / `simplicio_symbol` | unbounded `rg` of whole tree into context |
| Recall decisions | `simplicio_memory` | re-deriving from chat |
| Risk check | `simplicio_gate` | mutating without gate |
| Mechanical edit | `simplicio_edit` | host Write of whole files when plan fits edit |
| Validate | `simplicio_validate` | inventing pass without gate |
| Claims | `simplicio_claims` | bare MEASURED without check |
| Passthrough CLI | `simplicio_exec` / `simplicio_run` (gated) | raw destructive shell |

**Loop operators still apply:** mapper survey + fast hot path + dev-cli for mutations
under STRICT. MCP Runtime is the **token-saving read/orient/memory/gate** layer;
dev-cli/fast remain the **mutation** path under STRICT.

## MUST NOT

- Flood context with full source trees when `simplicio_map` / signatures exist
- Bypass MCP when it is registered and healthy just to "go faster"
- Treat Orca or other host side-channels as default (client opt-in only)

## Host wiring

```bash
# register MCP into Claude/Codex/Cursor/VS Code/Kiro/…
simplicio mcp register
# or from loop:
python3 scripts/mcp_force_sync.py --global --json
```

Restart the IDE/agent after registration so tools appear.

## Degraded mode

If Runtime/MCP is **down**, report explicitly (`UNVERIFIED|mcp_unavailable`) and fall
back to mapper/dev-cli CLI — never pretend MCP tools ran. `SIMPLICIO_REQUIRE_MCP=1`
makes preflight/doctor warn or block when Runtime was expected operational.

## Related

- `docs/MCP_SETUP.md`
- `docs/MULTI_LLM_CONTRACT.md`
- `packaging/host-rules/simplicio-loop-operator-flow.md`
- Runtime: `INSTALL_MCP.md` / `simplicio serve --mcp --stdio`
