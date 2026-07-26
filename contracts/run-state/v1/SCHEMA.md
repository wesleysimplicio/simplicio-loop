# `simplicio.run-state/v1`

Contrato mínimo do runner público iniciado por `simplicio-loop run`.

Artefatos persistidos:

- `.simplicio/orchestrator/runs/<run-id>/manifest.json`
- `.simplicio/orchestrator/runs/<run-id>/state.json`
- `.simplicio/orchestrator/runs/<run-id>/transitions.jsonl`
- `.simplicio/orchestrator/runs/<run-id>/task-contract.json`
- `.simplicio/orchestrator/runs/<run-id>/mapper-preflight.json`
- `.simplicio/orchestrator/runs/<run-id>/mapper-context.json`
- `.simplicio/orchestrator/runs/<run-id>/plan.json`
- `.simplicio/orchestrator/runs/<run-id>/operator-receipt.json`
- `.simplicio/orchestrator/runs/<run-id>/completion-receipt.json`
- `.simplicio/orchestrator/runs/<run-id>/loop/scratchpad.md`
- `.simplicio/orchestrator/runs/<run-id>/loop/watcher_challenge.json`

Intenção:

- tornar `run/status/resume/cancel` resumíveis sem depender do host lembrar protocolo;
- obrigar `run` a só chegar em `awaiting_decision` depois de persistir contexto do mapper;
- registrar a proposta real do operador (`simplicio-dev-cli`) como recibo, mesmo antes da mutação final;
- persistir o verdict do completion oracle vinculado ao `run` + `watcher_challenge`, para que cleanup/finalização não dependam de memória transitória do hook;
- deixar a próxima fase (`mapping/planning/executing/...`) apoiada em estado tipado;
- separar “run armado” de “run realmente executado”, para evitar falso positivo.
