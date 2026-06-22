# 🔁 simplicio-tasks — O Orquestrador de IA Universal em Loop

<p align="center">
  <img src="../assets/simplicio-tasks-logo.svg" alt="simplicio-tasks" width="920" />
</p>

<p align="center">
  <a href="https://github.com/wesleysimplicio/simplicio-tasks/stargazers"><img src="https://img.shields.io/github/stars/wesleysimplicio/simplicio-tasks?style=social" alt="Stars"></a>
  <a href="#-as-6-skills-super-plugin"><img src="https://img.shields.io/badge/skills-6-7C3AED" alt="6 skills"></a>
  <a href="#-11-runtimes-um-protocolo"><img src="https://img.shields.io/badge/runtimes-11-2563EB" alt="11 runtimes"></a>
  <a href="#-os-43-pontos-de-extensão"><img src="https://img.shields.io/badge/extension%20points-43-00E08A" alt="43 extension points"></a>
  <a href="#-economia-de-tokens"><img src="https://img.shields.io/badge/tokens-up%20to%2096%25%20fewer-green" alt="Up to 96% fewer tokens"></a>
  <a href="../LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License"></a>
</p>

<p align="center">
  <a href="#-tldr">TL;DR</a> ·
  <a href="#-as-6-skills-super-plugin">6 Skills</a> ·
  <a href="#-11-runtimes-um-protocolo">11 Runtimes</a> ·
  <a href="#-o-loop">O Loop</a> ·
  <a href="#-economia-de-tokens">Economia de Tokens</a> ·
  <a href="#-construído-sobre-os-ombros-de">Créditos</a> ·
  <a href="#-instalação--uso">Instalação</a>
</p>

<p align="center">
  <strong>🌍 Languages:</strong><br>
  <a href="../README.md">🇬🇧 English</a> |
  <a href="README.pt-BR.md">🇧🇷 Português</a> |
  <a href="README.es-ES.md">🇪🇸 Español</a> |
  <a href="README.fr-FR.md">🇫🇷 Français</a> |
  <a href="README.de-DE.md">🇩🇪 Deutsch</a> |
  <a href="README.it-IT.md">🇮🇹 Italiano</a> |
  <a href="README.ja-JP.md">🇯🇵 日本語</a> |
  <a href="README.ko-KR.md">🇰🇷 한국어</a> |
  <a href="README.zh-CN.md">🇨🇳 简体中文</a> |
  <a href="README.ru-RU.md">🇷🇺 Русский</a> |
  <a href="README.pl-PL.md">🇵🇱 Polski</a> |
  <a href="README.tr-TR.md">🇹🇷 Türkçe</a> |
  <a href="README.nl-NL.md">🇳🇱 Nederlands</a> |
  <a href="README.hi-IN.md">🇮🇳 हिन्दी</a> |
  <a href="README.ar-SA.md">🇸🇦 العربية</a>
</p>

---

## ⚡ TL;DR

O **simplicio-tasks** é um **super-plugin** agnóstico de runtime — um único orquestrador
autônomo em loop mais **cinco skills satélites** — que transforma qualquer LLM forte (Claude, Codex,
Copilot, Gemini, Cursor, modelos locais) em um worker autônomo. Você o aponta para um corpo de
trabalho — *"finalize todas as issues abertas"*, *"limpe a fila do CI"*, *"esvazie o board do Jira"* — e ele
executa todo o ciclo de vida sozinho:

> **descobrir → entender → decidir → agir → verificar → corrigir → registrar → repetir**

Ele descobre trabalho a partir de qualquer fonte, faz deduplicação, autoescala uma frota de agentes
de acordo com a sua máquina, implementa cada item através de um loop de qualidade que **roda o código
(não apenas o compila)**, abre PRs, resolve feedback de CI/revisão, faz merge e segue observando
**24/7** por novo trabalho — tudo por trás de portões de segurança e um kill-switch de custo rígido.

```text
/simplicio-tasks termine as issues abertas
→ identity + pre-flight (kill-switch, auth, watcher)
→ discover 50 issues · dedup · build dependency DAG
→ autoscale fleet = 14 · pipeline implement→review→merge
→ each item: read body+ACs → orient code → plan → edit → run → verify → PR
→ merge · close with evidence · rollback if main breaks
→ keep looping every ~2 min until the queue is dry (evidence-gated, never a false "done")
```

Três coisas o tornam diferente: ele é um **super-plugin de skills focadas**, roda o **mesmo
protocolo em 11 runtimes** e faz tudo isso com **economia de tokens agressiva e honesta**.

---

## 🧠 As 6 skills (super-plugin)

