"""Offline, receipt-backed report; never ranks these diagnostics as Loop arms."""
import argparse
from datetime import datetime, timezone
import hashlib
import html
import json
import math
from pathlib import Path
import shlex


def quantile(values, fraction):
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def summarize(root):
    root = Path(root).resolve()
    inputs, attempts, calls, resources = {}, [], {}, {}

    def read(path):
        path = Path(path)
        raw = path.read_bytes()
        inputs[str(path)] = hashlib.sha256(raw).hexdigest()
        return json.loads(raw)

    def resource(path, task, stage):
        path = Path(path).resolve()
        if path in resources or not path.exists():
            return
        value = read(path)
        value.pop("samples", None)
        resources[path] = dict(value, task=task, stage=stage, receipt=str(path))

    def provider(path, verified=None):
        record = read(path)
        response = record.get("response", {})
        identity = response.get("id") or str(path)
        if identity in calls:
            return
        usage = response.get("usage") or {}
        task = Path(record.get("task", "unknown")).stem
        calls[identity] = {
            "id": identity, "task": task, "receipt": str(path),
            "status": record.get("status"), "attempt_verified": verified,
            "model": response.get("model", record.get("model")),
            "provider": response.get("provider"), "repair_of": record.get("repair_of"),
            "wall_seconds": record.get("provider_wall_ns", 0) / 1e9 if record.get("provider_wall_ns") is not None else None,
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
            "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
            "cache_write_tokens": (usage.get("prompt_tokens_details") or {}).get("cache_write_tokens"),
            "cost_usd": usage.get("cost"),
            "context_sha256": record.get("context_sha256"),
            "parent_context_sha256": record.get("parent_context_sha256"),
        }

    for path in sorted(root.glob("diagnostic-*/summary.json")):
        item = read(path)
        attempts.append(dict(item, receipt=str(path)))
        for stage in item.get("stages", []):
            resource(stage["receipt"], item["task"], stage["name"])
        if (path.parent / "provider.json").exists():
            provider(path.parent / "provider.json", item.get("verified"))
    for name in ["GH-102", "GH-103", "JIRA-201"]:
        path = root / ("openrouter-worker-" + name + ".json")
        if path.exists():
            provider(path)
    for name, stage in [("worker", "provider"), ("apply", "apply"), ("verify", "verify")]:
        resource(root / ("resource-" + name + "-JIRA201") / "receipt.json", "JIRA-201", stage)
    final_path = root / "queue-final-verification-20260911/stdout.txt"
    final = read(final_path) if final_path.exists() else None
    resource(root / "queue-final-verification-20260911/receipt.json", "all", "final_verify")
    calls = list(calls.values())
    fields = ["input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens", "cache_write_tokens", "cost_usd"]
    totals = {}
    for field in fields:
        values = [x[field] for x in calls if x[field] is not None]
        totals[field] = {"observed_sum": sum(values) if values else None,
                         "known": len(values), "total": len(calls),
                         "complete": len(values) == len(calls) and bool(calls)}
    latencies = [x["wall_seconds"] for x in calls if x["wall_seconds"] is not None]
    return {"schema": "loop-diagnostic-report/v1", "generated_at": datetime.now(timezone.utc).isoformat(),
            "eligible_loop_cli_arm": False, "winner": None, "inputs": inputs,
            "attempts": attempts, "provider_calls": calls, "provider_totals": totals,
            "resource_stages": list(resources.values()), "final_verification": final,
            "latency": {"population": "heterogeneous diagnostic provider attempts, including failed proposal",
                        "n": len(latencies), "p50_seconds_nearest_rank": quantile(latencies, .5),
                        "p99_seconds_nearest_rank": quantile(latencies, .99),
                        "sum_seconds": sum(latencies), "warning": "p99 is exploratory maximum, not a tail estimate or workflow ranking"}}


