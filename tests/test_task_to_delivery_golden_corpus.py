import json
from pathlib import Path

from simplicio_loop.task_contract import compile_many


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "contracts" / "task-to-delivery" / "golden-corpus.json"


def test_golden_corpus_covers_required_task_families_from_raw_markdown():
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    assert corpus["schema"] == "simplicio.task-to-delivery-golden-corpus/v1"
    cases = corpus["cases"]
    assert {item["category"] for item in cases} == {
        "frontend", "backend", "full-stack", "migration", "bug",
        "cli", "docs", "security", "multi-task-dag", "planes",
    }
    for item in cases:
        if item.get("source"):
            raw = (CORPUS.parent / item["source"]).read_text(encoding="utf-8")
        else:
            raw = item["task"]
        compiled = compile_many(raw, source_path=item["id"])
        assert compiled["task_count"] == (2 if item["category"] == "multi-task-dag" else 1)
        task = compiled["tasks"][0]
        if item["category"] == "planes":
            assert len(task["scenarios"]) == item["expected_scenarios"]
            assert len(task["rules"]) == item["expected_rules"]
        else:
            assert task["scenarios"]
            assert task["rules"]
