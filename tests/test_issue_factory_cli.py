from simplicio_loop.issue_factory_cli import main


def test_azure_discover_dry_run(capsys):
    assert main(["discover", "--source", "azure", "--org", "https://dev.azure.com/acme",
                 "--project", "Loop", "--state", "New", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "az boards query" in output
    assert "https://dev.azure.com/acme" in output
    assert "--project Loop" in output