def markdown(report):
    out = ["# OpenRouter diagnostic measurements", "",
           "Not a qualified Loop serial/Prism/Fast comparison. No winning default.", "",
           "## Provider attempts (including rejected proposal)", "",
           "|Task|Request seconds|Input|Output incl. reasoning|Reasoning subset|Cached input|USD|",
           "|---|---:|---:|---:|---:|---:|---:|"]
    for row in report["provider_calls"]:
        out.append("|" + "|".join(str(row[k]) for k in ["task", "wall_seconds", "input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens", "cost_usd"]) + "|")
    out += ["", "Missing values are unavailable, never zero. Reasoning is already included in output.", "", "## Exact measured commands", ""]
    for row in report["resource_stages"]:
        out += [f"### {row['task']} / {row['stage']}", "",
                "```text", shlex.join(row["argv"]), "```", "",
                f"cwd: `{row['cwd']}`; exit: {row['exit_code']}; wall: {row['wall_ns']/1e9:.6f}s; CPU: {row.get('reaped_children_cpu_seconds')}s; sampled RSS peak: {row.get('peak_tree_rss_bytes_sampled')} bytes.",
                f"Receipt: `{row['receipt']}`", ""]
    out += ["## Interpretation", "",
            "Provider wall time is request-to-complete-response, not TTFT. CPU is local process-tree accounting, not provider CPU. RSS sampled every 100 ms can miss short peaks and double-count shared pages.", "",
            "The first pilots lack full end-to-end resource coverage. Do not sum these stage maxima or heterogeneous task times into a comparative workflow result. All inputs and SHA-256 hashes are in summary.json.", ""]
    return "\n".join(out)


