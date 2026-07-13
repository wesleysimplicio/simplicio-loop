Sistema: Maintenance deferred demo
Funcionalidade: Broader loop goal continues after backlog-only correction capture
Tipo: Evolução

COMO operador do loop,
QUERO registrar uma correção que não pode mutar o plano de controle congelado
PARA continuar o objetivo maior com recibo auditável e retomada explícita

1. Critérios de Aceite

Cenário 1: Correção adiada sem falso done
  Dado que o run está ativo
  Quando uma correção depende de uma janela de manutenção
  Então a correção é registrada em backlog-only e o objetivo maior continua aberto [RN01][RN02]

2. Regras de Negócio

RN01 – O backlog-only não pode marcar completion.ready=true.
RN02 – O resume deve rearmar mapper/operator para a continuação normal.
