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
python -m scripts.benchmark_fast_fanout --root simplicio_loop --slots 5 --repeats 10 --engine library
```

O modo `library` chama o núcleo `simplicio_fast.build_snapshot` no mesmo processo,
evitando o custo de iniciar Mapper/Fast em cada slot; `--engine cli` preserva a
medição E2E por subprocesso. Em 10 repetições e 5 slots no pacote
`simplicio_loop`, a medição real produziu 50 builds independentes contra 10
canônicos compartilhados, redução `5.0x`, speedup de parede `4.698x`, speedup de
CPU `4.955x` e `functional_equivalence=true`. TTFT, tokens, RSS e page faults
ficam `null` com motivo explícito: este fluxo não usa LLM local e a contabilidade
de processos filhos não é portátil no Windows.

O coordenador delega transições `shadow`, `canary`, `disable` e `rollback` ao
receipt atômico do Fast (`disable` usa o modo Fast `fallback`). Promoção fica
bloqueada enquanto o rollout estiver desabilitado, em fallback ou revertido.
