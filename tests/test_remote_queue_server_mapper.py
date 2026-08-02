from __future__ import annotations

from pathlib import Path

import pytest

from simplicio_loop import remote_queue_server_cli


def test_mapper_db_is_an_explicit_server_lane(monkeypatch, tmp_path: Path):
    captured = {}

    class FakeQueue:
        def __init__(self, database, *, auto_create):
            captured["database"] = database
            captured["auto_create"] = auto_create

        def initialize(self):
            captured["initialized"] = True
            return {"status": "ready"}

    class FakeServer:
        server_port = 9876

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            captured["closed"] = True

        def shutdown(self):
            pass

    monkeypatch.setattr(remote_queue_server_cli, "MapperRemoteQueue", FakeQueue)
    monkeypatch.setattr(remote_queue_server_cli, "create_http_queue_server",
                        lambda queue, *args, **kwargs: (captured.__setitem__("queue", queue) or FakeServer()))
    monkeypatch.setattr(remote_queue_server_cli.signal, "signal", lambda *args: None)
    monkeypatch.setattr(
        "sys.argv",
        ["simplicio-remote-queue-server", "--mapper-db", str(tmp_path / "operations.sqlite"),
         "--mapper-init", "--token", "test-token"],
    )
    assert remote_queue_server_cli.main() == 0
    assert captured["database"] == str(tmp_path / "operations.sqlite")
    assert captured["auto_create"] is False
    assert captured["initialized"] is True
    assert captured["closed"] is True


def test_mapper_db_and_legacy_db_cannot_be_combined(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["simplicio-remote-queue-server", "--db", "legacy.db", "--mapper-db", "operations.sqlite",
         "--token", "test-token"],
    )
    with pytest.raises(SystemExit):
        remote_queue_server_cli.main()