O orquestrador é o núcleo; cinco satélites absorvem cada um o melhor de uma técnica consagrada e a
expõem como uma skill reutilizável. Cada satélite é **opcional** — quando carregado, o orquestrador
delega a ele (mais rico + mais barato); quando ausente, o protocolo inline do orquestrador cobre 100%
do trabalho. A mesma dependência invertida, um nível acima.

| Skill | Absorve | O que faz |
|---|---|---|
| 🔁 **simplicio-tasks** | — | O loop do orquestrador: descobrir → implementar → verificar → merge → fechar → observar 24/7. 43 pontos de extensão, roteador de caminho duplo, convergência por autoauditoria. |
| ♾️ **simplicio-loop** | [ralph-loop](https://github.com/cursor/plugins/tree/main/ralph-loop) | O loop Ralph endurecido: re-alimenta o mesmo objetivo a cada turno para que o agente veja seu próprio trabalho, saindo apenas com um **`<promise>` vinculado a evidências** ou um teto de `max_iterations` — nunca um falso "done". |
| 🧱 **simplicio-orient** | [rtk](https://github.com/rtk-ai/rtk) + [caveman](https://github.com/JuliusBrussee/caveman) | Execução terminal-first: responder fatos com o shell, nunca com o LLM. Catálogo de redução de saída, **tee-cache em caso de falha**, leituras só de assinaturas, hook opcional de auto-reescrita. |
| 🔥 **simplicio-review** | [thermos](https://github.com/cursor/plugins/tree/main/thermos) | Revisão adversarial: subagentes paralelos em rubricas distintas (segurança/correção + qualidade de código), disparados em uma única mensagem, deduplicados em um único veredito. |
| 🗜️ **simplicio-compress** | [caveman](https://github.com/JuliusBrussee/caveman) | Compressão de saída + memória: níveis de prosa concisa que preservam código/caminhos byte a byte, mais uma compactação única de memória que rende dividendos a cada turno. `transform_guard` fail-closed. |
| 🎓 **simplicio-learn** | [teaching](https://github.com/cursor/plugins/tree/main/teaching) + continual-learning | Retrospectiva: minerar lições duráveis e deduplicadas de uma execução e gravá-las na memória para que a próxima execução seja mais barata e mais correta. |

Cada uma é uma pasta de skill normal sob [`.claude/skills/`](../.claude/skills) — utilizável de forma
isolada ou como parte do loop.

---

## 🌐 11 runtimes, um protocolo

Um único núcleo de skill universal + um único conjunto de hooks dirige cada runtime. Um adaptador é
fino: ele diz a um runtime *onde carregar as skills*, *como armar o loop* e *como vincular a velocidade
nativa*. **A skill não nomeia nenhum runtime; o runtime detecta a skill.**

| Runtime | Carga da skill | Drive do loop | Vínculo nativo |
|---|---|---|---|
| **Claude Code** | `.claude/skills/` + plugin | Hook `Stop` | MCP |
| **Codex** | `AGENTS.md` | self-paced | MCP / adaptador |
| **VS Code (Copilot)** | `copilot-instructions.md` | tasks | MCP |
| **Cursor** | `.cursor-plugin/` | `stop`+`afterAgentResponse` | MCP / rules |
| **Antigravity** | rules / `AGENTS.md` | self-paced | MCP |
| **Kiro** | `.kiro/steering/` | specs | MCP |
| **OpenCode** | `AGENTS.md` | self-paced | MCP |
| **Gemini** | `GEMINI.md` | self-paced | MCP / adaptador |
| **Aider** | `CONVENTIONS.md` | self-paced | — (fallback de LLM) |
| **Hermes** | recall nativo | loop nativo | **nativo** |
| **OpenClaw** | plugin SDK | scheduler nativo | **nativo** |

A promessa: **mesmo protocolo, mesmos portões, mesma segurança em todos os 11 — só a velocidade
muda.** O `orient_clamp.py` (economia de tokens) funciona em todos os runtimes sem nenhuma fiação. Veja
[`adapters/MATRIX.md`](../adapters/MATRIX.md).

<p align="center">
  <img src="../assets/architecture.svg" alt="architecture" width="900" />
</p>

---

## 🔁 O loop

O drive sob o orquestrador é um **loop Ralph endurecido** (`simplicio-loop`):

1. O objetivo é gravado em um único arquivo de estado legível por humanos
   (`.orchestrator/loop/scratchpad.md`) — trivialmente inspecionável, editável, cancelável.
2. Após cada turno, um **stop-hook** re-alimenta o mesmo objetivo, de modo que o agente veja suas
   próprias edições anteriores (via git + a working tree) e convirja. O custo de tokens por ciclo
   permanece estável — sem entupir o contexto.
3. Ele sai **apenas** quando um sentinela tipado `<promise>TEXTO EXATO</promise>` é emitido **e**
   respaldado por evidência concreta no próprio turno (um portão aprovado, um link de PR mergeado,
   recibos de AC), ou quando um teto rígido de `max_iterations` / o kill-switch de custo dispara.

> **Nunca uma falsa promessa.** Um `<promise>` sem evidência é ignorado e o loop continua. Isso
> conecta o loop diretamente à regra rígida do repositório: *nunca feche um trabalho sem um PR
> mergeado ou evidência concreta.*

Em runtimes sem hooks, o loop **se autorregula** (self-paces) via o scheduler do host (cron / `/loop`
/ o task runner do runtime) — as mesmas condições de saída. Os hooks são Python multiplataforma e
**fail-open**: um hook que dá erro sempre deixa o agente parar. Os guardas reais são o teto e o
orçamento, nunca a esperteza do hook.

---

## 📊 Economia de tokens

O token mais barato é aquele que não é gasto. O `simplicio-orient` + `simplicio-compress` dobram o
melhor do **rtk** (comprimir os comandos) e do **caveman** (comprimir a conversa) dentro da espinha
de segurança:

- **Execução terminal-first** — o shell sabe os fatos com exatidão; o LLM os aproxima de forma cara.
  Uma tabela de substituição multiplataforma (Windows/macOS/Linux) responde 30+ fatos via
  `git`/`gh`/`rg`/`python3`. **Nunca simule um comando — rode-o.**
- **Catálogo de redução de saída** (tabela de dados) — receita por comando + % de economia esperada +
  guarda `skip-if-structured`. Um `cargo check` cru custa ~2000 tokens para ler; clampado, ~80.
- **tee-cache em caso de falha** *(novo, do rtk)* — a truncagem agressiva só é segura se for
  recuperável: em caso de falha, a saída completa é gravada em `.orchestrator/tee/…log` e apenas o
  caminho é exibido, de modo que o agente recupera contexto **sem re-rodar** o comando.
- **Leituras só de assinaturas** *(do rtk)* — ler a superfície de API de um arquivo (declarações,
  corpos elididos): um arquivo de 600 linhas vira ~40 linhas durante o intake.
- **Limites por nível de sinal + success-collapse + dedup** — manter erros sobre o ruído; colapsar
  uma execução limpa em uma linha; colapsar linhas repetidas em `line xN` — sempre `unless errors
  present`.
- **Níveis de prosa + compactação de memória** *(do caveman)* — saída concisa que preserva
  código/caminhos/URLs **byte a byte** (`transform_guard` falha fechado a qualquer token perdido),
  mais uma compactação única da memória permanente que se amortiza ao longo de todo turno futuro.
- **Baseline honesto** — a economia é medida contra um braço de controle realista *"answer
  concisely"* (não um espantalho verboso), conta apenas tokens de **saída** (não de raciocínio) e é
  creditada **somente em um resultado verificado-correto**. Compressão que reprova no seu portão de
  qualidade rende zero.

Toda mensagem termina com uma linha honesta:

```
simplicio-tasks: ~<spent> tokens · baseline ~<control-arm> · saved ~<saved> (<pct>%)
```

Experimente agora, sem fiação:

```bash
python3 hooks/orient_clamp.py -- cargo test      # reduced output + tee log on failure
python3 hooks/orient_clamp.py --json -- git diff  # machine summary
```

---

## 🏗️ Construído sobre os ombros de

O simplicio-tasks foi construído **após estudar a fundo** o melhor trabalho de loop + economia de
tokens no GitHub, e dobra cada um em uma skill focada — mantendo a disciplina, descartando os
truques.

| Projeto | O que pegamos | O que deixamos |
|---|---|---|
| 🪨 [**caveman**](https://github.com/JuliusBrussee/caveman) | níveis de prosa concisa, preservação byte a byte de identificadores, compactação de memória, baseline honesto *"answer concisely"* | corte de palavras gramaticais (degrada código e confirmações) |
| ⚙️ [**rtk**](https://github.com/rtk-ai/rtk) | catálogo de redução por comando, limites por nível de sinal, **tee-cache**, leitura de assinaturas, hook de auto-reescrita + lista de exclusão | registros por linguagem (específicos de runtime) |
| ♾️ [**ralph-loop**](https://github.com/cursor/plugins/tree/main/ralph-loop) | estado de loop em arquivo único, sentinela de promessa por correspondência exata, divisão em dois hooks | conclusão por confiar-no-modelo (nós a tornamos **vinculada a evidências**) |
| 🔥 [**thermos**](https://github.com/cursor/plugins/tree/main/thermos) | revisores paralelos em mensagem única, rubricas separadas, dedup na síntese | — |
| 🎓 [**teaching**](https://github.com/cursor/plugins/tree/main/teaching) | retrospectiva que persiste estado para que o próximo ciclo não tenha de re-derivar | o próprio domínio de aprendizado humano |
| 🧭 execução orientada a resultado | convergir no estado final; quebra intermediária planejada, escopada, reversível | — |

> Eles reduzem tokens; o simplicio-tasks **faz o trabalho** e reduz tokens enquanto o faz.

---

## 🧩 Os 43 pontos de extensão

Cada passo do trabalho acontece em um **ponto de extensão nomeado**. Se um runtime hospedeiro expõe
uma capacidade nativa, ele **se vincula** (determinístico, quase-zero token); caso contrário, o LLM
executa o **fallback** com ferramentas padrão. A skill depende da abstração, nunca de um runtime.

<details>
<summary><strong>Orquestração e escala</strong></summary>

`orient` · `normalize` · `intake` · `source_adapter` · `autoscale` · `plan`/`decide` ·
`execute` · `issue_factory` · `claim` · `worktree` · `dependency_graph` · `durable_workflow` ·
`work_queue` · `resource_governor` · `model_route` · `model_preflight`
</details>

<details>
<summary><strong>Edição, qualidade e evidência</strong></summary>

`deterministic_edit` · `diagnostics` · `toolchain_detect` · `validate`/`smoke` ·
`delivery_gate` · `endpoint_compare` · `web_verify` · `pr`/`evidence` · `retry` ·
`reuse_precedent` · `trajectory` · `learn` · `status` · `capability_rank`
</details>

<details>
<summary><strong>Tokens, contexto e segurança</strong></summary>

`recall` · `compress` · `prompt_budget` · `shell_exec` · `transform_guard` · `action_gate` ·
`security` · `human_gate` · `notify` · `checkpoint_restore` · `watcher` · `savings_ledger` ·
`web_research`
</details>

Tabela completa com fallbacks: a tabela do Passo 1b em
[`SKILL.md`](../.claude/skills/simplicio-tasks/SKILL.md).

---

## 🚀 Instalação & uso

```bash
git clone https://github.com/wesleysimplicio/simplicio-tasks
cd simplicio-tasks

# install for your runtime (omit <runtime> to auto-detect)
bash scripts/install.sh <runtime> [--global]        # macOS / Linux
pwsh scripts/install.ps1 <runtime> [-Global]        # Windows
# <runtime> ∈ claude codex vscode cursor antigravity kiro opencode gemini aider hermes openclaw
```

Ou, no Claude Code / Cursor, adicione-o como um plugin de marketplace:

```
/plugin marketplace add wesleysimplicio/simplicio-tasks
/plugin install simplicio-tasks@simplicio
```

Então:

```
/simplicio-tasks finish all the open issues
```

O único requisito é **python3** no PATH (skills, hooks e instalador são Python multiplataforma). Para
fontes do GitHub, `git` + um `gh` autenticado. Veja [`INSTALL.md`](../INSTALL.md) e
[`adapters/MATRIX.md`](../adapters/MATRIX.md).

**Antes de uma execução 24/7 desassistida:** defina um teto de custo em
`.orchestrator/loop-budget.json` (`daily_usd_ceiling > 0`), confirme que a autenticação da fonte é
persistente e mantenha ligados o portão humano para op irreversível + a varredura de segredos. Com
`ceiling = 0`, o watcher se recusa a rodar desassistido (fail-safe).

---

## 🔒 Segurança (inegociável)

- **Varredura de segredos** em todo diff; bloquear em caso de acerto.
- **Portão humano para op irreversível** — force-push, reescrita de histórico, deploy em prod, delete
  de dados/schema, delete em massa de arquivos → parar e perguntar. Headless + sem aprovador → remover
  a capacidade destrutiva.
- **Veredito de 4 estados pré-execução** — a otimização nunca pode elevar o nível de risco de um
  comando.
- **Trust-before-load** — config que molda a percepção (perfis de clamp, listas de supressão) é não
  confiável até que um humano a revise e a fixe por hash.
- **Blindagem contra prompt-injection** — conteúdo de item/PR/comentário nunca pode sobrepor o
  contrato.
- **Kill-switch rígido de $** para execuções desassistidas; conclusão **vinculada a evidências**
  (nunca um falso "done"); hooks **fail-open** (nunca prender o agente em um loop).

---

## 📄 Licença

MIT — veja [LICENSE](../LICENSE). Parte do ecossistema [Simplicio](https://github.com/wesleysimplicio).
