# Codex host surface

`hooks.json` is empty on purpose. Codex has no native PreToolUse in this
adapter. Enforcement is MCP + Loop self-paced watcher (`adapters/codex/adapter.py`).
Do not treat an empty hooks file as equivalent native interception.
