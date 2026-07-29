#!/usr/bin/env python3
"""Render the Loop-stack economy interpretation report (PDF + charts).

Reads multi_issue_lanes_metrics.json (and optional issue27 metrics) and writes
docs/evidence/loop_stack_economy_benchmark_report.pdf with:
  - executive narrative (why results look the way they do)
  - pie charts (token composition, savings attribution)
  - bar charts (lanes × issues, wall, tokens)
  - tables and interpretation

Usage:
  python scripts/render_loop_stack_economy_report_pdf.py
  python scripts/render_loop_stack_economy_report_pdf.py --out docs/evidence/foo.pdf
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEFAULT_METRICS = REPO / "docs" / "evidence" / "multi_issue_lanes_metrics.json"
DEFAULT_OUT = REPO / "docs" / "evidence" / "loop_stack_economy_benchmark_report.pdf"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def render(metrics: dict, out: Path, *, title_prefix: str = "Simplicio") -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages
    from matplotlib.patches import FancyBboxPatch

    runs = metrics["runs"]
    issues = metrics.get("issues", {})
    agg = metrics["aggregate"]
    issue_nums = metrics.get("issue_numbers_old_to_new") or sorted(
        {r["issue"] for r in runs}
    )
    lanes = metrics.get("lanes") or [
        "baseline",
        "loop",
        "loop_mcp",
        "mcp_only",
        "loop_no_fast",
    ]
    colors = {
        "baseline": "#6b7280",
        "loop": "#2563eb",
        "loop_mcp": "#7c3aed",
        "mcp_only": "#f59e0b",
        "loop_no_fast": "#059669",
    }
    labels_pt = {
        "baseline": "baseline (host reads)",
        "loop": "loop STRICT (mapper+fast)",
        "loop_mcp": "loop + Agent MCP",
        "mcp_only": "mcp_only (tools)",
        "loop_no_fast": "loop sem Fast (diag.)",
    }

    def lane_run(issue: int, lane: str):
        return next((r for r in runs if r["issue"] == issue and r["lane"] == lane), None)

    # average loop step tokens for pie
    loop_steps_acc: dict[str, list[float]] = {}
    for r in runs:
        if r["lane"] != "loop":
            continue
        for s in r["steps"]:
            loop_steps_acc.setdefault(s["name"], []).append(s["estimated_tokens"])
    loop_step_means = {
        k: statistics.mean(v) for k, v in loop_steps_acc.items() if statistics.mean(v) > 1
    }

    out.parent.mkdir(parents=True, exist_ok=True)
    with PdfPages(str(out)) as pdf:
        # ---- Page 1: title + TL;DR ----
        fig, ax = plt.subplots(figsize=(11, 8.5))
        ax.axis("off")
        fig.patch.set_facecolor("#0b1220")
        ax.set_facecolor("#0b1220")
        ax.text(
            0.05,
            0.92,
            f"{title_prefix} — Loop stack economy benchmark",
            color="#f8fafc",
            fontsize=18,
            weight="bold",
            transform=ax.transAxes,
        )
        ax.text(
            0.05,
            0.86,
            "Why the numbers look the way they do · multi-issue matrix · Fast is part of loop",
            color="#93c5fd",
            fontsize=11,
            transform=ax.transAxes,
        )
        body = f"""
Stack measured: {json.dumps(metrics.get('versions', {}), ensure_ascii=False)}
Issues (old→new): {issue_nums}
Lanes: {lanes}
Token method: {metrics.get('token_estimate_method', 'context_bytes/4')}
Runs: {len(runs)}  |  schema: {metrics.get('schema')}

TL;DR
• STRICT loop already includes Fast when operational (mapper · fast · dev-cli).
• “% savings” is vs a host full-file baseline — not a quality score.
• loop_no_fast often “saves more tokens” because it does LESS work (no Fast packets).
• mcp_only ~99% savings = almost no repo survey (tool metadata), not better delivery.
• loop_mcp ≈ loop on tokens (+small overhead) — MCP is a capability bus, not the main compressor.
• Wall clock is high on cold monorepo operator startup; multi-turn cost tracks tokens more.

