#!/usr/bin/env python3
"""Reproducible Monte Carlo projection for the simplicio-loop v4 architecture.

Every generated performance/cost number is SIMULATED. Repository metadata and
issue scope are observed inputs, not performance measurements. Edit
assumptions.json to calibrate the model with future measured receipts.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import statistics
import zipfile
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from matplotlib.ticker import FuncFormatter
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

PHASES = ("map", "context", "plan", "execute", "validate", "review", "delivery", "recovery")
PALETTE = ["#64748B", "#0EA5E9", "#8B5CF6", "#10B981", "#F59E0B"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assumptions", type=Path, default=Path(__file__).with_name("assumptions.json"))
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("output"))
    parser.add_argument("--repetitions", type=int)
    return parser.parse_args()


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def effective_parallelism(task_count: int, conflict_ratio: float, capacity: int) -> float:
    if task_count <= 1:
        return 1.0
    conflict_penalty = 1.0 + conflict_ratio * max(1.0, math.log2(task_count))
    return max(1.0, min(float(capacity), task_count / conflict_penalty))


def simulate(config: dict[str, Any], repetitions: int) -> pd.DataFrame:
    rng = np.random.default_rng(int(config["seed"]))
    uncertainty = config["uncertainty"]
    rows: list[dict[str, Any]] = []

    for workload_id, workload in config["workloads"].items():
        for scenario_id, scenario in config["scenarios"].items():
            parallelism = effective_parallelism(
                int(workload["task_count"]),
                float(workload["conflict_ratio"]),
                int(scenario["parallel_capacity"]),
            )
            for repetition in range(repetitions):
                row: dict[str, Any] = {
                    "benchmark_id": config["benchmark_id"],
                    "classification": "SIMULATED",
                    "seed": config["seed"],
                    "repetition": repetition,
                    "workload": workload_id,
                    "workload_label": workload["label"],
                    "scenario": scenario_id,
                    "scenario_label": scenario["label"],
                    "task_count": workload["task_count"],
                    "parallelism": parallelism,
                }

                total_seconds = 0.0
                for phase in PHASES:
                    base = float(workload["phases_seconds"][phase])
                    multiplier = float(scenario["phase_multipliers"][phase])
                    phase_parallel = parallelism if phase in {"execute", "validate", "review"} else 1.0
                    network = float(scenario["network_overhead"]) if phase in {"execute", "delivery"} else 1.0
                    value = base * multiplier * network / phase_parallel
                    if value > 0:
                        value *= float(rng.lognormal(0.0, float(uncertainty["phase_lognormal_sigma"])))
                    row[f"{phase}_seconds"] = value
                    total_seconds += value

                queue_wait = max(
                    0.0,
                    float(rng.normal(float(uncertainty["queue_jitter_seconds"]), 1.5))
                    * (1.0 + float(workload["conflict_ratio"])),
                )
                total_seconds += queue_wait
                row["queue_wait_seconds"] = queue_wait

                success_probability = min(
                    0.995,
                    max(0.5, float(workload["base_success_probability"]) + float(scenario["success_delta"])),
                )
                successful = bool(rng.random() <= success_probability)
                retries = 0 if successful else int(rng.integers(1, 4))
                retry_seconds = (
                    total_seconds
                    * float(uncertainty["failure_retry_time_fraction"])
                    * retries
                    * float(rng.uniform(0.8, 1.3))
                )
                total_seconds += retry_seconds

                tokens = (
                    float(workload["base_tokens"])
                    * float(scenario["token_multiplier"])
                    * float(rng.lognormal(0.0, float(uncertainty["token_lognormal_sigma"])))
                )
                retry_tokens = (
                    tokens
                    * float(uncertainty["failure_retry_token_fraction"])
                    * retries
                    * float(rng.uniform(0.8, 1.2))
                )
                tokens += retry_tokens
                llm_calls = max(
                    0,
                    int(
                        round(
                            float(workload["base_llm_calls"])
                            * float(scenario["llm_call_multiplier"])
                            * float(rng.uniform(0.9, 1.1))
                        )
                    ),
                )
                estimated_cost = (
                    tokens / 1_000_000.0 * float(config["cost_model"]["blended_usd_per_million_tokens"])
                )

                row.update(
                    {
                        "total_seconds": total_seconds,
                        "retries": retries,
                        "successful": successful,
                        "tokens": int(round(tokens)),
                        "llm_calls": llm_calls,
                        "estimated_cost_usd": estimated_cost,
                        "retry_seconds": retry_seconds,
                        "retry_tokens": int(round(retry_tokens)),
                        "throughput_tasks_per_hour": float(workload["task_count"]) / total_seconds * 3600.0,
                    }
                )
                rows.append(row)
    return pd.DataFrame(rows)


def quantile(values: pd.Series, q: float) -> float:
    return float(values.quantile(q))


def summarize(samples: pd.DataFrame) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    grouped = samples.groupby(
        ["scenario", "scenario_label", "workload", "workload_label"], sort=False
    )
    for keys, frame in grouped:
        scenario, scenario_label, workload, workload_label = keys
        records.append(
            {
                "classification": "SIMULATED",
                "scenario": scenario,
                "scenario_label": scenario_label,
                "workload": workload,
                "workload_label": workload_label,
                "runs": len(frame),
                "duration_p50_seconds": quantile(frame["total_seconds"], 0.5),
                "duration_p95_seconds": quantile(frame["total_seconds"], 0.95),
                "tokens_p50": quantile(frame["tokens"], 0.5),
                "tokens_p95": quantile(frame["tokens"], 0.95),
                "llm_calls_p50": quantile(frame["llm_calls"], 0.5),
                "throughput_p50_tasks_hour": quantile(frame["throughput_tasks_per_hour"], 0.5),
                "estimated_cost_p50_usd": quantile(frame["estimated_cost_usd"], 0.5),
                "completion_rate": float(frame["successful"].mean()),
                "retry_rate": float((frame["retries"] > 0).mean()),
            }
        )
    summary = pd.DataFrame(records)
    baseline = summary[summary["scenario"] == "S0_CURRENT_RELEASE"][
        ["workload", "duration_p50_seconds", "tokens_p50", "estimated_cost_p50_usd"]
    ].rename(
        columns={
            "duration_p50_seconds": "baseline_duration_p50_seconds",
            "tokens_p50": "baseline_tokens_p50",
            "estimated_cost_p50_usd": "baseline_cost_p50_usd",
        }
    )
    summary = summary.merge(baseline, on="workload", how="left")
    summary["duration_reduction_percent"] = (
        1.0 - summary["duration_p50_seconds"] / summary["baseline_duration_p50_seconds"]
    ) * 100.0
    summary["token_reduction_percent"] = (
        1.0 - summary["tokens_p50"] / summary["baseline_tokens_p50"]
    ) * 100.0
    summary["cost_reduction_percent"] = (
        1.0 - summary["estimated_cost_p50_usd"] / summary["baseline_cost_p50_usd"]
    ) * 100.0
    return summary


def simulate_quant(config: dict[str, Any], repetitions: int) -> pd.DataFrame:
    """Simulate comparable Q0/Q1/Q2 lanes on identical corpus inputs."""
    rng = np.random.default_rng(int(config["seed"]) + 198)
    quant = config["quant_benchmark"]
    uncertainty = quant["uncertainty"]
    rows: list[dict[str, Any]] = []
    for corpus_id, corpus in quant["corpora"].items():
        for lane_id, lane in quant["lanes"].items():
            for repetition in range(repetitions):
                query_ms = (
                    float(corpus["q0_query_ms"])
                    * float(lane["query_multiplier"])
                    * float(rng.lognormal(0.0, float(uncertainty["query_lognormal_sigma"])))
                    + float(lane["rerank_ms"])
                )
                rows.append(
                    {
                        "benchmark_id": config["benchmark_id"],
                        "classification": "SIMULATED",
                        "seed": config["seed"],
                        "repetition": repetition,
                        "corpus": corpus_id,
                        "corpus_label": corpus["label"],
                        "vectors": corpus["vectors"],
                        "lane": lane_id,
                        "lane_label": lane["label"],
                        "index_mb": float(corpus["q0_index_mb"])
                        * float(lane["index_multiplier"])
                        * float(rng.lognormal(0.0, float(uncertainty["size_lognormal_sigma"]))),
                        "rss_mb": float(corpus["q0_rss_mb"])
                        * float(lane["rss_multiplier"])
                        * float(rng.lognormal(0.0, float(uncertainty["rss_lognormal_sigma"]))),
                        "build_seconds": float(corpus["q0_build_seconds"])
                        * float(lane["build_multiplier"])
                        * float(rng.lognormal(0.0, float(uncertainty["build_lognormal_sigma"]))),
                        "query_ms": query_ms,
                        "rerank_ms": float(lane["rerank_ms"]),
                        "recall_at_10": float(
                            np.clip(
                                rng.normal(
                                    float(lane["recall_at_10"]),
                                    float(uncertainty["quality_sigma"]),
                                ),
                                0.0,
                                1.0,
                            )
                        ),
                        "ndcg_at_10": float(
                            np.clip(
                                rng.normal(
                                    float(lane["ndcg_at_10"]),
                                    float(uncertainty["quality_sigma"]),
                                ),
                                0.0,
                                1.0,
                            )
                        ),
                        "qps": 1000.0 / query_ms,
                    }
                )
    return pd.DataFrame(rows)


def summarize_quant(samples: pd.DataFrame) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    grouped = samples.groupby(["corpus", "corpus_label", "lane", "lane_label"], sort=False)
    for keys, frame in grouped:
        corpus, corpus_label, lane, lane_label = keys
        records.append(
            {
                "classification": "SIMULATED",
                "corpus": corpus,
                "corpus_label": corpus_label,
                "lane": lane,
                "lane_label": lane_label,
                "runs": len(frame),
                "index_p50_mb": quantile(frame["index_mb"], 0.5),
                "rss_p50_mb": quantile(frame["rss_mb"], 0.5),
                "build_p50_seconds": quantile(frame["build_seconds"], 0.5),
                "query_p50_ms": quantile(frame["query_ms"], 0.5),
                "query_p95_ms": quantile(frame["query_ms"], 0.95),
                "qps_p50": quantile(frame["qps"], 0.5),
                "recall_at_10_mean": float(frame["recall_at_10"].mean()),
                "ndcg_at_10_mean": float(frame["ndcg_at_10"].mean()),
            }
        )
    summary = pd.DataFrame(records)
    q0 = summary[summary["lane"] == "Q0_FULL_PRECISION"][
        ["corpus", "index_p50_mb", "rss_p50_mb", "query_p50_ms"]
    ].rename(
        columns={
            "index_p50_mb": "q0_index_p50_mb",
            "rss_p50_mb": "q0_rss_p50_mb",
            "query_p50_ms": "q0_query_p50_ms",
        }
    )
    summary = summary.merge(q0, on="corpus", how="left")
    summary["index_reduction_percent"] = (1.0 - summary["index_p50_mb"] / summary["q0_index_p50_mb"]) * 100
    summary["rss_reduction_percent"] = (1.0 - summary["rss_p50_mb"] / summary["q0_rss_p50_mb"]) * 100
    summary["query_reduction_percent"] = (
        1.0 - summary["query_p50_ms"] / summary["q0_query_p50_ms"]
    ) * 100
    return summary


def setup_charts() -> None:
    sns.set_theme(style="whitegrid", context="notebook")
    plt.rcParams.update(
        {
            "figure.dpi": 150,
            "savefig.dpi": 180,
            "font.family": "DejaVu Sans",
            "axes.titleweight": "bold",
            "axes.titlesize": 14,
            "axes.labelsize": 10,
            "legend.frameon": False,
        }
    )


def save_figure(fig: plt.Figure, path: Path) -> None:
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def create_charts(
    samples: pd.DataFrame,
    summary: pd.DataFrame,
    quant_summary: pd.DataFrame,
    config: dict[str, Any],
    out: Path,
) -> list[Path]:
    setup_charts()
    charts = out / "charts"
    charts.mkdir(parents=True, exist_ok=True)
    order = list(config["scenarios"])
    labels = {key: value["label"] for key, value in config["scenarios"].items()}
    workload_order = list(config["workloads"])
    workload_labels = {key: value["label"] for key, value in config["workloads"].items()}
    paths: list[Path] = []

    cross = summary[summary["workload"] == "cross_module"].set_index("scenario").loc[order]
    fig, ax = plt.subplots(figsize=(10.8, 5.4))
    values = cross["duration_p50_seconds"] / 60.0
    errors = (cross["duration_p95_seconds"] - cross["duration_p50_seconds"]) / 60.0
    ax.bar(range(len(order)), values, yerr=errors, color=PALETTE, capsize=4)
    ax.set_xticks(range(len(order)), [labels[key].replace(" - ", "\n") for key in order])
    ax.set_ylabel("Minutes")
    ax.set_title("Projected duration for a cross-module change")
    ax.text(
        0.99,
        0.98,
        "SIMULATED - bars: p50, whiskers: p95-p50",
        transform=ax.transAxes,
        ha="right",
        va="top",
        fontsize=9,
        color="#475569",
    )
    path = charts / "01_duration_bar.png"
    save_figure(fig, path)
    paths.append(path)

    projected = summary[summary["scenario"] == "S4_PROJECTED_DISTRIBUTED"].set_index("workload").loc[
        workload_order
    ]
    fig, ax = plt.subplots(figsize=(10.8, 5.6))
    vals = projected["token_reduction_percent"]
    ax.barh(range(len(workload_order)), vals, color="#10B981")
    ax.set_yticks(range(len(workload_order)), [workload_labels[key] for key in workload_order])
    ax.set_xlabel("Reduction versus S0 (%)")
    ax.set_xlim(0, 100)
    ax.set_title("Projected token reduction by workload")
    for index, value in enumerate(vals):
        ax.text(value + 1, index, f"{value:.1f}%", va="center")
    path = charts / "02_token_reduction_horizontal.png"
    save_figure(fig, path)
    paths.append(path)

    slots = np.array([1, 2, 4, 8, 16, 32])
    fig, ax = plt.subplots(figsize=(10.8, 5.6))
    for scenario_id, color in zip(order, PALETTE):
        scenario = config["scenarios"][scenario_id]
        capacity = float(scenario["parallel_capacity"])
        conflict = float(config["workloads"]["conflict_100"]["conflict_ratio"])
        effective = np.minimum(slots, capacity) / (1.0 + conflict * np.log2(slots + 1.0))
        normalized = effective / effective[0]
        ax.plot(slots, normalized, marker="o", linewidth=2.2, label=labels[scenario_id], color=color)
    ax.set_xscale("log", base=2)
    ax.set_xticks(slots, [str(item) for item in slots])
    ax.set_xlabel("Available worker slots")
    ax.set_ylabel("Normalized throughput (1 slot = 1.0)")
    ax.set_title("Projected throughput scaling under conflict")
    ax.legend(ncol=2, fontsize=8)
    path = charts / "03_throughput_line.png"
    save_figure(fig, path)
    paths.append(path)

    fig, ax = plt.subplots(figsize=(10.8, 6.0))
    plot = summary.copy()
    plot["duration_minutes"] = plot["duration_p50_seconds"] / 60.0
    sns.scatterplot(
        data=plot,
        x="duration_minutes",
        y="estimated_cost_p50_usd",
        hue="scenario_label",
        style="workload_label",
        s=110,
        palette=PALETTE,
        ax=ax,
    )
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Projected p50 duration (minutes, log scale)")
    ax.set_ylabel("Normalized token cost (USD, log scale)")
    ax.set_title("Time-cost frontier across scenarios and workloads")
    ax.legend(bbox_to_anchor=(1.02, 1), loc="upper left", fontsize=7)
    path = charts / "04_cost_duration_scatter.png"
    save_figure(fig, path)
    paths.append(path)

    cross_samples = samples[samples["workload"] == "cross_module"]
    phase_means = (
        cross_samples.groupby(["scenario", "scenario_label"])[[f"{phase}_seconds" for phase in PHASES]]
        .median()
        .reset_index()
        .set_index("scenario")
        .loc[order]
    )
    fig, ax = plt.subplots(figsize=(10.8, 6.0))
    bottom = np.zeros(len(order))
    phase_colors = sns.color_palette("crest", n_colors=len(PHASES))
    for phase, color in zip(PHASES, phase_colors):
        values = phase_means[f"{phase}_seconds"].to_numpy() / 60.0
        ax.bar(range(len(order)), values, bottom=bottom, label=phase, color=color)
        bottom += values
    ax.set_xticks(range(len(order)), [labels[key].replace(" - ", "\n") for key in order])
    ax.set_ylabel("Median phase minutes")
    ax.set_title("Where projected time is spent")
    ax.legend(ncol=4, fontsize=8, loc="upper right")
    path = charts / "05_phase_mix_stacked.png"
    save_figure(fig, path)
    paths.append(path)

    recovery = samples[samples["workload"] == "recovery_fault"].copy()
    recovery["recovery_minutes"] = (recovery["recovery_seconds"] + recovery["retry_seconds"]) / 60.0
    fig, ax = plt.subplots(figsize=(10.8, 5.8))
    sns.boxplot(
        data=recovery,
        x="scenario_label",
        y="recovery_minutes",
        hue="scenario_label",
        order=[labels[key] for key in order],
        palette=PALETTE,
        showfliers=False,
        legend=False,
        ax=ax,
    )
    ax.set_xlabel("")
    ax.set_ylabel("Recovery minutes")
    ax.set_title("Projected recovery distribution after an injected crash")
    ax.tick_params(axis="x", rotation=18)
    path = charts / "06_recovery_box.png"
    save_figure(fig, path)
    paths.append(path)

    corpus_order = list(config["quant_benchmark"]["corpora"])
    lane_order = list(config["quant_benchmark"]["lanes"])
    quant_labels = {
        key: value["label"] for key, value in config["quant_benchmark"]["lanes"].items()
    }
    quant_palette = ["#64748B", "#0EA5E9", "#F59E0B", "#10B981"]

    fig, ax = plt.subplots(figsize=(10.8, 5.8))
    for lane_id, color in zip(lane_order, quant_palette):
        lane_frame = quant_summary[quant_summary["lane"] == lane_id].set_index("corpus").loc[corpus_order]
        ax.plot(
            range(len(corpus_order)),
            lane_frame["query_p50_ms"],
            marker="o",
            linewidth=2.2,
            label=quant_labels[lane_id],
            color=color,
        )
    ax.set_xticks(
        range(len(corpus_order)),
        [config["quant_benchmark"]["corpora"][key]["label"] for key in corpus_order],
    )
    ax.set_ylabel("Query latency p50 (ms)")
    ax.set_title("Q0/Q1/Q2 projected query latency")
    ax.legend(fontsize=8)
    path = charts / "07_quant_latency_line.png"
    save_figure(fig, path)
    paths.append(path)

    million = quant_summary[quant_summary["corpus"] == "C1M"].set_index("lane").loc[lane_order]
    positions = np.arange(len(lane_order))
    width = 0.35
    fig, ax = plt.subplots(figsize=(10.8, 5.8))
    ax.bar(
        positions - width / 2,
        million["index_reduction_percent"],
        width,
        label="Index reduction",
        color="#0EA5E9",
    )
    ax.bar(
        positions + width / 2,
        million["rss_reduction_percent"],
        width,
        label="RSS reduction",
        color="#8B5CF6",
    )
    ax.set_xticks(positions, [quant_labels[key].replace(" ", "\n", 1) for key in lane_order])
    ax.set_ylabel("Reduction versus Q0 (%)")
    ax.set_ylim(0, 90)
    ax.set_title("Projected 1m-vector footprint reduction")
    ax.legend()
    path = charts / "08_quant_footprint_bar.png"
    save_figure(fig, path)
    paths.append(path)

    fig, ax = plt.subplots(figsize=(10.8, 5.8))
    for lane_id, color in zip(lane_order, quant_palette):
        row = million.loc[lane_id]
        ax.scatter(
            row["query_p50_ms"],
            row["recall_at_10_mean"],
            s=max(90, row["index_p50_mb"] / 3),
            color=color,
            alpha=0.8,
            edgecolor="white",
            linewidth=1,
            label=quant_labels[lane_id],
        )
        ax.annotate(
            quant_labels[lane_id].split(" ")[0],
            (row["query_p50_ms"], row["recall_at_10_mean"]),
            xytext=(5, 5),
            textcoords="offset points",
        )
    ax.set_xlabel("Query latency p50 (ms)")
    ax.set_ylabel("Recall@10")
    ax.set_ylim(0.915, 1.0)
    ax.set_title("Projected quality-speed trade-off at 1m vectors")
    ax.legend(fontsize=8, loc="lower right")
    path = charts / "09_quant_quality_tradeoff_scatter.png"
    save_figure(fig, path)
    paths.append(path)

    return paths


def pct(value: float) -> str:
    return f"{value:.1f}%"


def seconds_human(value: float) -> str:
    if value < 120:
        return f"{value:.0f} s"
    if value < 7200:
        return f"{value / 60.0:.1f} min"
    return f"{value / 3600.0:.1f} h"


def write_markdown(
    config: dict[str, Any], summary: pd.DataFrame, quant_summary: pd.DataFrame, out: Path
) -> Path:
    target = summary[summary["scenario"] == "S4_PROJECTED_DISTRIBUTED"]
    lines = [
        "# Simplicio Loop 4.0 - Simulated Benchmark Projection",
        "",
        "> Classification: **SIMULATED**. This document is not evidence of measured production gains.",
        "",
        f"- Baseline release metadata: `{config['baseline_release']}`",
        f"- Baseline main SHA observed during design: `{config['baseline_main_sha']}`",
        f"- Projection: `{config['projection_name']}`",
        f"- Seed: `{config['seed']}`",
        f"- Repetitions per scenario/workload: `{int(summary['runs'].iloc[0])}`",
        "",
        "## Projected S4 outcomes",
        "",
        "| Workload | p50 duration | p95 duration | Token reduction | Completion rate | Throughput |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for _, row in target.iterrows():
        lines.append(
            f"| {row['workload_label']} | {seconds_human(row['duration_p50_seconds'])} | "
            f"{seconds_human(row['duration_p95_seconds'])} | {pct(row['token_reduction_percent'])} | "
            f"{pct(row['completion_rate'] * 100)} | {row['throughput_p50_tasks_hour']:.2f} tasks/h |"
        )
    million = quant_summary[quant_summary["corpus"] == "C1M"]
    lines += [
        "",
        "## Projected quantization outcomes at 1m vectors",
        "",
        "> Q0/Q1/Q2 results are also **SIMULATED**. Q2a isolates 4-bit retrieval; Q2b adds integral re-ranking.",
        "",
        "| Lane | Query p50 | Index | Index reduction | RSS reduction | Recall@10 | nDCG@10 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for _, row in million.iterrows():
        lines.append(
            f"| {row['lane_label']} | {row['query_p50_ms']:.2f} ms | {row['index_p50_mb']:.0f} MB | "
            f"{pct(row['index_reduction_percent'])} | {pct(row['rss_reduction_percent'])} | "
            f"{row['recall_at_10_mean']:.3f} | {row['ndcg_at_10_mean']:.3f} |"
        )
    lines += [
        "",
        "## Method",
        "",
        "A deterministic Monte Carlo model samples phase duration, token volume, failures and retries.",
        "Parallel capacity is bounded by scenario capacity and reduced by workload conflict ratio.",
        "The quant matrix uses identical corpus inputs across Q0 full precision, Q1 int8, "
        "Q2a TurboQuant 4-bit and Q2b 4-bit with integral re-ranking.",
        "Every coefficient is editable in `assumptions.json`; rerun the script to regenerate all outputs.",
        "",
        "## Interpretation rules",
        "",
        "- `MEASURED`: repository release metadata only.",
        "- `OBSERVED`: architecture and issue scope found in the repository.",
        "- `SIMULATED`: every duration, token, cost, throughput and completion result.",
        "- `TARGET`: desired release behavior, not proof.",
        "",
        "## Important limitations",
        "",
        "- Phase baselines are calibration assumptions, not timings from production receipts.",
        "- The blended USD token rate is a normalization input, not a provider price claim.",
        "- Network, repository shape, model behavior and test suites can dominate real results.",
        "- Quant quality values are assumptions until measured on identical corpus, queries, embeddings and hardware.",
        "- The simulation must be replaced progressively with measured distributions from issue #816.",
        "",
        "## Reproduce",
        "",
        "```bash",
        "python3 run_projection.py --assumptions assumptions.json --output output",
        "```",
        "",
        "## Sources",
        "",
    ]
    lines.extend(f"- [{item['label']}]({item['url']})" for item in config["sources"])
    path = out / "SIMULATED_BENCHMARK_REPORT.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def register_fonts() -> None:
    regular = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    bold = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    pdfmetrics.registerFont(TTFont("DejaVu", regular))
    pdfmetrics.registerFont(TTFont("DejaVu-Bold", bold))


def footer(canvas: Any, document: Any) -> None:
    canvas.saveState()
    canvas.setFont("DejaVu", 8)
    canvas.setFillColor(colors.HexColor("#64748B"))
    canvas.drawString(18 * mm, 10 * mm, "SIMULATED - not a measured production benchmark")
    canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {document.page}")
    canvas.restoreState()


def create_pdf(
    config: dict[str, Any],
    summary: pd.DataFrame,
    quant_summary: pd.DataFrame,
    chart_paths: list[Path],
    out: Path,
) -> Path:
    register_fonts()
    pdf_path = out / "simplicio-loop-v4-simulated-benchmark.pdf"
    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title="Simplicio Loop 4.0 - Simulated Benchmark Projection",
        author="Simplicio benchmark projection",
    )
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="TitleDV",
            parent=styles["Title"],
            fontName="DejaVu-Bold",
            fontSize=24,
            leading=29,
            textColor=colors.HexColor("#0F172A"),
            spaceAfter=10,
        )
    )
    styles.add(
        ParagraphStyle(
            name="H1DV",
            parent=styles["Heading1"],
            fontName="DejaVu-Bold",
            fontSize=16,
            leading=20,
            textColor=colors.HexColor("#0F766E"),
            spaceBefore=8,
            spaceAfter=8,
        )
    )
    styles.add(
        ParagraphStyle(
            name="BodyDV",
            parent=styles["BodyText"],
            fontName="DejaVu",
            fontSize=9.5,
            leading=14,
            textColor=colors.HexColor("#334155"),
        )
    )
    styles.add(
        ParagraphStyle(
            name="Callout",
            parent=styles["BodyText"],
            fontName="DejaVu-Bold",
            fontSize=11,
            leading=16,
            textColor=colors.HexColor("#991B1B"),
            backColor=colors.HexColor("#FEE2E2"),
            borderPadding=8,
            borderColor=colors.HexColor("#FCA5A5"),
            borderWidth=0.5,
        )
    )
    story: list[Any] = []
    story.append(Spacer(1, 16 * mm))
    story.append(Paragraph("Simplicio Loop 4.0", styles["TitleDV"]))
    story.append(Paragraph("Simulated benchmark projection", styles["H1DV"]))
    story.append(Spacer(1, 5 * mm))
    story.append(
        Paragraph(
            "SIMULATED: no performance, cost, token or quality result in this report is a measured production claim.",
            styles["Callout"],
        )
    )
    story.append(Spacer(1, 8 * mm))
    metadata = [
        ["Baseline release", config["baseline_release"]],
        ["Projection", config["projection_name"]],
        ["Baseline main SHA", config["baseline_main_sha"][:12]],
        ["Seed", str(config["seed"])],
        ["Runs per scenario/workload", str(int(summary["runs"].iloc[0]))],
        ["Generated classification", "SIMULATED"],
    ]
    table = Table(metadata, colWidths=[58 * mm, 103 * mm])
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "DejaVu"),
                ("FONTNAME", (0, 0), (0, -1), "DejaVu-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#334155")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#ECFDF5")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CBD5E1")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 8 * mm))
    story.append(
        Paragraph(
            "Purpose: estimate the potential shape of a release in which every child of epic #801 is complete, "
            "while preserving multi-device, multi-LLM, source reporting, recovery and evidence gates.",
            styles["BodyDV"],
        )
    )
    story.append(PageBreak())

    story.append(Paragraph("Executive projection", styles["H1DV"]))
    target = summary[summary["scenario"] == "S4_PROJECTED_DISTRIBUTED"]
    data = [["Workload", "p50", "p95", "Token reduction", "Completion"]]
    for _, row in target.iterrows():
        data.append(
            [
                row["workload_label"],
                seconds_human(row["duration_p50_seconds"]),
                seconds_human(row["duration_p95_seconds"]),
                pct(row["token_reduction_percent"]),
                pct(row["completion_rate"] * 100),
            ]
        )
    result_table = Table(data, colWidths=[61 * mm, 25 * mm, 25 * mm, 30 * mm, 25 * mm], repeatRows=1)
    result_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "DejaVu"),
                ("FONTNAME", (0, 0), (-1, 0), "DejaVu-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8.2),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0F766E")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CBD5E1")),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(result_table)
    story.append(Spacer(1, 6 * mm))
    story.append(
        Paragraph(
            "These values are hypotheses produced by the coefficients in assumptions.json. They indicate what to test, "
            "not what the current product has already achieved.",
            styles["BodyDV"],
        )
    )

    story.append(PageBreak())
    story.append(Paragraph("Q0/Q1/Q2 quantization projection", styles["H1DV"]))
    story.append(
        Paragraph(
            "All values below are SIMULATED. Q2a isolates 4-bit retrieval and Q2b measures the projected "
            "effect of integral re-ranking. The promotion gate requires identical corpus, queries, embeddings "
            "and hardware across every lane.",
            styles["BodyDV"],
        )
    )
    story.append(Spacer(1, 5 * mm))
    million = quant_summary[quant_summary["corpus"] == "C1M"]
    quant_data = [["Lane", "Query p50", "Index", "Index red.", "RSS red.", "Recall@10"]]
    for _, row in million.iterrows():
        quant_data.append(
            [
                row["lane_label"],
                f"{row['query_p50_ms']:.2f} ms",
                f"{row['index_p50_mb']:.0f} MB",
                pct(row["index_reduction_percent"]),
                pct(row["rss_reduction_percent"]),
                f"{row['recall_at_10_mean']:.3f}",
            ]
        )
    quant_table = Table(
        quant_data,
        colWidths=[55 * mm, 24 * mm, 23 * mm, 23 * mm, 23 * mm, 23 * mm],
        repeatRows=1,
    )
    quant_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "DejaVu"),
                ("FONTNAME", (0, 0), (-1, 0), "DejaVu-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 7.8),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#7C3AED")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CBD5E1")),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(quant_table)
    story.append(Spacer(1, 5 * mm))
    story.append(
        Paragraph(
            "Decision lane: Q2b is promoted only if measured recall/nDCG parity survives while retaining "
            "the projected footprint and latency advantage. Otherwise the safe fallback is Q1 or Q0.",
            styles["BodyDV"],
        )
    )

    for index, path in enumerate(chart_paths):
        story.append(PageBreak())
        story.append(Paragraph(path.stem.replace("_", " ").title(), styles["H1DV"]))
        image = Image(str(path), width=172 * mm, height=92 * mm)
        story.append(image)
        story.append(Spacer(1, 4 * mm))
        captions = [
            "Duration combines phase-level uncertainty, queue jitter, retries and scenario-specific acceleration.",
            "Token reduction is relative to the simulated S0 baseline for the same workload.",
            "Scaling is capacity-bounded and penalized by the conflict ratio of the 100-issue workload.",
            "Cost uses an editable blended token-rate normalization and is not a provider price quote.",
            "Phase composition shows where optimization remains valuable after context and scheduling improvements.",
            "Recovery includes checkpoint replay plus retry work after an injected crash.",
            "Quant latency compares Q0, Q1, raw Q2 and re-ranked Q2 across the same projected corpus sizes.",
            "Footprint reduction normalizes index size and resident memory against Q0 at one million vectors.",
            "The quant frontier combines p50 latency and Recall@10; marker area represents projected index size.",
        ]
        story.append(Paragraph(captions[index], styles["BodyDV"]))

    story.append(PageBreak())
    story.append(Paragraph("Method and assumptions", styles["H1DV"]))
    method_paragraphs = [
        "The simulator executes a deterministic Monte Carlo model. It samples lognormal phase duration and token volume, "
        "then applies queue jitter, a workload success probability and retry overhead.",
        "Parallelism is the lower of task count and scenario capacity, reduced by a conflict penalty. Only execute, "
        "validate and review phases receive that parallel speedup.",
        "S0 through S4 are architectural scenarios. S4 represents the projected combination of a resident Fast layer, "
        "Python control plane, Rust hot paths, bounded multi-device workers and heterogeneous runtime routing.",
        "The quantization model evaluates Q0 full precision, Q1 int8, Q2a TurboQuant 4-bit and Q2b 4-bit with "
        "integral re-ranking. Its quality priors are deliberately separated from measured evidence.",
        "The simulation does not model every repository topology, model provider, rate limit, CI queue or human approval. "
        "Real receipts must progressively replace each assumed distribution.",
    ]
    for paragraph in method_paragraphs:
        story.append(Paragraph(paragraph, styles["BodyDV"]))
        story.append(Spacer(1, 3 * mm))

    scenario_data = [["Scenario", "Tokens", "LLM calls", "Slots", "Success delta"]]
    for scenario in config["scenarios"].values():
        scenario_data.append(
            [
                scenario["label"],
                f"{scenario['token_multiplier']:.2f}x",
                f"{scenario['llm_call_multiplier']:.2f}x",
                str(scenario["parallel_capacity"]),
                f"+{scenario['success_delta'] * 100:.0f} pts",
            ]
        )
    scenario_table = Table(scenario_data, colWidths=[67 * mm, 24 * mm, 27 * mm, 18 * mm, 29 * mm])
    scenario_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "DejaVu"),
                ("FONTNAME", (0, 0), (-1, 0), "DejaVu-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8.2),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E293B")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CBD5E1")),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    story.append(Spacer(1, 4 * mm))
    story.append(scenario_table)

    story.append(PageBreak())
    story.append(Paragraph("Validation plan", styles["H1DV"]))
    validation = [
        "Replace S0 phase assumptions with receipts from a clean 3.38.5 artifact.",
        "Measure S1 on the same hardware, repository, provider, model and task inputs.",
        "Recalibrate each phase after the corresponding #801 child issue lands.",
        "Run at least 10 measured repetitions per lane; keep warmup separate.",
        "For quant lanes, freeze corpus, queries, embeddings, hardware and candidate count.",
        "Record Q2 both before and after integral re-ranking; compare Recall@10, nDCG@10 and MRR.",
        "Report p50, p95, dispersion, quality and acceptance-criteria coverage together.",
        "Publish null plus a reason when tokens, cost, CPU, RSS or I/O cannot be observed.",
        "Never promote a simulated value into README or release claims.",
    ]
    for item in validation:
        story.append(Paragraph(f"- {item}", styles["BodyDV"]))
        story.append(Spacer(1, 2 * mm))
    story.append(Spacer(1, 5 * mm))
    story.append(Paragraph("Reproduction command", styles["H1DV"]))
    story.append(
        Paragraph(
            "python3 run_projection.py --assumptions assumptions.json --output output",
            ParagraphStyle(
                name="CodeDV",
                fontName="DejaVu",
                fontSize=8.5,
                leading=12,
                backColor=colors.HexColor("#F1F5F9"),
                borderPadding=7,
            ),
        )
    )

    story.append(PageBreak())
    story.append(Paragraph("Sources and classification", styles["H1DV"]))
    for source in config["sources"]:
        story.append(Paragraph(f"- {source['label']}: {source['url']}", styles["BodyDV"]))
        story.append(Spacer(1, 2 * mm))
    story.append(Spacer(1, 5 * mm))
    story.append(
        Paragraph(
            "Final classification: this report is SIMULATED. It is a reusable planning and calibration artifact, "
            "not proof that version 4.0 has delivered the projected gains.",
            styles["Callout"],
        )
    )
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return pdf_path


def create_reusable_package(source_dir: Path, out: Path) -> tuple[Path, Path]:
    included = [
        source_dir / "README.md",
        source_dir / "assumptions.json",
        source_dir / "run_projection.py",
        out / "SIMULATED_BENCHMARK_REPORT.md",
        out / "simplicio-loop-v4-simulated-benchmark.pdf",
        out / "simulated_raw_samples.csv.gz",
        out / "simulated_summary.csv",
        out / "quant_simulated_raw_samples.csv.gz",
        out / "quant_simulated_summary.csv",
        out / "simulation_manifest.json",
        *sorted((out / "charts").glob("*.png")),
    ]
    checksum_lines = []
    for path in included:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        checksum_lines.append(f"{digest}  {path.relative_to(source_dir)}")
    checksum_path = out / "SHA256SUMS"
    checksum_path.write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")

    archive_path = out / "simplicio-loop-v4-simulated-benchmark-package.zip"
    archive_items = included + [checksum_path]
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in archive_items:
            arcname = str(Path("benchmark_projection") / path.relative_to(source_dir))
            info = zipfile.ZipInfo(arcname, date_time=(2026, 7, 28, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return checksum_path, archive_path


def main() -> None:
    args = parse_args()
    config = load_config(args.assumptions)
    repetitions = int(args.repetitions or config["default_repetitions"])
    args.output.mkdir(parents=True, exist_ok=True)

    samples = simulate(config, repetitions)
    summary = summarize(samples)
    quant_samples = simulate_quant(config, repetitions)
    quant_summary = summarize_quant(quant_samples)

    raw_csv = args.output / "simulated_raw_samples.csv"
    samples.to_csv(raw_csv, index=False, quoting=csv.QUOTE_MINIMAL)
    with raw_csv.open("rb") as source, (args.output / "simulated_raw_samples.csv.gz").open("wb") as compressed:
        with gzip.GzipFile(fileobj=compressed, mode="wb", mtime=0) as target:
            target.write(source.read())
    raw_csv.unlink()
    summary.to_csv(args.output / "simulated_summary.csv", index=False)
    quant_raw_csv = args.output / "quant_simulated_raw_samples.csv"
    quant_samples.to_csv(quant_raw_csv, index=False, quoting=csv.QUOTE_MINIMAL)
    with quant_raw_csv.open("rb") as source, (
        args.output / "quant_simulated_raw_samples.csv.gz"
    ).open("wb") as compressed:
        with gzip.GzipFile(fileobj=compressed, mode="wb", mtime=0) as target:
            target.write(source.read())
    quant_raw_csv.unlink()
    quant_summary.to_csv(args.output / "quant_simulated_summary.csv", index=False)
    (args.output / "simulation_manifest.json").write_text(
        json.dumps(
            {
                "schema": "simplicio.loop.simulation-manifest/v1",
                "benchmark_id": config["benchmark_id"],
                "classification": "SIMULATED",
                "seed": config["seed"],
                "repetitions_per_lane": repetitions,
                "scenarios": list(config["scenarios"]),
                "workloads": list(config["workloads"]),
                "sample_rows": len(samples),
                "quant_lanes": list(config["quant_benchmark"]["lanes"]),
                "quant_corpora": list(config["quant_benchmark"]["corpora"]),
                "quant_sample_rows": len(quant_samples),
                "total_sample_rows": len(samples) + len(quant_samples),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    charts = create_charts(samples, summary, quant_summary, config, args.output)
    write_markdown(config, summary, quant_summary, args.output)
    create_pdf(config, summary, quant_summary, charts, args.output)
    checksum_path, archive_path = create_reusable_package(args.assumptions.parent, args.output)
    print(
        json.dumps(
            {
                "status": "ok",
                "output": str(args.output),
                "rows": len(samples),
                "quant_rows": len(quant_samples),
                "total_rows": len(samples) + len(quant_samples),
                "checksums": str(checksum_path),
                "package": str(archive_path),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
