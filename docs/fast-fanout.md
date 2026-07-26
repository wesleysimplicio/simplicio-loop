# Fast fan-out no Loop

O Loop usa o Simplicio Fast como contexto canônico opcional no estágio `orient` e
como base compartilhada do fan-out. O Fast não é autoridade de escrita: cada
slot recebe uma chave de overlay própria, os candidatos são vinculados à
`generation` e ao `context_hash`, e somente um candidato verificado pode ser
promovido.

## Orient

```text
simplicio-loop orient --repo . --task "..." --fast auto --fast-context-budget 48000
```

`--fast on` falha fechado se o operador Fast não estiver pronto. `--fast auto`
emite uma receipt `FALLBACK` usando Mapper quando Fast não estiver disponível;
`--fast off` mantém o comportamento standalone. Todas as receipts declaram
`local_llm=false`.

## Journal e invalidação

`FastFanoutCoordinator.snapshot()` produz a receipt
`simplicio.loop-fast-fanout/v1`. `FastFanoutCoordinator.from_snapshot(...)`
restaura slots, candidatos, winner e métricas sem novo `prepare`, preservando a
geração fixada. `invalidate()` refresca o snapshot Fast, prepara o novo contexto
e elimina candidatos antigos antes de aceitar novos changesets.

## Medição

```text
python -m scripts.benchmark_fast_fanout --root . --slots 5 --repeats 10
```

O benchmark compara construções independentes com uma construção canônica por
repetição e emite `build_reduction_factor` e `wall_speedup`. TTFT, tokens, RSS e
page faults ficam `null` com motivo explícito: este fluxo não usa LLM local e a
contabilidade de processos filhos não é portátil no Windows.