Mean token savings vs baseline (5 issues):
"""
        for lane, st in agg.get("mean_token_savings_vs_baseline", {}).items():
            body += (
                f"  {lane:14s}  mean {st['mean_token_savings_pct']:6.1f}%  "
                f"med {st['median_token_savings_pct']:6.1f}%  "
                f"[{st['min']:6.1f} .. {st['max']:6.1f}]\n"
            )
        by = agg.get("by_lane", {})
        body += "\nMean wall / tokens / fast_calls:\n"
        for lane in lanes:
            if lane not in by:
                continue
            st = by[lane]
            body += (
                f"  {lane:14s}  wall {st['wall_s']['mean']:6.1f}s  "
                f"tok {st['estimated_tokens']['mean']:8.0f}  "
                f"fast {st['fast_calls']['mean']:.1f}  "
                f"success {st['success_rate']*100:.0f}%\n"
            )
        ax.text(
            0.05,
            0.80,
            body,
            color="#e2e8f0",
            fontsize=8.5,
            family="monospace",
            va="top",
            transform=ax.transAxes,
        )
        pdf.savefig(fig, facecolor=fig.get_facecolor())
        plt.close(fig)

        # ---- Page 2: architecture / barramento diagram (boxes) ----
        fig, ax = plt.subplots(figsize=(11, 8.5))
        ax.set_xlim(0, 10)
        ax.set_ylim(0, 10)
        ax.axis("off")
        ax.set_title(
            "Barramento do stack (contrato STRICT) — o que cada camada faz",
            fontsize=13,
            pad=12,
        )

        def box(x, y, w, h, text, color):
            p = FancyBboxPatch(
                (x, y),
                w,
                h,
                boxstyle="round,pad=0.05,rounding_size=0.15",
                facecolor=color,
                edgecolor="#1e293b",
                linewidth=1.2,
                alpha=0.92,
            )
            ax.add_patch(p)
            ax.text(
                x + w / 2,
                y + h / 2,
                text,
                ha="center",
                va="center",
                fontsize=8.5,
                color="white",
                wrap=True,
            )

        box(0.4, 8.2, 9.2, 1.2, "LLM host (Claude / Grok / Cursor / …)\nrazão + coordenação — NÃO deve dump full-tree", "#334155")
        box(0.4, 6.5, 9.2, 1.2, "simplicio-loop STRICT\npreflight · journal · evidence · operators floor", "#2563eb")
        box(0.4, 4.6, 2.9, 1.5, "mapper\nsurvey / handoff\nestrutura", "#0ea5e9")
        box(3.55, 4.6, 2.9, 1.5, "fast (parte do loop)\nunderstand / plan / context\npacotes bounded", "#059669")
        box(6.7, 4.6, 2.9, 1.5, "dev-cli\nmutações mecânicas\nSTRICT only", "#7c3aed")
        box(0.4, 2.6, 4.5, 1.5, "Agent MCP (opcional)\n19 tools · CLI fallback · watchdog\ncapacidade / barramento", "#f59e0b")
        box(5.2, 2.6, 4.4, 1.5, "Runtime MCP (opcional)\nsimplicio serve --mcp\neconomia de tools no host", "#ea580c")
        box(0.4, 0.6, 9.2, 1.4, "NÃO obrigatório para o loop: Runtime\nCore loop = mapper + dev-cli (+ Fast se operacional)\nMCP força tools quando presente — não torna Runtime hard-dep", "#64748b")
        ax.annotate("", xy=(5, 6.5), xytext=(5, 8.2), arrowprops=dict(arrowstyle="->", color="#94a3b8", lw=1.5))
        ax.annotate("", xy=(5, 6.1), xytext=(5, 6.5), arrowprops=dict(arrowstyle="->", color="#94a3b8", lw=1.5))
        pdf.savefig(fig)
        plt.close(fig)

        # ---- Page 3: pie mean token savings composition story ----
        fig, axes = plt.subplots(1, 2, figsize=(11, 8.5))
        # pie 1: mean tokens by lane
        means = [agg["by_lane"][L]["estimated_tokens"]["mean"] for L in lanes if L in agg["by_lane"]]
        names = [labels_pt.get(L, L) for L in lanes if L in agg["by_lane"]]
        cols = [colors.get(L, "#999") for L in lanes if L in agg["by_lane"]]
        axes[0].pie(
            means,
            labels=names,
            colors=cols,
            autopct=lambda p: f"{p:.0f}%",
            textprops={"fontsize": 7},
            startangle=90,
        )
        axes[0].set_title("Share of mean est. tokens by lane\n(relative size of intake)")

        # pie 2: loop step composition
        if loop_step_means:
            keys = list(loop_step_means.keys())
            vals = [loop_step_means[k] for k in keys]
            axes[1].pie(
                vals,
                labels=keys,
                autopct=lambda p: f"{p:.0f}%",
                textprops={"fontsize": 7},
                startangle=90,
                colors=plt.cm.Blues([0.3 + 0.08 * i for i in range(len(keys))]),
            )
            axes[1].set_title("Where loop tokens go (mean across issues)\nFast packets dominate on purpose")
        else:
            axes[1].text(0.5, 0.5, "no loop step data", ha="center")
            axes[1].axis("off")
        fig.suptitle("Pizza — composição de contexto", fontsize=13)
        fig.tight_layout()
        pdf.savefig(fig)
        plt.close(fig)

        # ---- Page 4: bar tokens by issue × lane ----
        fig, ax = plt.subplots(figsize=(11, 8.5))
        x = range(len(issue_nums))
        width = 0.15
        for i, lane in enumerate(lanes):
            vals = []
            for num in issue_nums:
                hit = lane_run(num, lane)
                vals.append(hit["estimated_tokens"] if hit else 0)
            ax.bar(
                [xi + (i - 2) * width for xi in x],
                vals,
                width,
                label=labels_pt.get(lane, lane),
                color=colors.get(lane, "#999"),
            )
        ax.set_xticks(list(x))
        ax.set_xticklabels(
            [
                f"#{n}\n{(issues.get(str(n), {}).get('title') or '')[:22]}"
                for n in issue_nums
            ],
            fontsize=7,
        )
        ax.set_ylabel("Estimated tokens (bytes/4)")
        ax.set_title("Barras — est. tokens por issue × lane")
        ax.legend(fontsize=7, loc="upper right")
        fig.tight_layout()
        pdf.savefig(fig)
        plt.close(fig)

        # ---- Page 5: wall bars ----
        fig, ax = plt.subplots(figsize=(11, 8.5))
        for i, lane in enumerate(lanes):
            vals = []
            for num in issue_nums:
                hit = lane_run(num, lane)
                vals.append(hit["wall_s"] if hit else 0)
            ax.bar(
                [xi + (i - 2) * width for xi in x],
                vals,
                width,
                label=labels_pt.get(lane, lane),
                color=colors.get(lane, "#999"),
            )
        ax.set_xticks(list(x))
        ax.set_xticklabels([f"#{n}" for n in issue_nums])
        ax.set_ylabel("Wall time (s)")
        ax.set_title("Barras — wall clock frio por issue × lane\n(operators perdem no cold start; baseline é leitura local)")
        ax.legend(fontsize=7)
        fig.tight_layout()
        pdf.savefig(fig)
        plt.close(fig)

        # ---- Page 6: savings horizontal + is fast acting ----
        fig, axes = plt.subplots(1, 2, figsize=(11, 8.5))
        ms = agg.get("mean_token_savings_vs_baseline", {})
        names = list(ms.keys())
        vals = [ms[n]["mean_token_savings_pct"] for n in names]
        axes[0].barh(
            [labels_pt.get(n, n) for n in names],
            vals,
            color=[colors.get(n, "#999") for n in names],
        )
        axes[0].axvline(0, color="black", lw=0.8)
        axes[0].set_xlabel("Mean token savings % vs baseline")
        axes[0].set_title("Economia média (5 issues)\nCuidado: mcp_only alto = pouco trabalho")
        for i, v in enumerate(vals):
            axes[0].text(v + 0.5, i, f"{v:.1f}%", va="center", fontsize=8)

        by = agg.get("by_lane", {})
        ln = [L for L in lanes if L in by]
        axes[1].bar(
            [labels_pt.get(L, L) for L in ln],
            [by[L]["fast_calls"]["mean"] for L in ln],
            color="#059669",
        )
        axes[1].tick_params(axis="x", rotation=25, labelsize=7)
        axes[1].set_title("Fast está atuando?\n(mean fast_calls / issue)")
        axes[1].set_ylabel("fast_calls")
        for i, L in enumerate(ln):
            axes[1].text(i, by[L]["fast_calls"]["mean"] + 0.05, f"{by[L]['fast_calls']['mean']:.0f}", ha="center")
        fig.tight_layout()
        pdf.savefig(fig)
        plt.close(fig)

        # ---- Page 7: pairwise heatmap-style table ----
        fig, ax = plt.subplots(figsize=(11, 8.5))
        ax.axis("off")
        header = ["Issue", "baseline tok", "loop %", "loop_mcp %", "loop_no_fast %", "mcp_only %"]
        rows = [header]
        pw = agg.get("pairwise_vs_baseline", {})
        for num in issue_nums:
            base = lane_run(num, "baseline")
            p = pw.get(str(num), {})
            rows.append(
                [
                    f"#{num}",
                    f"{(base or {}).get('estimated_tokens', 0):.0f}",
                    f"{p.get('loop', {}).get('token_savings_pct', '—')}",
                    f"{p.get('loop_mcp', {}).get('token_savings_pct', '—')}",
                    f"{p.get('loop_no_fast', {}).get('token_savings_pct', '—')}",
                    f"{p.get('mcp_only', {}).get('token_savings_pct', '—')}",
                ]
            )
        table = ax.table(cellText=rows, loc="center", cellLoc="center")
        table.auto_set_font_size(False)
        table.set_fontsize(9)
        table.scale(1.15, 1.7)
        ax.set_title(
            "Tabela — savings % vs baseline por issue\n"
            "Negativo (#171) = baseline pequeno < piso semântico do loop (~21k tok)",
            fontsize=11,
        )
        pdf.savefig(fig)
        plt.close(fig)

        # ---- Page 8: interpretation narrative ----
        fig, ax = plt.subplots(figsize=(11, 8.5))
        ax.axis("off")
        text = """
