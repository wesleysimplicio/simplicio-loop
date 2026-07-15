"""#78: coverage for scripts/az_boards_adapter.py — the Azure DevOps source_adapter binding.

Shells to the `az` CLI (resolved via shutil.which, invoked with a list argv — never a shell).
This suite never lets a real `az` process run: every non-dry-run invocation is made with PATH
restricted to a directory containing no `az` binary, so `_exe("az")` falls back to the bare
name and subprocess.run raises FileNotFoundError — the adapter's own documented fallback path
("az not found on PATH" -> exit 3), never a real Azure Boards/Repos/Pipelines call. `--dry-run`
invocations never even reach subprocess.run (opts.get("dry-run") short-circuits _az()).
"""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADAPTER = os.path.join(REPO, "scripts", "az_boards_adapter.py")


def _no_az_env():
    env = dict(os.environ)
    env["PATH"] = "/usr/bin:/bin"  # no `az` here (verified absent from the real host PATH too)
    return env


def _run(args, dry_run_safe=True):
    env = os.environ.copy() if dry_run_safe else _no_az_env()
    return subprocess.run([sys.executable, ADAPTER] + args, capture_output=True, text=True,
                          cwd=REPO, env=env, timeout=30)


# ── --dry-run: prints the resolved `az` argv, never executes it ───────────────


def test_list_ready_dry_run_prints_az_command():
    r = _run(["list_ready", "--dry-run"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.startswith("az boards query")
    assert "WIQL" not in r.stdout  # the raw query text, not the template placeholder


def test_get_details_dry_run_prints_two_az_calls():
    r = _run(["get_details", "--id", "1234", "--dry-run"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "work-item show" in r.stdout
    assert "devops invoke" in r.stdout
    assert "1234" in r.stdout


def test_claim_dry_run_prints_read_and_write_calls():
    r = _run(["claim", "--id", "1234", "--me", "me@org.com", "--dry-run"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "work-item show" in r.stdout
    assert "work-item update" in r.stdout
    assert "me@org.com" in r.stdout


def test_open_pr_dry_run_quotes_embedded_spaces_and_quotes_safely():
    r = _run(["open_pr", "--repo", "Web", "--source", "feat/x", "--id", "1234",
              "--title", 'Fix "quoted" bug', "--dry-run"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'Fix \\"quoted\\" bug' in r.stdout, r.stdout


def test_pr_status_run_pipeline_pipeline_status_dry_run():
    for args in (["pr_status", "--pr", "57", "--dry-run"],
                 ["run_pipeline", "--pipeline", "CI", "--dry-run"],
                 ["pipeline_status", "--run", "99", "--dry-run"]):
        r = _run(args)
        assert r.returncode == 0, "%r -> %s" % (args, r.stdout + r.stderr)
        assert r.stdout.startswith("az "), r.stdout


# ── required-flag validation happens BEFORE any az call, dry-run or not ──────


def test_get_details_missing_id_exits_2_before_any_az_call():
    r = _run(["get_details"])
    assert r.returncode == 2, r.stdout + r.stderr
    assert "missing required flag" in r.stdout.lower()
    assert "--id" in r.stdout


def test_claim_missing_me_exits_2():
    r = _run(["claim", "--id", "1"])
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--me" in r.stdout


def test_open_pr_missing_repo_source_id_lists_all_missing():
    r = _run(["open_pr"])
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--repo" in r.stdout and "--source" in r.stdout and "--id" in r.stdout


# ── real (non-dry-run) call with `az` absent from PATH: clean block, never a crash ─


def test_list_ready_without_az_on_path_blocks_cleanly():
    r = _run(["list_ready"], dry_run_safe=False)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "Traceback" not in r.stderr
    assert "az not found on PATH" in r.stderr


def test_pr_status_without_az_on_path_blocks_cleanly():
    r = _run(["pr_status", "--pr", "1"], dry_run_safe=False)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "az not found on PATH" in r.stderr


# ── CLI contract: no args / unknown verb prints the module docstring, exits 2 ──


def test_no_args_prints_usage_and_exits_2():
    r = _run([])
    assert r.returncode == 2, r.stdout + r.stderr
    assert "Usage:" in r.stdout


def test_unknown_verb_prints_usage_and_exits_2():
    r = _run(["not-a-real-verb"])
    assert r.returncode == 2, r.stdout + r.stderr
    assert "Usage:" in r.stdout


# ── pure helper functions, imported directly (no subprocess, no `az`) ─────────


def _import_adapter():
    import importlib
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    if "az_boards_adapter" in sys.modules:
        del sys.modules["az_boards_adapter"]
    return importlib.import_module("az_boards_adapter")


def test_wiql_lit_escapes_single_quotes():
    mod = _import_adapter()
    assert mod._wiql_lit("O'Brien") == "O''Brien"


def test_quote_wraps_and_escapes_spaces_and_quotes():
    mod = _import_adapter()
    assert mod._quote("no-spaces") == "no-spaces"
    assert mod._quote('has space') == '"has space"'
    assert mod._quote('has "quote"') == '"has \\"quote\\""'


def test_parse_builds_opts_dict_from_flag_value_pairs():
    mod = _import_adapter()
    opts = mod._parse(["--id", "42", "--dry-run", "--state", "Active"])
    assert opts == {"id": "42", "dry-run": True, "state": "Active"}


def test_org_and_common_read_from_opts_over_env():
    mod = _import_adapter()
    old_org = os.environ.pop("AZURE_DEVOPS_ORG", None)
    old_proj = os.environ.pop("AZURE_DEVOPS_PROJECT", None)
    try:
        assert mod._org({}) == []
        assert mod._org({"org": "https://dev.azure.com/x"}) == \
            ["--organization", "https://dev.azure.com/x"]
        assert mod._common({"org": "https://dev.azure.com/x", "project": "P"}) == \
            ["--organization", "https://dev.azure.com/x", "--project", "P"]
    finally:
        if old_org is not None:
            os.environ["AZURE_DEVOPS_ORG"] = old_org
        if old_proj is not None:
            os.environ["AZURE_DEVOPS_PROJECT"] = old_proj


def test_require_exits_2_and_lists_all_missing_keys():
    mod = _import_adapter()
    try:
        mod._require({"id": "1"}, "id", "state", "note")
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("expected SystemExit(2)")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_az_boards_adapter")
