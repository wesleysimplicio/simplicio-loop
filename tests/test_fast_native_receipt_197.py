from simplicio_fast.generation_receipts import seal_receipt
from simplicio_fast.native_backend import PythonBackend, backend_receipt_fields
from simplicio_loop.fast_receipt_audit import audit_fast_generation


def test_loop_observes_native_selection_or_explicit_python_fallback():
    fields = backend_receipt_fields(PythonBackend(), "RUST_UNAVAILABLE")
    receipt = seal_receipt(
        kind="query", repo="org/repo", commit="a" * 40,
        snapshot_digest="b" * 64, generation="g1",
        source_hashes={"src/a.py": "c" * 64}, **fields)
    audit = audit_fast_generation(
        receipt, repo="org/repo", commit="a" * 40, generation="g1",
        source_hashes={"src/a.py": "c" * 64})
    assert audit["backend"] == "python"
    assert audit["fallback_reason"] == "RUST_UNAVAILABLE"
    assert audit["completion_authority"] == "LOOP"
