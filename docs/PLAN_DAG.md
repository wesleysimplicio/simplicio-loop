# Deterministic PlanDAG execution

The Loop keeps `canonical_plan.py` as the consumer boundary for the Dev CLI
contract. `plan_dag.py` adds deterministic execution analysis after normalized
planning input exists. It does not invoke an LLM or provider.

The compiler pins Mapper context snapshot/hash and Fast generation, normalizes
goal, acceptance criteria, risks, non-goals and oracle, then freezes a plan
hash and revision. Nodes carry logical task identity, capabilities, I/O,
read/write sets, context references and tests.

Dependencies come from explicit edges and matching producer outputs to consumer
inputs. Cycles and missing dependencies fail closed. Exact semantic resources
(`symbol:`, `config:`, `db:`, `api:`) conflict; explicit `dir:` ownership also
conflicts with nested `file:` writes. Execution waves never place conflicting
write-sets together.

Drift invalidates only nodes bound to changed context, generation-sensitive
nodes, and their descendants. Replan requires an observable reason plus
evidence and retains the previous revision/hash in history. Slot assignments
are separate receipts and never change logical task identity or plan hash.

Run:

```bash
python -m pytest -q tests/test_plan_dag.py
```

Performance metrics are `null`; this work proves planning safety and
determinism without claiming speed improvements.
