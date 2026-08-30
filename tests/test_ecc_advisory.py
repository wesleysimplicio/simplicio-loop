from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess

from simplicio_loop.ecc_guidance import (
    ECC_DEFAULT_MANIFEST_HASH,
    ECC_DEFAULT_REF,
    ECC_GUIDANCE_REF_SCHEMA,
    ECC_SOURCE_REPOSITORY,
    ensure_ecc_ready,
    extract_guidance_reference,
    inspect_ecc,
)


def _ready_provider_payload() -> dict:
    return {
        "schema": "simplicio.ecc-doctor/v1",
        "status": "READY",
        "enabled": True,
        "available": True,
        "root": "/tmp/ECC",
        "source": {"repository": ECC_SOURCE_REPOSITORY, "ref": ECC_DEFAULT_REF},
        "provenance": {
            "status": "VERIFIED",
            "expected_ref": ECC_DEFAULT_REF,
            "observed_ref": ECC_DEFAULT_REF,
        },
        "manifest_hash": ECC_DEFAULT_MANIFEST_HASH,
        "allow_hooks": False,
        "hooks_effective": False,
        "errors": [],
    }


def _ready_reference() -> dict:
    return {
        "schema": ECC_GUIDANCE_REF_SCHEMA,
        "status": "READY",
        "stage": "planning",
        "role_id": "mapper-planner",
        "source": {"repository": ECC_SOURCE_REPOSITORY, "ref": ECC_DEFAULT_REF},
        "provenance": {
            "status": "VERIFIED",
            "expected_ref": ECC_DEFAULT_REF,
            "observed_ref": ECC_DEFAULT_REF,
        },
        "manifest_hash": ECC_DEFAULT_MANIFEST_HASH,
        "pack_hash": "a" * 64,
        "authority": "simplicio-mapper",
        "execution_policy": "advisory-only",
        "components": [
            {
                "kind": "skill",
                "name": "search-first",
                "path": "skills/search-first/SKILL.md",
                "sha256": "b" * 64,
                "content_sha256": "c" * 64,
                "truncated": False,
            }
        ],
    }


def test_ecc_admission_is_disabled_without_opt_in(tmp_path: Path) -> None:
    calls = []

    def fake_runner(*args, **kwargs):
        calls.append(args)
        return CompletedProcess(args[0], 0, "{}", "")

    receipt = inspect_ecc(
        tmp_path,
        tmp_path / "run",
        env={},
        runner=fake_runner,
    )

    assert receipt["status"] == "DISABLED"
    assert receipt["enabled"] is False
    assert calls == []
    persisted = json.loads((tmp_path / "run" / "ecc-doctor.json").read_text())
    assert persisted["status"] == "DISABLED"


def test_ecc_admission_records_verified_provenance_without_bodies(tmp_path: Path) -> None:
    def fake_runner(*args, **kwargs):
        return CompletedProcess(args[0], 0, json.dumps(_ready_provider_payload()), "")

    receipt = inspect_ecc(
        tmp_path,
        tmp_path / "run",
        env={"SIMPLICIO_ECC_ENABLED": "1", "SIMPLICIO_ECC_ROOT": "/tmp/ECC"},
        runner=fake_runner,
    )

    assert receipt["status"] == "READY"
    assert receipt["provenance"]["observed_ref"] == ECC_DEFAULT_REF
    assert "prompt" not in receipt
    persisted = json.loads((tmp_path / "run" / "ecc-doctor.json").read_text())
    assert persisted["body_policy"].startswith("loop stores provenance")
    assert persisted["manifest_hash"] == ECC_DEFAULT_MANIFEST_HASH


def test_required_ecc_admission_fails_closed(tmp_path: Path) -> None:
    def fake_runner(*args, **kwargs):
        return CompletedProcess(args[0], 2, "", "missing")

    receipt = inspect_ecc(
        tmp_path,
        tmp_path / "run",
        env={"SIMPLICIO_ECC_REQUIRED": "1"},
        runner=fake_runner,
    )

    assert receipt["status"] == "BLOCKED"
    assert receipt["required"] is True
    try:
        ensure_ecc_ready(receipt)
    except ValueError as exc:
        assert "required" in str(exc).casefold()
    else:
        raise AssertionError("required ECC admission should fail closed")


def test_only_hash_reference_is_admitted() -> None:
    reference = _ready_reference()
    payload = {"result": {"ecc_guidance_ref": reference}}
    assert extract_guidance_reference(payload) == reference

    with_body = dict(reference)
    with_body["prompt"] = "must not enter Loop receipt"
    assert extract_guidance_reference({"ecc_guidance_ref": with_body}) is None
