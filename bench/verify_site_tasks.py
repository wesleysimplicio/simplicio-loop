#!/usr/bin/env python3
"""Independent filesystem verifier for the five PAGE benchmark tasks."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def _read(root: Path, relative: str) -> str:
    path = root / relative
    if not path.is_file():
        raise AssertionError(f"missing file: {relative}")
    return path.read_text(encoding="utf-8")


def _require(text: str, pattern: str, label: str) -> None:
    if not re.search(pattern, text, flags=re.I | re.S):
        raise AssertionError(f"missing {label}")


def verify(task: str, root: Path) -> None:
    index = _read(root, "site/index.html")
    if task == "PAGE-101":
        css = _read(root, "site/styles.css")
        _require(index, r"<!doctype\s+html>", "HTML doctype")
        _require(index, r"<html\b[^>]*\blang=[\"']en[\"']", "English language")
        _require(index, r"<meta\b[^>]*name=[\"']viewport[\"']", "viewport metadata")
        _require(index, r"<title>[^<]*simplicio", "Simplicio title")
        _require(index, r"<h1\b[^>]*>[^<]*simplicio", "Simplicio heading")
        _require(index, r"<header\b.*?<main\b.*?<footer\b", "semantic page structure")
        _require(css, r"@media\s*\(", "responsive media rule")
    elif task == "PAGE-102":
        _require(index, r"<nav\b[^>]*>.*?</nav>", "primary navigation")
        for label, destination in (("Home", "index\\.html"), ("About", "about\\.html"), ("Contact", r"#contact")):
            _require(index, rf"<a\b[^>]*href=[\"'][^\"']*{destination}[^\"']*[\"'][^>]*>\s*{label}\s*</a>", f"{label} navigation link")
    elif task == "PAGE-103":
        css = _read(root, "site/styles.css")
        _require(index, r"<link\b[^>]*rel=[\"']stylesheet[\"'][^>]*href=[\"'](?:\.\/)?styles\.css[\"']", "stylesheet link")
        _require(css, r"(?:^|[;}])\s*[^{}]+\{", "base stylesheet rules")
        _require(css, r"@media\s*\(", "responsive media query")
    elif task == "PAGE-104":
        _require(index, r"<section\b[^>]*id=[\"']contact[\"']", "contact section")
        _require(index, r"<form\b[^>]*>.*?</form>", "contact form")
        _require(index, r"<label\b[^>]*for=[\"'][^\"']*(?:name|email|message)[^\"']*[\"']", "associated form labels")
        _require(index, r"<input\b[^>]*type=[\"']text[\"'][^>]*name=[\"']name[\"'][^>]*required", "required name control")
        _require(index, r"<input\b[^>]*type=[\"']email[\"'][^>]*name=[\"']email[\"'][^>]*required", "required email control")
        _require(index, r"<textarea\b[^>]*name=[\"']message[\"'][^>]*required", "required message control")
        _require(index, r"<button\b[^>]*type=[\"']submit[\"']", "submit button")
    elif task == "PAGE-105":
        about = _read(root, "site/about.html")
        _require(about, r"<!doctype\s+html>", "about HTML doctype")
        _require(about, r"<(?:title|h1)\b[^>]*>[^<]*simplicio", "about Simplicio identity")
        _require(about, r"<a\b[^>]*href=[\"'](?:\.\/)?index\.html[\"']", "return link")
    else:
        raise AssertionError(f"unknown task: {task}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--repo", type=Path, required=True)
    args = parser.parse_args()
    verify(args.task, args.repo.resolve())
    print(f"PASS {args.task}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