POR QUE OS RESULTADOS SAÍRAM ASSIM

1) Três eixos misturados
   • Economia de contexto (tokens estimados = context_bytes/4)
   • Trabalho real (orientar / planejar / tools)
   • Wall-clock frio (subir preflight/mapper/fast no monorepo)
   Ler só “% savings” como “melhor sistema” é erro.

2) Baseline ≠ tratamento
   Host full-file: barato em CPU, caro em tokens se a árvore for grande.
   Loop STRICT: mais wall frio, contexto empacotado (mapper + Fast).
   loop_no_fast: menos texto que full loop porque OMITTE Fast.
   mcp_only: quase zero survey de repo → % altíssimo e enganoso.

3) Fast É parte do loop STRICT
   Contrato: mapper · fast (se operacional) · dev-cli.
   Nos runs loop/loop_mcp: fast_calls = 5 sempre (doctor, refresh/build,
   understand, plan, context). Não é “ligar Fast de novo”.

4) Onde vão os tokens do loop
   A maior parte vem de understand/plan/context do Fast (~15–18k),
   depois mapper handoff. Isso é piso semântico útil, não desperdício.

5) MCP quase não corta tokens
   loop_mcp ≈ loop (+~80 tok, +~3–4 s). MCP = barramento/tools/governança,
   não compressor principal. Runtime MCP no host é outra superfície.

