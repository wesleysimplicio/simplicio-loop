"""Independent behavioral oracle for the ten frozen queue benchmark tasks."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import time

TASK_IDS = ("GH-101", "GH-102", "GH-103", "GH-104", "JIRA-201", "JIRA-202", "JIRA-203", "ADO-301", "ADO-302", "ADO-303")


def function(root, filename, name):
    spec = importlib.util.spec_from_file_location("queue_fixture_" + name, root / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, name)


def check(root: Path, task: str):
    if task == "GH-101":
        fn = function(root, "src/slug.py", "slugify")
        assert [fn(s) for s in (" Hello WORLD ", "a   b", "", "A\tB")] == ["hello-world", "a-b", "", "a-b"]
    elif task == "GH-102":
        fn = function(root, "src/total.py", "total")
        assert [fn(v) for v in ([], [-5, 2], [2, 3, 7])] == [0, -3, 12]
    elif task == "GH-103":
        data = json.loads((root / "answers/timeout.json").read_text())
        assert data == {"pointer": "/network/timeout_seconds", "value": 30}
    elif task == "GH-104":
        path = root / "tests/test_total.py"
        assert path.is_file()
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", str(path)],
                                cwd=root, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, "submitted tests did not pass"
        # Require behavioral sensitivity to the original len(values) defect.
        program = """import pytest
import src.total
src.total.total = lambda values: len(values)
raise SystemExit(pytest.main(['-q', 'tests/test_total.py']))
"""
        mutant = subprocess.run([sys.executable, "-c", program], cwd=root,
                                capture_output=True, text=True, timeout=30)
        assert mutant.returncode == 1, "submitted tests did not detect original bug"
    elif task == "JIRA-201":
        fn = function(root, "src/unique.py", "unique")
        assert fn([3, 1, 3, 2, 1]) == [3, 1, 2]
        assert fn([]) == []
    elif task == "JIRA-202":
        fn = function(root, "src/clamp.py", "clamp")
        assert [fn(v, 0, 10) for v in (-2, 4, 20)] == [0, 4, 10]
    elif task == "JIRA-203":
        text = (root / "docs/configuration.md").read_text().lower()
        assert "timeout" in text and re.search(r"\b30\b", text) and "second" in text
    elif task == "ADO-301":
        assert json.loads((root / "answers/deprecated.json").read_text()) == ["legacy", "v0"]
    elif task == "ADO-302":
        fn = function(root, "src/median.py", "median")
        assert fn([9, 1, 3]) == 3 and fn([4, 1, 3, 2]) == 2.5 and fn([8]) == 8
    elif task == "ADO-303":
        fn = function(root, "src/boolean.py", "parse_bool")
        assert fn("TRUE") is True and fn("false") is False and fn("FaLsE") is False
        try:
            fn("invalid")
        except ValueError:
            pass
        else:
            raise AssertionError("invalid boolean was accepted")
    else:
        raise ValueError("unknown task")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--task", choices=TASK_IDS)
    args = parser.parse_args()
    root = Path(args.repo).resolve()
    rows = []
    for task in ([args.task] if args.task else TASK_IDS):
        start = time.perf_counter_ns()
        try:
            check(root, task)
            status, reason = "passed", None
        except Exception as exc:
            status, reason = "failed", type(exc).__name__
        rows.append({"task": task, "status": status, "reason": reason,
                     "verification_wall_ns": time.perf_counter_ns() - start})
    print(json.dumps({"schema": "loop-queue-behavioral-verification/v1", "repo": str(root), "results": rows}, indent=2))
    return 0 if all(row["status"] == "passed" for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