def render_html(report, coverage):
    escape = lambda value: html.escape(str(value))
    calls = report["provider_calls"]
    def bars(field, unit):
        known = [x[field] for x in calls if x[field] is not None]
        maximum = max(known, default=1) or 1
        rows = []
        for i, row in enumerate(calls, 1):
            value = row[field]
            width = 0 if value is None else value / maximum * 100
            rows.append(f'<div class="mark"><span>{escape(row["task"])} · {i}</span><div class="track"><div style="width:{width:.4f}%"></div></div><span>{"N/D" if value is None else f"{value:.6g}"} {unit}</span></div>')
        return "".join(rows)
    passed = sum(x["status"] == "passed" for x in (report.get("final_verification") or {}).get("results", []))
    cost = report["provider_totals"]["cost_usd"]
    sections = []
    for attempt in report["attempts"]:
        state = "PASS" if attempt.get("verified") else "BLOQUEADA / REJEITADA"
        sections.append(f'<tr><td>{escape(attempt["task"])}</td><td>{state}</td><td>{escape(attempt.get("error", "—"))}</td><td>{escape(attempt["receipt"])}</td></tr>')
    stages = []
    for row in report["resource_stages"]:
        stages.append(f'<tr><td>{escape(row["task"])} / {escape(row["stage"])}</td><td>{row["wall_ns"]/1e9:.4f}</td><td>{escape(row.get("reaped_children_cpu_seconds"))}</td><td>{escape(row.get("peak_tree_rss_bytes_sampled"))}</td><td>{row["exit_code"]}</td><td>{escape(shlex.join(row["argv"]))}<br>cwd: {escape(row["cwd"])}<br>{escape(row["receipt"])}</td></tr>')
    tokens = "".join(f'<tr><td>{escape(r["task"])}</td><td>{r["input_tokens"]}</td><td>{r["output_tokens"]}</td><td>{r["reasoning_tokens"]}</td><td>{r["cached_tokens"]}</td><td>{r["cache_write_tokens"]}</td><td>{r["cost_usd"]}</td></tr>' for r in calls)
    help_rows = "".join(f'<tr><td>{escape(shlex.join(r["argv"]))}</td><td>{escape(r["status"])}</td><td>{r["exit_code"]}</td><td>{escape(r["workflow_status"])}</td></tr>' for r in coverage.get("rows", []))
    totals = "".join(f'<li>{escape(key)}: {escape(value["observed_sum"])} ({value["known"]}/{value["total"]} recibos)</li>' for key, value in report["provider_totals"].items())
    lat = report["latency"]
    return f'''<!doctype html><html lang="pt-BR" data-theme="luxury"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Simplicio Loop — evidências do benchmark</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/daisyui@5.5.19/daisyui.css"><link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/daisyui@5.5.19/themes.css"><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4.2.4/dist/index.global.js"></script>
<style>*{{box-sizing:border-box}}body{{margin:0}}main{{max-width:1200px;margin:auto;padding:32px}}h1,h2{{font-family:Georgia,serif}}h1{{font-size:clamp(2rem,5vw,4rem);line-height:1.1}}h2{{font-size:1.8rem;margin:32px 0 16px}}p,li,td,th{{overflow-wrap:anywhere}}section{{margin:30px 0}}.columns{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:30px}}.columns>*{{min-width:0}}.mark{{display:grid;grid-template-columns:100px minmax(0,1fr) 100px;gap:10px;align-items:center;margin:12px 0;font-size:13px}}.track{{background:var(--color-base-300);height:12px}}.track>div{{height:100%;background:var(--color-primary)}}.tablewrap{{overflow:auto;max-width:100%}}td:last-child{{max-width:550px}}summary{{cursor:pointer;padding:16px 0}}@media(max-width:750px){{main{{padding:18px}}.columns{{grid-template-columns:minmax(0,1fr)}}.mark{{grid-template-columns:95px minmax(0,1fr) 90px}}}}@media print{{details{{display:block}}}}</style></head><body><main>
<p class="text-primary">SIMPLICIO LOOP · RELEASE 3.43.10 · 11 SET 2026</p><h1>Dez tarefas entregues.<br>O padrão ainda não está provado.</h1>
<p class="py-5">Piloto real com OpenRouter / deepseek/deepseek-v4.1-flash. GitHub, Jira e Azure DevOps foram simulados localmente; não houve tickets reais alterados.</p>
<div class="stats stats-vertical md:stats-horizontal w-full bg-base-200"><div class="stat"><div class="stat-title">Verificação consolidada</div><div class="stat-value">{passed}/10</div></div><div class="stat"><div class="stat-title">Chamadas pagas registradas</div><div class="stat-value">{len(calls)}</div></div><div class="stat"><div class="stat-title">Custo observado (USD)</div><div class="stat-value">{cost['observed_sum']:.7f}</div></div></div>
<section class="alert alert-warning"><span><strong>Sem vencedor elegível.</strong> Mapper → modelo → escrita nativa → verificação foi executado. Os braços equivalentes serial, Prism/wave e Fast não foram concluídos. Não usar estes números como economia comparativa ou capacidade máxima segura.</span></section>
<div class="columns"><section><h2>Latência por chamada</h2><p>Segundos até a resposta completa; inclui a proposta rejeitada. Não mede TTFT.</p>{bars('wall_seconds','s')}</section><section><h2>Custo por chamada</h2><p>USD informado pelo provedor; inclui a correção de formato.</p>{bars('cost_usd','USD')}</section></div>
<p>n={lat['n']} tentativas heterogêneas · p50 nearest-rank: {lat['p50_seconds_nearest_rank']:.6f}s · p99: {lat['p99_seconds_nearest_rank']:.6f}s. Com esta amostra, p99 é o máximo observado, não uma estimativa confiável da cauda. A soma das chamadas não é makespan do fluxo.</p>
<h2>Tokens e cache</h2><div class="tablewrap"><table class="table table-zebra"><thead><tr><th>Tarefa</th><th>Input</th><th>Output</th><th>Reasoning ⊂ output</th><th>Cache hit</th><th>Cache write</th><th>USD</th></tr></thead><tbody>{tokens}</tbody></table></div><ul class="list-disc pl-6 py-4">{totals}</ul><p>Zero é um campo observado; N/D significa não coletado. Cache local do Mapper não é cache de prompt do provedor. O primeiro piloto enviou contexto bruto maior; as tarefas são diferentes, portanto a diferença não prova economia.</p>
<h2>O que falhou e o que foi corrigido</h2><p>Uma resposta ADO-303 veio sem o objeto files e foi rejeitada antes de escrever; a reparação explícita foi contabilizada. GH-101 e GH-104 exigiram requisitos escritos no fixture e nova geração central do Mapper, sem baixar o gate. Isso mudou o cenário e impede tratar a sequência como um braço experimental congelado.</p><div class="tablewrap"><table class="table"><thead><tr><th>Tarefa</th><th>Resultado da tentativa</th><th>Motivo</th><th>Recibo local</th></tr></thead><tbody>{''.join(sections)}</tbody></table></div>
<h2>CPU, RAM e comandos reais</h2><p>CPU-segundos locais dos processos; RSS máximo amostrado em bytes, a cada 100 ms. Não são recursos do servidor do modelo. As primeiras duas tarefas não têm instrumentação completa. Cada estágio abaixo aponta para argv, diretório, saída e recibo exatos.</p><details><summary>Inspecionar {len(stages)} estágios medidos</summary><div class="tablewrap"><table class="table table-sm"><thead><tr><th>Etapa</th><th>Wall s</th><th>CPU s</th><th>RSS bytes</th><th>Exit</th><th>Comando e evidência</th></tr></thead><tbody>{''.join(stages)}</tbody></table></div></details>
<h2>Cobertura de comandos</h2><p>{len(coverage.get('rows',[]))} consultas de ajuda. <strong>Help funcionando não é fluxo validado.</strong> run/tick encontraram gates de storage, fence e watcher; os detalhes estão no protocolo. Prism é skill e scheduler; wave é uma barreira de reconciliação. Armar scratchpad não inicia agentes.</p><details><summary>Lista completa das consultas registradas</summary><div class="tablewrap"><table class="table table-sm"><thead><tr><th>Comando</th><th>Help</th><th>Exit</th><th>Fluxo na varredura</th></tr></thead><tbody>{help_rows}</tbody></table></div></details>
<h2>Conclusão que a evidência permite</h2><p>Manter Mapper central obrigatório, contexto certificado e limitado, dependências e verificação independente. Não promover configuração de concorrência como mais rápida ou barata sem braços equivalentes repetidos. O perfil economy recomenda flags MCP. O CLI já resolve --slots 0 automaticamente; a correção local alinha a API Python direta arm(slots=0), antes limitada a um slot. Isso não prova saturação segura em todos os comandos.</p><p>Check geral falhou e a árvore mudou durante um checkpoint paralelo; não é gate de release válido. O teste comportamental das dez entregas e os recibos de uso permanecem preservados.</p><footer class="border-t border-base-content/20 mt-10 pt-5">Fontes: summary.json e diagnostics.md no diretório do relatório; protocolo: docs/QUEUE_BENCHMARK_PROTOCOL.md. Design: Tailwind 4 + DaisyUI 5/luxury, fallback após inspeção do projeto sem design system de interface. Gerado {escape(report['generated_at'])}.</footer></main></body></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    report = summarize(args.root)
    coverage_path = Path(args.root) / "cli-coverage-bounded-20260911/coverage.json"
    coverage = json.loads(coverage_path.read_text()) if coverage_path.exists() else {}
    (output / "summary.json").write_text(json.dumps(report, indent=2))
    (output / "diagnostics.md").write_text(markdown(report))
    (output / "report.html").write_text(render_html(report, coverage))
    print(json.dumps({"output": str(output.resolve()), "provider_calls": len(report["provider_calls"]),
                      "provider_totals": report["provider_totals"], "latency": report["latency"]}, indent=2))


if __name__ == "__main__":
    main()
