"""Tests for simplicio_loop.agent_identity (issue #434, EPIC #422).

Covers: HOST4 normalization (ASCII, unicode, special chars, short/long,
missing/empty), the explicit fallback + reason code, display-name
formatting/sanitization, and that display-name collisions across different
hosts/LLMs are explicitly allowed (uniqueness lives in the technical
identity, never in the display name).
"""
from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from simplicio_loop import agent_identity as ai


# --------------------------------------------------------------------------- #
# HOST4 normalization
# --------------------------------------------------------------------------- #
def test_derive_host4_ascii_username():
    assert ai.derive_host4("alice") == "ALIC"


def test_derive_host4_is_case_insensitive():
    assert ai.derive_host4("AlIcE") == ai.derive_host4("alice") == "ALIC"


def test_derive_host4_short_username_not_padded():
    assert ai.derive_host4("Jo") == "JO"
    assert ai.derive_host4("A") == "A"


def test_derive_host4_long_username_capped_at_four():
    host4 = ai.derive_host4("wesleysimplicio")
    assert host4 == "WESL"
    assert len(host4) == 4


def test_derive_host4_strips_symbols_and_spaces():
    assert ai.derive_host4("j.o-s_e!! ") == "JOSE"
    assert ai.derive_host4("  w e s  ") == "WES"


def test_derive_host4_unicode_folds_to_ascii():
    # accented characters fold via NFKD before stripping non-ASCII remnants
    assert ai.derive_host4("José") == "JOSE"
    assert ai.derive_host4("Müller") == "MULL"


def test_derive_host4_unicode_only_falls_back():
    # characters with no ASCII decomposition (e.g. CJK/emoji) normalize to
    # nothing -> explicit fallback, never an invented identifier
    host4, reason = ai.resolve_host4("北京")
    assert host4 == ai.HOST4_FALLBACK
    assert reason == ai.HOST4_FALLBACK_REASON

    host4, reason = ai.resolve_host4("🚀🔥")
    assert host4 == ai.HOST4_FALLBACK
    assert reason == ai.HOST4_FALLBACK_REASON


def test_derive_host4_is_pure_and_deterministic():
    for _ in range(5):
        assert ai.derive_host4("wesley") == "WESL"


# --------------------------------------------------------------------------- #
# Fallback path + reason code
# --------------------------------------------------------------------------- #
def test_resolve_host4_missing_user_returns_explicit_fallback():
    host4, reason = ai.resolve_host4(None)
    assert host4 == ai.HOST4_FALLBACK
    assert reason == ai.HOST4_FALLBACK_REASON


def test_resolve_host4_empty_string_returns_explicit_fallback():
    host4, reason = ai.resolve_host4("")
    assert host4 == ai.HOST4_FALLBACK
    assert reason == ai.HOST4_FALLBACK_REASON


def test_resolve_host4_symbols_only_returns_explicit_fallback():
    host4, reason = ai.resolve_host4("###!!!")
    assert host4 == ai.HOST4_FALLBACK
    assert reason == ai.HOST4_FALLBACK_REASON


def test_resolve_host4_never_raises_on_non_string():
    host4, reason = ai.resolve_host4(12345)  # type: ignore[arg-type]
    assert host4 == "1234"
    assert reason is None


def test_derive_host4_for_this_host_without_env_falls_back(monkeypatch):
    for var in ("USER", "USERNAME", "LOGNAME", "HOSTNAME", "COMPUTERNAME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        "socket.gethostname", lambda: "", raising=True
    )
    monkeypatch.setattr(
        os, "getlogin", lambda: (_ for _ in ()).throw(OSError("no login")), raising=True
    )
    host4, reason = ai.derive_host4_for_this_host()
    assert host4 == ai.HOST4_FALLBACK
    assert reason == ai.HOST4_FALLBACK_REASON


def test_derive_host4_for_this_host_uses_username_env(monkeypatch):
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.setenv("USERNAME", "wesley")
    host4, reason = ai.derive_host4_for_this_host()
    assert host4 == "WESL"
    assert reason is None


def test_derive_host4_for_this_host_falls_back_to_hostname(monkeypatch):
    for var in ("USER", "USERNAME", "LOGNAME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOSTNAME", "buildbox1")
    host4, reason = ai.derive_host4_for_this_host()
    assert host4 == "BUIL"
    assert reason is None


# --------------------------------------------------------------------------- #
# Display-name formatting
# --------------------------------------------------------------------------- #
def test_format_display_name_matches_required_format():
    assert ai.format_display_name("Alex", "Review", "PC1", "Claude") == "Alex Review - #PC1 - Claude"


def test_format_display_name_examples_from_issue():
    assert ai.format_display_name("Alex", "Review", "PC1", "Codex") == "Alex Review - #PC1 - Codex"
    assert ai.format_display_name("Alex", "Review", "PC2", "Claude") == "Alex Review - #PC2 - Claude"


