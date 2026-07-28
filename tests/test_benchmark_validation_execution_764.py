import json
import subprocess
import sys

from scripts.benchmark_validation_execution_764 import main


def test_benchmark_requires_and_measures_three_pinned_repositories(tmp_path):
    repos = []
    for index in range(3):
        repo = tmp_path / f"repo-{index}"
        repo.mkdir()
        subprocess.run(("git", "init", "-q", str(repo)), check=True)
        subprocess.run(("git", "-C", str(repo), "config", "user.email", "test@example.invalid"), check=True)
        subprocess.run(("git", "-C", str(repo), "config", "user.name", "Test"), check=True)
        (repo / "sample.py").write_text("value = 1\n", encoding="utf-8")
        subprocess.run(("git", "-C", str(repo), "add", "sample.py"), check=True)
        subprocess.run(("git", "-C", str(repo), "commit", "-qm", "fixture"), check=True)
        repos.append(repo)
    workload = tmp_path / "workload.json"
    workload.write_text(json.dumps([[sys.executable, "-c", "pass"]]), encoding="utf-8")
    output = tmp_path / "receipt.json"
    argv = [part for repo in repos for part in ("--repo", str(repo))]
    argv += ["--workload", str(workload), "--repetitions", "3", "--output", str(output)]
    assert main(argv) == 0
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert len(receipt["repositories"]) == 3
    assert all(len(item["sha"]) == 40 for item in receipt["repositories"])
    assert all(item["summary"]["quality_regression"] is False for item in receipt["repositories"])
    assert receipt["local_llm_started"] is False
