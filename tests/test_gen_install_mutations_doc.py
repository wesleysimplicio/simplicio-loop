"""Unit tests for `scripts/gen_install_mutations_doc.py`'s machine-readable JSON manifest
(#293 gap 4: "machine-readable effects manifest" beyond the per-run `install-transaction/v1`
plan). The `.md` doc and the `.json` manifest are BOTH rendered from the SAME
`MUTATIONS`/`OS_DIFFS`/`_consent_rows()` source-of-truth constants — these tests assert the JSON
shape, that it stays in sync with the constants, and that `--check`/`--json` behave as documented.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "gen_install_mutations_doc", ROOT / "scripts" / "gen_install_mutations_doc.py")
gen = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gen)  # type: ignore[union-attr]


def test_manifest_dict_has_expected_top_level_shape():
    manifest = gen.build_manifest_dict()
    assert manifest["schema"] == "simplicio.install-mutations/v1"
    assert isinstance(manifest["mutations"], list) and manifest["mutations"]
    assert isinstance(manifest["os_differences"], list) and manifest["os_differences"]
    assert isinstance(manifest["consent_matrix"], list) and manifest["consent_matrix"]


def test_manifest_mutation_rows_match_source_constant_count_and_fields():
    manifest = gen.build_manifest_dict()
    assert len(manifest["mutations"]) == len(gen.MUTATIONS)
    for row in manifest["mutations"]:
        assert set(row) == {"source", "function", "effect", "scope", "reversible",
                            "consent_required"}


def test_manifest_os_diff_rows_match_source_constant():
    manifest = gen.build_manifest_dict()
    assert len(manifest["os_differences"]) == len(gen.OS_DIFFS)
    for row in manifest["os_differences"]:
        assert set(row) == {"concern", "linux", "macos", "windows"}


def test_manifest_consent_matrix_matches_consent_rows_helper():
    manifest = gen.build_manifest_dict()
    assert len(manifest["consent_matrix"]) == len(gen._consent_rows())
    for row in manifest["consent_matrix"]:
        assert set(row) == {"effect", "trigger", "required_consent"}


def test_manifest_json_is_deterministic_across_calls():
    a = json.dumps(gen.build_manifest_dict(), sort_keys=True)
    b = json.dumps(gen.build_manifest_dict(), sort_keys=True)
    assert a == b


def test_cli_json_flag_prints_valid_json_matching_build_manifest_dict():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen_install_mutations_doc.py"),
                       "--json"], capture_output=True, text=True, timeout=30,
                      encoding="utf-8", stdin=subprocess.DEVNULL, cwd=str(ROOT))
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert payload == gen.build_manifest_dict()


def test_generated_json_file_on_disk_matches_generator_output():
    """docs/install-mutations.json must be committed and byte-identical to the generator's
    current output — the same drift contract claims_audit.py enforces for the .md doc."""
    json_path = ROOT / "docs" / "install-mutations.json"
    assert json_path.exists(), "docs/install-mutations.json must exist (generated, checked in)"
    on_disk = json.loads(json_path.read_text(encoding="utf-8"))
    assert on_disk == gen.build_manifest_dict()


def test_check_flag_fails_when_json_file_is_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(gen, "JSON_PATH", str(tmp_path / "install-mutations.json"))
    monkeypatch.setattr(gen, "DOC_PATH", str(tmp_path / "INSTALL_MUTATIONS.md"))
    # Neither generated file exists yet under this isolated tmp_path -> --check must fail.
    rc = gen.main(["--check"])
    assert rc == 1


def test_check_flag_passes_after_writing_both_files(tmp_path, monkeypatch):
    monkeypatch.setattr(gen, "JSON_PATH", str(tmp_path / "install-mutations.json"))
    monkeypatch.setattr(gen, "DOC_PATH", str(tmp_path / "INSTALL_MUTATIONS.md"))
    assert gen.main([]) == 0
    assert gen.main(["--check"]) == 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _selfrun import run_module
    run_module(globals(), "test_gen_install_mutations_doc")
