# `simplicio.run-state/v1`

Contrato mínimo do runner público iniciado por `simplicio-loop run`.

Artefatos persistidos:

- `.orchestrator/runs/<run-id>/manifest.json`
- `.orchestrator/runs/<run-id>/state.json`
- `.orchestrator/runs/<run-id>/transitions.jsonl`
- `.orchestrator/runs/<run-id>/task-contract.json`
- `.orchestrator/runs/<run-id>/mapper-preflight.json`
- `.orchestrator/runs/<run-id>/mapper-context.json`
- `.orchestrator/runs/<run-id>/plan.json`
- `.orchestrator/runs/<run-id>/operator-receipt.json`
- `.orchestrator/runs/<run-id>/completion-receipt.json`
- `.orchestrator/runs/<run-id>/loop/scratchpad.md`
- `.orchestrator/runs/<run-id>/loop/watcher_challenge.json`

Intenção:

- tornar `run/status/resume/cancel` resumíveis sem depender do host lembrar protocolo;
- obrigar `run` a só chegar em `awaiting_decision` depois de persistir contexto do mapper;
- registrar a proposta real do operador (`simplicio-dev-cli`) como recibo, mesmo antes da mutação final;
- persistir o verdict do completion oracle vinculado ao `run` + `watcher_challenge`, para que cleanup/finalização não dependam de memória transitória do hook;
- deixar a próxima fase (`mapping/planning/executing/...`) apoiada em estado tipado;
- separar “run armado” de “run realmente executado”, para evitar falso positivo.
