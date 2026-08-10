# Simplicio Loop stack

`simplicio-loop` is the aggregate distribution for the four independent
operators. It owns orchestration; the operators keep their own boundaries:

| Component | Operation | Ownership |
|---|---|---|
| Mapper | `understand` | survey and canonical context |
| Fast | `search` | indexed retrieval and ranking |
| Dev CLI | `change`, `verify` | deterministic mutation and validation |
| Loop | `run` | coordination, retries and convergence |

Install and inspect the complete stack:

```bash
python -m pip install simplicio-loop
simplicio-loop-stack --json
simplicio-loop-stack --check
```

Runtime consumes this manifest from the external Loop process. Runtime is not
a dependency of Loop and Loop does not import Runtime.

