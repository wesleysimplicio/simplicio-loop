import json

import pytest

from simplicio_loop.tee_cache import retrieve, write


def test_tee_round_trip_is_content_addressed(tmp_path):
    content = json.dumps({"status": "READY", "unicode": "ação"})
    path = write(tmp_path, content)
    assert path.name.endswith(".out")
    assert retrieve(path, tmp_path) == content
    assert write(tmp_path, content) == path


def test_tee_rejects_tampering_and_outside_path(tmp_path):
    path = write(tmp_path, "safe")
    path.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        retrieve(path, tmp_path)
    with pytest.raises(ValueError, match="direct"):
        retrieve(tmp_path / "other.out", tmp_path)
