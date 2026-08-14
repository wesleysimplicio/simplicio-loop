# Kiro host surface

Steering files under `.kiro/steering/` load the Loop protocol. This adapter
does not invent native PreToolUse hooks. Spec task state is not the Simplicio
source of truth — a spec cannot close without a MEASURED Runtime/Loop receipt.
See `adapters/kiro/adapter.py`.
