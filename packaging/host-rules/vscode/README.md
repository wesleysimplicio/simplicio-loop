# VS Code / Copilot host rule

VS Code has no native PreToolUse. Bind MCP via `.vscode/mcp.json` and drive
the Loop self-paced. Unmanaged editor mutations are residual — never report
them as enforced. Adapter: `adapters/vscode/adapter.py`.
