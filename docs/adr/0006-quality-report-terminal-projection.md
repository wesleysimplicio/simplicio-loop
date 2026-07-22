# ADR 0006: Separate quality reports from the terminal quality matrix

**Status:** accepted

Provider quality reports are broad, evolving diagnostic documents. The core completion oracle
needs a small, stable and auditable decision contract. Therefore providers own their report
schemas, while the core owns one closed `simplicio.quality-matrix/v2` schema and a deterministic
projection between them. This avoids turning rich diagnostics into unversioned core fields and
avoids schema vendoring by extensions. The oracle and watcher call the same v2 evaluator.

The compatibility reader accepts v1 only through an explicit migration. Since v1 cannot prove
content addressing, freshness, or independent audit, migration preserves failures and blocks
former passes pending fresh v2 evidence. This is intentionally fail-closed.
