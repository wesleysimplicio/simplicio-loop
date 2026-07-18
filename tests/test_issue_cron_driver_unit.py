"""Unit tests for ``scripts/issue_cron_driver.classify_status`` and helpers.

Covers the broadened infra-dependency classifier (issue #495-#558 family):
AC1 prefix set, AC2 title prefix match, AC3 label match, AC4 body keyword
match, AC5 intake_ok=False short-circuit, AC6 non-infra Todo.
"""
from __future__ import annotations

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "issue_cron_driver.py")


def _load():
    spec = importlib.util.spec_from_file_location("issue_cron_driver", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


mod = _load()
classify_status = mod.classify_status
_is_infra_dependent = mod._is_infra_dependent


def _issue(title, labels=None, body=""):
    return {"title": title, "labels": labels or [], "body": body}


# AC1: prefix set covers Hub/Supervisor/Async/Architecture/Epic/Performance/ReleaseTrain
def test_prefix_set_covers_domains():
    for p in ("[HUB]", "[SUPERVISOR]", "[ASYNC]", "[ARCHITECTURE]",
              "[EPIC]", "[PERFORMANCE]", "[RELEASE TRAIN]", "[P0][EPIC]"):
        assert p in mod.INFRA_DEPENDENT_DOMAINS, p


# AC2: title prefix -> Blocked
def test_title_prefix_blocked():
    for t in ("[Hub] Implementar daemon singleton",
              "[Supervisor] Backend Rust/Tokio",
              "[Async] Migrar subprocessos",
              "[Architecture] Centralizar processo",
              "[Epic] Criar serviço central de mapas",
              "[Performance] Tornar o núcleo assíncrono",
              "[P0][Release Train] Propagar versão",
              "[EPIC][P0] cross-cutting"):
        assert classify_status(_issue(t), True, []) == "Blocked", t


# AC3: label match -> Blocked
def test_label_blocked():
    for lbls in (["hub"], ["supervisor", "x"], ["async"], ["architecture"],
                  ["epic"], ["performance"], ["release-train"], ["infra"],
                  ["blocked-infra"]):
        assert classify_status(_issue("Qualquer título", lbls), True, []) == "Blocked", lbls


# AC4: body keyword -> Blocked
def test_body_keyword_blocked():
    for kw in ("requer infra", "infra ausente", "não presente neste host",
               "precisa de hub", "precisa de supervisor", "rust/tokio"):
        assert classify_status(_issue("Título", [], f"texto {kw} aqui"), True, []) == "Blocked", kw


# AC5: intake_ok=False -> Blocked (preserved behavior)
def test_intake_failure_blocked():
    assert classify_status(_issue("Título comum não-infra"), False, ["intake_blocked"]) == "Blocked"


# AC6: non-infra + intake_ok -> Todo
def test_non_infra_todo():
    assert classify_status(_issue("Corrigir typo no README"), True, []) == "Todo"
    assert classify_status(_issue("[Docs] Atualizar exemplo"), True, []) == "Todo"


# _is_infra_dependent helper correctness
def test_helper_combos():
    assert _is_infra_dependent(_issue("[Hub] x"))
    assert _is_infra_dependent(_issue("y", ["hub"]))
    assert _is_infra_dependent(_issue("z", [], "precisa de supervisor"))
    assert not _is_infra_dependent(_issue("Docs: fix typo", [], "sem palavra-chave"))


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