def test_format_display_name_sanitizes_markdown_and_html():
    name = ai.format_display_name("Alex`rm -rf`", "<script>Review</script>", "PC1", "[Claude](evil)")
    for bad in ("`", "<", ">", "[", "]", "(", ")"):
        assert bad not in name


def test_format_display_name_handles_empty_fields_without_crashing():
    name = ai.format_display_name("", "", "", "")
    assert isinstance(name, str)
    assert name  # never empty — falls back to safe defaults


# --------------------------------------------------------------------------- #
# resolve_agent_identity: receipt-ready dict + display/technical separation
# --------------------------------------------------------------------------- #
def test_resolve_agent_identity_separates_display_from_technical_fields():
    result = ai.resolve_agent_identity(
        name="Alex", role="Review", llm="Claude",
        agent_instance_id="inst-abc123", raw_user="alice",
        provider="anthropic", model="claude-sonnet-5", runtime="claude-code",
        host_id="host-fingerprint-1", run_id="run-1", task_id="task-1",
        attempt_id="attempt-1", fence="fence-1",
    )
    assert result["display_name"] == "Alex Review - #ALIC - Claude"
    assert result["host_identity_fallback"] is None
    assert result["agent_instance_id"] == "inst-abc123"
    assert result["provider"] == "anthropic"
    assert result["model"] == "claude-sonnet-5"
    assert result["runtime"] == "claude-code"
    assert result["host_user"] == "alice"
    assert result["host_id"] == "host-fingerprint-1"


def test_resolve_agent_identity_reports_fallback_reason_when_host_missing():
    result = ai.resolve_agent_identity(
        name="Alex", role="Review", llm="Claude",
        agent_instance_id="inst-1", raw_user="",
    )
    assert result["host_identity_fallback"] == ai.HOST4_FALLBACK_REASON
    assert result["host4"] == ai.HOST4_FALLBACK


def test_resolve_agent_identity_legacy_unbound_for_missing_fields():
    result = ai.resolve_agent_identity(
        name="Alex", role="Review", llm="Claude",
        agent_instance_id="inst-1", raw_user="alice",
    )
    for field in ("provider", "model", "runtime", "host_id", "run_id", "task_id", "attempt_id", "fence"):
        assert result[field] == "legacy-unbound"


def test_resolve_agent_identity_display_name_never_used_for_uniqueness():
    # Two distinct instances with the same name/role/host/llm collide on the
    # display name by design, but remain distinguishable by agent_instance_id.
    a = ai.resolve_agent_identity(
        name="Alex", role="Review", llm="Claude", agent_instance_id="inst-a", raw_user="alice",
    )
    b = ai.resolve_agent_identity(
        name="Alex", role="Review", llm="Claude", agent_instance_id="inst-b", raw_user="alice",
    )
    assert a["display_name"] == b["display_name"]
    assert a["agent_instance_id"] != b["agent_instance_id"]


def test_display_name_collision_across_different_hosts_and_llms():
    # "Alex Review - #PC1 - Claude" vs "Alex Review - #PC1 - Codex" from the
    # issue: different LLM -> different display name, but same host is fine
    # to collide on role+name when only the llm/instance differ.
    claude_agent = ai.resolve_agent_identity(
        name="Alex", role="Review", llm="Claude", agent_instance_id="inst-claude",
        raw_user="pc1user",
    )
    codex_agent = ai.resolve_agent_identity(
        name="Alex", role="Review", llm="Codex", agent_instance_id="inst-codex",
        raw_user="pc1user",
    )
    assert claude_agent["display_name"] != codex_agent["display_name"]
    assert claude_agent["host4"] == codex_agent["host4"]
    assert claude_agent["agent_instance_id"] != codex_agent["agent_instance_id"]

    # Same display name possible on a *different* host with a different LLM
    # combination that happens to normalize to the same HOST4 + name/role/llm.
    other_host_agent = ai.resolve_agent_identity(
        name="Alex", role="Review", llm="Claude", agent_instance_id="inst-other-host",
        raw_user="pc1user",  # same normalized HOST4 on purpose
    )
    assert other_host_agent["display_name"] == claude_agent["display_name"]
    assert other_host_agent["agent_instance_id"] != claude_agent["agent_instance_id"]


# --------------------------------------------------------------------------- #
# sanitize_field
# --------------------------------------------------------------------------- #
def test_sanitize_field_strips_markdown_html_control_chars():
    assert "`" not in ai.sanitize_field("a`b`c")
    assert "<" not in ai.sanitize_field("<img src=x onerror=alert(1)>")
    assert "\n" not in ai.sanitize_field("line1\nline2")


def test_sanitize_field_truncates_to_max_len():
    long_value = "x" * 100
    assert len(ai.sanitize_field(long_value, max_len=10)) == 10


def test_sanitize_field_handles_none():
    assert ai.sanitize_field(None) == ""
