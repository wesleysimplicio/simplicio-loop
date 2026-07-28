"""Measured local microbenchmark for issue #810; no provider or LLM is invoked."""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from simplicio_loop.adaptive_context import (
    AdaptiveContextController, BudgetLimits, BudgetScope, ContextSpan,
    ExpansionReason, RegexTokenCounter,
)


def digest(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def execute(runs):
    counter = RegexTokenCounter()
    required = {"execute", "budget", "failure"}
    corpus = [
        ContextSpan("def execute task provider", "mapper:signature", "bench", priority=1),
        ContextSpan("budget limits soft hard", "mapper:rule", "bench", priority=2),
        ContextSpan("unrelated documentation prose " * 30, "mapper:doc", "bench", priority=50),
        ContextSpan("failure retry reconnect", "fast:g1", "bench", priority=3,
                    handle="fast://g1/failure"),
    ]
    samples = []
    adaptive_prompt = None
    for _ in range(runs):
        started = time.perf_counter_ns()
        controller = AdaptiveContextController(
            BudgetScope(BudgetLimits(8, 20)), stage="plan", task="bench",
            provider="offline", expected_revision="bench", counter=counter,
        )
        controller.seed(corpus[:3])
        controller.expand(
            corpus[3:], reason=ExpansionReason.FAILING_TEST,
            evidence="failure fixture requires retry fact",
        )
        adaptive_prompt = controller.prompt()
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    full_text = "\n".join(item.content for item in corpus)
    static_text = "\n".join(item.content for item in corpus[:2])
    adaptive_text = "\n".join(item["content"] for item in adaptive_prompt["context"])
    facts = lambda text: len({fact for fact in required if fact in text}) / len(required)
    git = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True, text=True, check=False,
    )
    payload = {
        "schema": "simplicio.loop-adaptive-context-benchmark/v1",
        "classification": "MEASURED_LOCAL",
        "counter": counter.name,
        "runs": runs,
        "tokens": {
            "full_context": counter.count(full_text),
            "static_soft_context": counter.count(static_text),
            "adaptive_context": counter.count(adaptive_text),
        },
        "quality": {
            "full_fact_recall": facts(full_text),
            "static_fact_recall": facts(static_text),
            "adaptive_fact_recall": facts(adaptive_text),
        },
        "latency_ms": {
            "p50": statistics.median(samples),
            "p95": sorted(samples)[max(0, int(len(samples) * .95) - 1)],
            "min": min(samples), "max": max(samples),
        },
        "provider_tokens": None,
        "provider_tokens_reason": "offline benchmark did not invoke a provider",
        "environment": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "source_sha": git.stdout.strip() if git.returncode == 0 else None,
            "source_sha_reason": None if git.returncode == 0 else "git SHA unavailable",
        },
        "local_llm": False,
    }
    payload["receipt_hash"] = digest(payload)
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=500)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.runs < 100:
        parser.error("--runs must be at least 100")
    payload = execute(args.runs)
    Path(args.output).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
