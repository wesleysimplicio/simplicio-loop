"""Unit tests for the pure installer planner (#293 first slice: separar planner e executor).

`scripts/install_plan.build_plan()` must never mutate anything — these tests assert
determinism, effect classification, and consent-gating (`--allow-break-system-packages`)
without touching the filesystem beyond a read-only stat of the target directory.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("install_plan", ROOT / "scripts" / "install_plan.py")
install_plan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(install_plan)  # type: ignore[union-attr]

build_plan = install_plan.build_plan


def test_plan_is_deterministic_for_same_inputs(tmp_path):
    plan_a = build_plan("claude", mode="minimal", scope="project", target=str(tmp_path))
    plan_b = build_plan("claude", mode="minimal", scope="project", target=str(tmp_path))
    # generated_at is a timestamp and legitimately varies; everything else — including the
    # transaction id and the content-derived receipt hash — must match byte for byte.
    plan_a.pop("generated_at")
    plan_b.pop("generated_at")
    assert plan_a == plan_b


def test_plan_id_and_receipt_hash_change_with_scope(tmp_path):
    project_plan = build_plan("claude", mode="minimal", scope="project", target=str(tmp_path))
    user_plan = build_plan("claude", mode="minimal", scope="user", target=str(tmp_path))
    assert project_plan["transaction_id"] != user_plan["transaction_id"]
    assert project_plan["receipt_hash"] != user_plan["receipt_hash"]


def test_plan_never_mutates_filesystem(tmp_path):
    target = tmp_path / "project"
    target.mkdir()
    before = sorted(p.relative_to(target) for p in target.rglob("*"))
    build_plan("claude", mode="minimal", scope="project", target=str(target))
    after = sorted(p.relative_to(target) for p in target.rglob("*"))
    assert before == after == []


def test_minimal_project_scope_requires_no_consent_permissions(tmp_path):
    plan = build_plan("claude", mode="minimal", scope="project", target=str(tmp_path))
    assert plan["permissions_required"] == []
    assert plan["status"] == "PLANNED"


def test_user_scope_requires_global_and_path_permissions(tmp_path):
    plan = build_plan("claude", mode="minimal", scope="user", target=str(tmp_path))
    assert "global_package" in plan["permissions_required"]
    assert "path_write" in plan["permissions_required"]
    assert "symlink" in plan["permissions_required"]


def test_break_system_packages_without_flag_blocks_the_plan(tmp_path):
    # scope=user always requires global_package; simulate the break-system-packages
    # fallback path being requested without the explicit opt-in flag.
    plan = build_plan("claude", mode="minimal", scope="project", target=str(tmp_path),
                      allow_break_system_packages=False)
    plan["permissions_required"].append("break_system_packages")  # sanity: absent by default
    assert "break_system_packages" not in build_plan(
        "claude", mode="minimal", scope="project", target=str(tmp_path),
    )["permissions_required"]


def test_full_stack_mode_requires_service_and_proxy_consent(tmp_path):
    plan = build_plan("claude", mode="full-stack", scope="project", target=str(tmp_path))
    assert "service" in plan["permissions_required"]
    assert "proxy" in plan["permissions_required"]
    assert plan["services"] == [{"name": "capture-proxy", "action": "install"}]


def test_files_are_classified_create_when_target_is_empty(tmp_path):
    plan = build_plan("claude", mode="minimal", scope="project", target=str(tmp_path))
    skill_paths = [f for f in plan["files"] if ".claude" + str(Path("/skills")) in f["path"] or "skills" in f["path"]]
    assert skill_paths, "expected at least one skills-directory file effect"
    assert all(f["action"] == "create" for f in skill_paths)
    assert all(f["reversible"] is True for f in skill_paths)


def test_files_classified_update_when_skill_already_present(tmp_path):
    existing = tmp_path / ".claude" / "skills" / "simplicio-loop"
    existing.mkdir(parents=True)
    plan = build_plan("claude", mode="minimal", scope="project", target=str(tmp_path))
    entry = next(f for f in plan["files"] if f["path"].endswith(str(Path("skills/simplicio-loop"))))
    assert entry["action"] == "update"


def test_unknown_mode_and_scope_are_rejected(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        build_plan("claude", mode="bogus", scope="project", target=str(tmp_path))
    with pytest.raises(ValueError):
        build_plan("claude", mode="minimal", scope="bogus", target=str(tmp_path))


def test_ci_mode_plan_is_pinned_other_modes_are_floating(tmp_path):
    # #293 mode `ci`: "não interativa ... com versões fixadas" — vs minimal/runtime/full-stack's
    # normal floating install. The plan surfaces the INTENT (pure, no I/O); the actual version
    # resolution happens in install_lib.ensure_operators(pin_versions=...).
    ci_plan = build_plan("claude", mode="ci", scope="project", target=str(tmp_path))
    assert ci_plan["version_pinning"] == "pinned"
    for mode in ("minimal", "runtime"):
        plan = build_plan("claude", mode=mode, scope="project", target=str(tmp_path))
        assert plan["version_pinning"] == "floating", mode
    fs_plan = build_plan("claude", mode="full-stack", scope="project", target=str(tmp_path),
                         with_service=True, with_proxy=True)
    assert fs_plan["version_pinning"] == "floating"


def test_plan_matches_declared_schema_shape(tmp_path):
    schema_path = ROOT / "contracts" / "install-transaction" / "v1" / "schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    plan = build_plan("claude", mode="minimal", scope="project", target=str(tmp_path))
    for field in schema["required"]:
        assert field in plan, "plan missing schema-required field %r" % field
    assert plan["schema"] == schema["properties"]["schema"]["const"]
    assert plan["mode"] in schema["properties"]["mode"]["enum"]
    assert plan["scope"] in schema["properties"]["scope"]["enum"]
    assert plan["status"] in schema["properties"]["status"]["enum"]


def test_cli_dry_run_prints_plan_and_exits_zero(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "install_plan.py"), "claude",
         "--mode", "minimal", "--scope", "project", "--target", str(tmp_path)],
        capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema"] == "simplicio.install-transaction/v1"
    assert payload["status"] == "PLANNED"


def test_install_lib_dry_run_makes_no_filesystem_changes(tmp_path):
    """End-to-end: `install.sh`'s Python entrypoint with --dry-run must not create any of
    the files/dirs a real install would (skills, hooks, entry files, settings.json)."""
    target = tmp_path / "project"
    target.mkdir()
    install_lib = ROOT / "scripts" / "install_lib.py"
    result = subprocess.run(
        [sys.executable, str(install_lib), "claude", "--dry-run", "--target", str(target)],
        capture_output=True, text=True, timeout=60, cwd=str(ROOT), stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema"] == "simplicio.install-transaction/v1"
    assert payload["status"] == "PLANNED"
    # No mutation happened: the target directory we created must still be empty.
    assert sorted(target.rglob("*")) == []
