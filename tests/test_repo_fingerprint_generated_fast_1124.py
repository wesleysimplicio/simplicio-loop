from simplicio_loop import runner


def test_repo_fingerprint_excludes_generated_simplicio_fast_state(tmp_path):
    (tmp_path / "source.py").write_text("value = 1\n", encoding="utf-8")
    before = runner._repo_fingerprint(tmp_path)

    generated = tmp_path / ".simplicio-fast"
    generated.mkdir()
    (generated / "loop-ingest.json").write_text("{\"generation\":\"g1\"}\n", encoding="utf-8")

    after = runner._repo_fingerprint(tmp_path)

    assert after["tree_hash"] == before["tree_hash"]