6) Variância entre issues
   Baseline grande (#96/#322 ~125k) → loop ~83% savings.
   Baseline médio (#9/#711) → ~25–34%.
   Baseline pequeno (#171 ~16k) → loop “pior” em tokens (−31%) e ainda assim
   é o path STRICT correto (mais orientação).

7) Wall ~50s no loop
   Cold start monorepo (preflight + Fast + mapper). Multi-turn billed cost
   segue tokens, não esses segundos frios.

CONCLUSÃO DE PRODUTO
• Manter loop STRICT + Fast quando Fast up.
• Não vender MCP como economizador de tokens; vender como bus.
• Reportar sempre: tokens + wall frio + sucesso/trabalho — nunca só %.
• loop_no_fast e mcp_only são diagnósticos, não vitórias de economia.
"""
        ax.text(0.04, 0.97, text, va="top", family="monospace", fontsize=8.2)
        ax.set_title("Interpretação detalhada", fontsize=13, loc="left")
        pdf.savefig(fig)
        plt.close(fig)

        # ---- Page 9: what I would do next ----
        fig, ax = plt.subplots(figsize=(11, 8.5))
        ax.axis("off")
        text = """
O QUE EU FARIA A SEGUIR (recomendação)

1. Scorecard único no README
   tokens_saved · wall_cold · work_done · success
   (impede mcp_only 99% parecer vitória)

2. Bench “issue done”
   Mesma issue → PR + testes nas lanes, com snapshot Fast quente
   e tokens de sessão LLM reais (provider-billed), não só bytes/4

3. Separar superfícies MCP no marketing
   Agent MCP serve  vs  Runtime MCP (simplicio serve --mcp)
   Ambos “MCP”, papéis diferentes

4. Warm path documentado
   Publicar números frios E quentes (2ª+ iteração) lado a lado

5. Baseline policy
   Mesmo conjunto de arquivos “related” versionado no fixture
   para issues com baseline artificialmente pequeno

6. Telemetria contínua
   Journal loop: operator_calls, context_bytes, fast_calls por run
   Dashboard simples no loop/runtime doctor
"""
        ax.text(0.05, 0.92, text, va="top", family="monospace", fontsize=10)
        ax.set_title("Next steps", fontsize=13, loc="left")
        pdf.savefig(fig)
        plt.close(fig)

        # ---- Page 10: reproduce ----
        fig, ax = plt.subplots(figsize=(11, 8.5))
        ax.axis("off")
        text = f"""
REPRODUZIR

# no simplicio-agent (harness completo ~15–20 min)
python scripts/bench_multi_issue_lanes.py

# gerar este PDF a partir do JSON
python scripts/render_loop_stack_economy_report_pdf.py \\
  --metrics docs/evidence/multi_issue_lanes_metrics.json \\
  --out docs/evidence/loop_stack_economy_benchmark_report.pdf

Artefatos relacionados
• multi_issue_lanes_metrics.json — raw runs + aggregate
• multi_issue_lanes_bench.pdf — charts do harness multi-issue
• issue27_loop_fast_mcp_* — slice MCP bus #27
• Este arquivo — interpretação + pizza + barras + barramento

Repos
{issues}
"""
        ax.text(0.04, 0.95, text, va="top", family="monospace", fontsize=8)
        pdf.savefig(fig)
        plt.close(fig)

    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--metrics", type=Path, default=DEFAULT_METRICS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--title-prefix", default="Simplicio Loop")
    args = ap.parse_args()
    if not args.metrics.is_file():
        raise SystemExit(f"metrics not found: {args.metrics}")
    metrics = load(args.metrics)
    path = render(metrics, args.out, title_prefix=args.title_prefix)
    print(f"wrote {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
