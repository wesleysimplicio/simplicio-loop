# FORCE Simplicio Runtime MCP — when Runtime is present (all hosts)

**Goal:** token economy + full tool surface **when Runtime MCP is available**.

**Critical:** `simplicio-runtime` is **NOT mandatory** for `simplicio-loop`.
Core loop = **mapper + dev-cli** (and **Fast** when operational). Without Runtime,
the standalone loop continues; report `UNVERIFIED|mcp_unavailable` / degraded
`runtime-integration` — never pretend MCP ran and never block the loop solely
because Runtime is missing.

When Runtime **is** installed and healthy (`simplicio` on PATH, MCP registered),
agents **must** prefer Runtime MCP tools over bulk host Read/Grep/cat of source.

Applies to: Claude, Codex, Grok, Cursor, VS Code, Antigravity, Kiro, Hermes,
and any host with this rule installed.

## Env (mechanical floor)

```bash
export SIMPLICIO_LOOP_REQUIRE_RUNTIME=auto   # adaptive — NOT required
export SIMPLICIO_EXECUTION_PROFILE=auto      # runtime-backed only if Runtime up
export SIMPLICIO_REQUIRE_MCP=1              # MCP-first WHEN Runtime present
export SIMPLICIO_MCP_FORCE=1                # same intent
export SIMPLICIO_LOOP=1
export SIMPLICIO_LOOP_STRICT=1
export SIMPLICIO_LOOP_FORBID_HAND_EDIT=1
export SIMPLICIO_FAST_MODE=required         # only binds Fast if operational
```

`SIMPLICIO_REQUIRE_MCP=1` does **not** install or require Runtime. The action_gate
only blocks host bulk-read paths when the flag is set **and** `simplicio` is on PATH.

## MUST — tool order (economy, Runtime present)

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

**Loop operators always apply (Runtime optional):** mapper survey + fast hot path +
dev-cli for mutations under STRICT. MCP Runtime is the **token-saving**
read/orient/memory/gate layer **when present**.

## MUST NOT

- Treat Runtime as a hard dependency of simplicio-loop
- Flood context with full source trees when `simplicio_map` / signatures exist (and Runtime up)
- Bypass MCP when it is registered and healthy just to "go faster"
- Fake MCP use when Runtime is down
- Treat Orca or other host side-channels as default (client opt-in only)

## Host wiring (optional install)

```bash
# only if you want Runtime MCP economy tools:
simplicio mcp register
# or from loop:
python3 scripts/mcp_force_sync.py --global --json
```

Restart the IDE/agent after registration so tools appear.

## Degraded mode (no Runtime)

If Runtime/MCP is **down or absent**:

1. Report explicitly (`UNVERIFIED|mcp_unavailable` / degraded `runtime-integration`)
2. Continue with **mapper / fast / dev-cli** — full standalone loop
3. Host Read/Grep remain allowed (gate does not force MCP without Runtime)

## Related

- `docs/MCP_SETUP.md`
- `docs/MULTI_LLM_CONTRACT.md`
- `packaging/host-rules/simplicio-loop-operator-flow.md`
- Runtime (optional): `INSTALL_MCP.md` / `simplicio serve --mcp --stdio`
