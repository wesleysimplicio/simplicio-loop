# Run outcome contract

`simplicio-loop run` exposes the Completion Oracle's terminal decision as
`simplicio.run-outcome/v1`. The full legacy status remains on stdout. Wrappers should pass
`--result-file PATH`, read that single JSON document, and use the process exit code; they must
not scan logs or choose a run directory. Human progress may continue on other streams without
changing the result file.

| Outcome | Exit |
|---|---:|
| `COMPLETE` (fresh, same-run/source, `MEASURED` Oracle receipt only) | 0 |
| `BLOCKED` | 20 |
| `CANCELLED` | 21 |
| `PARTIAL` | 22 |
| `INVALID_RECEIPT` | 23 |
| `INFRASTRUCTURE_FAILURE` | 24 |

The document binds the run and source identity/digest, final phase, Oracle verdict, and exact
completion-receipt SHA-256. Extensions advertise/consume capability `run-outcome/v1`; they may
never create a completion receipt or override `oracle.authorized`.
