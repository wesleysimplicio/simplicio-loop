# Executor discovery and negotiation

`capability_negotiation.py` consumes the persisted ecosystem-doctor handshake
before planning. It performs no subprocess, network, model or provider action.

An installed component is not enough. Every selected capability must have
doctor evidence with `status=verified` and an `evidence_ref`. Missing,
degraded, incompatible, disabled, policy-denied and unproven executors remain
explicitly unavailable.

Each stage declares required capabilities, ordered alternatives, preferred
languages, offline requirement and maximum cost rank. Ranking is deterministic:
compatibility descending, language preference, cost rank, then executor ID.
Rust is eligible only when a Python peer proves the same capability set and
verified parity contract. Otherwise Python is an explicit, receipted choice.

Fallback only considers alternatives written in the stage requirement and the
receipt records the original and selected capability sets. No safe candidate
returns `BLOCKED(no_safe_executor)`, never an empty success. Negotiation is
always dry-run; execution belongs to a later fenced stage.

Run:

```bash
python -m pytest -q tests/test_capability_negotiation.py
```

Performance metrics are `null`; no performance claim is made.
