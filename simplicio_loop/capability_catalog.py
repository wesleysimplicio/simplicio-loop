"""Compact, language-neutral capability catalog for LLM hosts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

CATALOG_PATH = Path(__file__).with_name("_catalog") / "capabilities.json"

def load_catalog() -> dict[str, Any]:
    document = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    document["skills"] = sorted({item["skill"] for item in document["capabilities"]})
    return document

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover Simplicio capabilities")
    parser.add_argument("query", nargs="?")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    document = load_catalog()
    if args.query:
        needle = args.query.casefold()
        document["capabilities"] = [
            item for item in document["capabilities"]
            if needle in json.dumps(item, ensure_ascii=False).casefold()
        ]
        document["skills"] = sorted({item["skill"] for item in document["capabilities"]})
    print(json.dumps(document, ensure_ascii=False, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

