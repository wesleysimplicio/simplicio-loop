from simplicio_loop.map_service_delivery import SnapshotDeliveryStore, SnapshotLimitError


def test_mmap_delivery_matches_stream_content_and_gc_respects_pin(tmp_path):
    store = SnapshotDeliveryStore(str(tmp_path), max_bytes=1024)
    store.publish("old", b"old-map")
    store.publish("current", b"canonical-map\n")
    with store.acquire("current") as handle:
        assert handle.read() == b"canonical-map\n"
        assert b"".join(handle.iter_chunks(3)) == b"canonical-map\n"
        store.invalidate("old")
        assert store.gc() == ["old"]
        store.invalidate("current")
        assert store.gc() == []
    assert store.status()["pinned"] == 0
    assert store.gc() == []  # the latest snapshot is retained as the recovery point


def test_snapshot_size_limit_is_enforced(tmp_path):
    store = SnapshotDeliveryStore(str(tmp_path), max_bytes=3)
    try:
        store.publish("too-large", b"1234")
    except SnapshotLimitError:
        pass
    else:
        raise AssertionError("oversized snapshot was accepted")
