# ADR 0007: Orientação operacional no início de cada turno do Loop

- **Status:** Accepted
- **Date:** 2026-07-31
- **Issue:** [#921](https://github.com/wesleysimplicio/simplicio-loop/issues/921)

## Context

O `simplicio-loop` inicia e realimenta vários turnos de LLM. Instruções curtas
para limitar raciocínio, pesquisa externa e escopo precisam chegar a todos eles;
uma mensagem inicial isolada pode desaparecer no re-feed.

## Decision

Manter a orientação operacional em um único bloco marcado no arquivo canônico
`.claude/skills/simplicio-loop/SKILL.md`. `hooks/loop_stop.py` extrai esse bloco
do arquivo canônico ou de um espelho instalado e o precede a cada re-feed.

A orientação padrão usa o menor raciocínio suficiente, não expõe raciocínio
privado, não faz pesquisa web genérica nem usa conectores externos,
agentes/subagentes ou LLM local por padrão. Ela permite as ferramentas locais
autorizadas do Loop e GitHub/`gh` somente para estado atual de issue, PR ou
release; exige contexto/capacidade verificados, testes focados e relato honesto
de bloqueios.

A extração é fail-open. O hook não altera o scratchpad nem o corpo da tarefa e
não duplica o bloco se o corpo já contiver o par de marcadores; portanto, cada
turno recebe orientação estável sem crescimento cumulativo do prompt.

## Alternatives considered

1. Instrução somente na mensagem do usuário: não cobre novos re-feeds.
2. Copiar texto manualmente em cada espelho: aumenta o risco de drift; os
   espelhos devem ser gerados do arquivo canônico.
3. Bloquear todas as ferramentas: impediria contexto local e evidência live
   necessários para algumas tarefas.
4. Instruções longas ou task-specific: aumentariam tokens e fragilidade; os ACs
   continuam pertencendo à tarefa.

## Consequences

- Todo re-feed recebe a mesma política operacional e o label de evidência.
- O caminho quente faz uma leitura curta do skill e falha aberto.
- Mudanças exigem atualizar o bloco canônico, sincronizar espelhos e executar
  os testes do hook.
- Offline por padrão não significa ignorar GitHub quando a tarefa exige estado
  atual de fonte de verdade.

## Validation and rollout

Os testes E2E verificam a injeção única e o par único de marcadores. Antes do
merge devem passar a sincronização canônica/plugin/bundle, `scripts/check.py`,
`simplicio contracts smoke --json` e `simplicio validate`. A issue #921 e o
release que contiver o merge registram a publicação desta decisão.
