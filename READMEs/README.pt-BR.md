# 🔁 simplicio-loop — O Orquestrador de IA Universal em Loop

<p align="center">
  <img src="../assets/simplicio-loop-hero.jpg" alt="simplicio-loop" width="920" />
</p>

<p align="center">
  <a href="https://github.com/wesleysimplicio/simplicio-loop/stargazers"><img src="https://img.shields.io/github/stars/wesleysimplicio/simplicio-loop?style=social" alt="Stars"></a>
  <a href="#-as-10-skills--aceleradores"><img src="https://img.shields.io/badge/skills-10-7C3AED" alt="10 skills"></a>
  <a href="#-adaptadores-de-fonte"><img src="https://img.shields.io/badge/source%20adapters-6-00E08A" alt="6 source adapters"></a>
  <a href="#-11-runtimes-um-protocolo"><img src="https://img.shields.io/badge/runtimes-11-2563EB" alt="11 runtimes"></a>
  <a href="#-aceleradores"><img src="https://img.shields.io/badge/accelerators-3-FF6B6B" alt="3 accelerators"></a>
  <a href="#-economia-de-tokens"><img src="https://img.shields.io/badge/tokens-até%2096%25%20menos-green" alt="Até 96% menos tokens"></a>
  <a href="../LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License"></a>
</p>

---

## ⚡ TL;DR

O **simplicio-loop** é um **super-plugin** agnóstico de runtime — um orquestrador autônomo em loop
mais **cinco skills satélites** — que transforma qualquer LLM forte em um worker autônomo.
Aponte para um corpo de trabalho e ele executa todo o ciclo sozinho:

> **descobrir → entender → decidir → agir → verificar → corrigir → registrar → repetir**

Descobre trabalho de qualquer fonte (GitHub Issues, Jira, Azure DevOps, sessões agentsview),
deduplica, autoescala uma frota, implementa com loop de qualidade que **roda o código**, abre PRs,
resolve feedback, mergeia e segue 24/7 atrás de novo trabalho — com gates de segurança e
kill-switch de custo.

---

## 🧠 As 6 skills (super-plugin)

| Skill | Absorve | O que faz |
|---|---|---|
| 🔁 **simplicio-tasks** | — | O loop do orquestrador: 43 pontos de extensão, roteador de caminho duplo |
| ♾️ **simplicio-loop** | ralph-loop | Loop Ralph endurecido: re-alimenta o mesmo objetivo, saída only com evidência |
| 🧱 **simplicio-orient** | rtk + caveman | Execução terminal-first, catálogo de redução de saída |
| 🔥 **simplicio-review** | thermos | Revisão adversarial: subagentes paralelos em rubricas distintas |
| 🗜️ **simplicio-compress** | caveman | Compressão de saída + memória, fail-closed transform_guard |
| 🎓 **simplicio-learn** | continual-learning | Retrospectiva → lições duráveis na memória |

---

## 📡 Adaptadores de fonte

| Fonte | Adaptador | Propósito |
|---|---|---|
| GitHub Issues/PRs | `gh` CLI (nativo) | Fonte primária de work-items |
| Jira / Asana / ClickUp / Linear / Notion | connector do host | Gerenciamento de projeto |
| Trello / Azure DevOps | `az boards` adapter | Azure work tracking |
| **Agentsview sessões** | `scripts/agentsview_adapter.py` | Recuperação de sessões paradas + observabilidade de custo |
| Arquivos locais / fila CI | filesystem / CI API | Work tracking interno |

---

## ⚡ Aceleradores

| Acelerador | Ponto de extensão | Impacto em tokens |
|---|---|---|
| **Understand Anything** | `orient` / `recall` (Step 2b-2) | **L0 (zero tokens)** — queries JSON, não LLM |
| **Agentsview** | `source_adapter` + pre-flight budget | **L1** — SQL agregado, sem LLM |
| **LMCache** | `model_route` (Step 3d) + token economy | **40-70% menos TTFT** em modelos locais |

---

## 📋 Atividade recente

| # | PR | Estado | Descrição |
|---|---|---|---|
| 39 | [#39](https://github.com/wesleysimplicio/simplicio-loop/pull/39) | ✅ Mergeado | agentsview (source adapter) + Understand Anything (orient) + LMCache (accelerator) |
| 38 | [#38](https://github.com/wesleysimplicio/simplicio-loop/pull/38) | ✅ Mergeado | agentsview source adapter |
| 36 | [#36](https://github.com/wesleysimplicio/simplicio-loop/pull/36) | ✅ Mergeado | Operadores de loop obrigatórios |
| 35 | [#35](https://github.com/wesleysimplicio/simplicio-loop/pull/35) | ✅ Mergeado | Contrato normativo do loop |
| 33 | [#33](https://github.com/wesleysimplicio/simplicio-loop/pull/33) | ✅ Mergeado | Release 1.0.3 |
| 32 | [#32](https://github.com/wesleysimplicio/simplicio-loop/pull/32) | ✅ Mergeado | Hardening do contrato do loop |
| 25 | [#25](https://github.com/wesleysimplicio/simplicio-loop/pull/25) | ✅ Mergeado | PyPI packaging 1.0.2 |
| 24 | [#24](https://github.com/wesleysimplicio/simplicio-loop/pull/24) | ✅ Mergeado | Fix 1.0.2 |
| 23 | [#23](https://github.com/wesleysimplicio/simplicio-loop/pull/23) | ✅ Mergeado | Auto-loop + language policy |
| 22 | [#22](https://github.com/wesleysimplicio/simplicio-loop/pull/22) | ✅ Mergeado | Close #15/#10/#12 + e2e verifier |

---

## 🚀 Instalação

```bash
git clone https://github.com/wesleysimplicio/simplicio-loop
cd simplicio-loop
bash scripts/install.sh <runtime> [--global]
# <runtime> ∈ claude codex vscode cursor antigravity kiro opencode gemini aider hermes openclaw
```

Uso: `/simplicio-tasks finalize todas as issues abertas`

Veja o [README em inglês](../README.md) para documentação completa em inglês.
