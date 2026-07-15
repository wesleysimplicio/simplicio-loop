import json
from datetime import date
from pathlib import Path

from simplicio_loop.task_contract import compile_many

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "contracts" / "task-to-delivery" / "fixtures" / "planes"


def _load_task():
    return (FIXTURE / "task.md").read_text(encoding="utf-8")


def _load_dataset():
    return json.loads((FIXTURE / "dataset.json").read_text(encoding="utf-8"))


def _parse_date(raw):
    return date.fromisoformat(raw) if raw else None


def _correct_order(rows):
    def key(item):
        tipo = item["tipo"].lower()
        grupo = 0 if tipo == "estrutural" else 1
        inicio = _parse_date(item["inicio"]) if grupo == 1 else date.min
        return (item["usina"].casefold(), grupo, inicio, item["id"])
    return [item["id"] for item in sorted(rows, key=key)]


def _mutant_structural_not_first(rows):
    def key(item):
        tipo = item["tipo"].lower()
        grupo = 1 if tipo == "estrutural" else 0
        inicio = _parse_date(item["inicio"]) if tipo != "estrutural" else date.min
        return (item["usina"].casefold(), grupo, inicio, item["id"])
    return [item["id"] for item in sorted(rows, key=key)]


def _mutant_separate_by_type(rows):
    def key(item):
        tipo = item["tipo"].lower()
        order = {"estrutural": 0, "temporal": 1, "modelagem": 2}[tipo]
        inicio = _parse_date(item["inicio"]) if tipo != "estrutural" else date.min
        return (item["usina"].casefold(), order, inicio, item["id"])
    return [item["id"] for item in sorted(rows, key=key)]


def _mutant_descending_date(rows):
    def key(item):
        tipo = item["tipo"].lower()
        grupo = 0 if tipo == "estrutural" else 1
        inicio = _parse_date(item["inicio"]) if grupo == 1 else date.min
        ordinal = -inicio.toordinal() if grupo == 1 else date.min.toordinal()
        return (item["usina"].casefold(), grupo, ordinal, item["id"])
    return [item["id"] for item in sorted(rows, key=key)]


def _mutant_usina_not_alphabetic(rows):
    def key(item):
        tipo = item["tipo"].lower()
        grupo = 0 if tipo == "estrutural" else 1
        inicio = _parse_date(item["inicio"]) if grupo == 1 else date.min
        return ("".join(chr(255 - ord(ch)) for ch in item["usina"].casefold()), grupo, inicio, item["id"])
    return [item["id"] for item in sorted(rows, key=key)]


def test_planes_fixture_starts_from_raw_markdown_and_preserves_all_scenarios_and_rules():
    payload = compile_many(_load_task(), source_path=str(FIXTURE / "task.md"))
    assert payload["task_count"] == 1
    task = payload["tasks"][0]
    assert len(task["scenarios"]) == 5
    assert [rule["id"] for rule in task["rules"]] == ["RN01", "RN02", "RN03"]
    assert task["scenarios"][4]["rule_refs"] == ["RN01", "RN02", "RN03"]


def test_planes_golden_order_matches_expected_fixture():
    dataset = _load_dataset()
    assert _correct_order(dataset["rows"]) == dataset["expected_order"]


def test_planes_mutants_are_rejected_by_the_golden_fixture():
    dataset = _load_dataset()
    expected = dataset["expected_order"]
    assert _mutant_structural_not_first(dataset["rows"]) != expected
    assert _mutant_separate_by_type(dataset["rows"]) != expected
    assert _mutant_descending_date(dataset["rows"]) != expected
    assert _mutant_usina_not_alphabetic(dataset["rows"]) != expected
