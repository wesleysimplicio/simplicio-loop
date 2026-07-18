# Test-infrastructure probe

`scripts/test_infra_probe.py` turns repository structure into measured evidence
for adaptive DoD. It detects .NET, Node, Python, Go, Rust and Java test
surfaces, coverage tooling and GitHub Actions without asking an LLM to invent
counts.

When native tests are absent, an external harness is accepted only when all
three fields are present: `source`, a named PASS/FAIL `log`, and `code_hash`.
Coverage or CI without corresponding tooling is reported as
`waived:no-infra` with a reason; missing unit evidence remains `pending` until
the harness is complete.

```powershell
python scripts/test_infra_probe.py .
python scripts/test_infra_probe.py . --external-harness harness-receipt.json
```
