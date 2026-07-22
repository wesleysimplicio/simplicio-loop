import json

from scripts import issue_meta_audit as audit
from tests.test_issue_meta_audit_647_unit import compliant_issue


def test_offline_cli_writes_deterministic_json_and_markdown(tmp_path):
    source = tmp_path / "issues.json"
    source.write_text(json.dumps([compliant_issue()]), encoding="utf-8")
    json_out, md_out = tmp_path / "report.json", tmp_path / "report.md"
    argv = ["--repo", "acme/repo", "--input", str(source), "--json-out", str(json_out),
            "--markdown-out", str(md_out)]
    assert audit.main(argv) == 0
    first = json_out.read_bytes()
    assert audit.main(argv) == 0
    assert json_out.read_bytes() == first
    assert json.loads(first)["schema"] == audit.SCHEMA
    assert "Decisão final: **READY**" in md_out.read_text(encoding="utf-8")


def test_cli_keeps_reports_and_returns_one_for_nonconformance(tmp_path):
    source = tmp_path / "issues.json"
    source.write_text(json.dumps([{"number": 1, "state": "open", "body": ""}]), encoding="utf-8")
    json_out, md_out = tmp_path / "report.json", tmp_path / "report.md"
    assert audit.main(["--input", str(source), "--json-out", str(json_out), "--markdown-out", str(md_out)]) == 1
    assert json.loads(json_out.read_text())["summary"]["ready"] is False
    assert md_out.exists()
