#!/usr/bin/env python3
"""Dependency-free mutation gate for the PLANES golden ordering contract."""
import argparse, hashlib, json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "contracts" / "task-to-delivery" / "fixtures" / "planes" / "dataset.json"
SCHEMA = "simplicio.golden-mutation-receipt/v1"

def _order(rows, mode):
    def key(item):
        kind = item["tipo"].lower(); structural = kind == "estrutural"
        group = 0 if structural else 1
        if mode == "structural-last": group = 1 - group
        if mode == "separate-types": group = {"estrutural": 0, "temporal": 1, "modelagem": 2}[kind]
        start = date.fromisoformat(item["inicio"]).toordinal() if not structural else date.min.toordinal()
        if mode == "descending-date" and not structural: start = -start
        plant = item["usina"].casefold()
        if mode == "plant-descending": plant = "".join(chr(255 - ord(c)) for c in plant)
        return plant, group, start, item["id"]
    return [item["id"] for item in sorted(rows, key=key)]

def run(dataset_path=DATASET):
    dataset_path = Path(dataset_path); data = json.loads(dataset_path.read_text(encoding="utf-8"))
    expected = data["expected_order"]
    descriptions = {"structural-last": "structural items are not first", "separate-types": "temporal/modelagem are split", "descending-date": "dates are descending", "plant-descending": "plants are not alphabetical"}
    mutations = [{"mutation": name, "description": desc, "observed_order": _order(data["rows"], name), "expected_order": expected} for name, desc in descriptions.items()]
    for item in mutations: item["rejected"] = item["observed_order"] != expected
    receipt = {"schema": SCHEMA, "dataset": str(dataset_path.relative_to(ROOT)).replace("\\", "/"), "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(), "mutations": mutations}
    receipt["status"] = "MEASURED" if all(i["rejected"] for i in mutations) else "UNVERIFIED"; receipt["match"] = receipt["status"] == "MEASURED"
    receipt["receipt_hash"] = hashlib.sha256(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return receipt

def main(argv=None):
    parser = argparse.ArgumentParser(); parser.add_argument("--dataset", type=Path, default=DATASET); parser.add_argument("--json", action="store_true"); args = parser.parse_args(argv)
    receipt = run(args.dataset.resolve()); print(json.dumps(receipt, ensure_ascii=False, indent=2) if args.json else "%s|golden mutations=%d" % (receipt["status"], len(receipt["mutations"])))
    return 0 if receipt["match"] else 1

if __name__ == "__main__": raise SystemExit(main())
