"""TOON (Token-Oriented Object Notation) codec — round-trip losslessness + fallback rules.

`scripts/toon_codec.py` is a plain, dependency-free encode/decode pair for LLM-prompt-facing
payloads (github.com/toon-format/toon). Every JSON-representable value must survive
decode(encode(x)) == x; arrays that can't be tabulated (empty / non-uniform keys / nested
elements) must fall back to compact JSON rather than lose data.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import toon_codec as toon  # noqa: E402


def test_roundtrip_nested_objects():
    value = {
        "user": {"id": 1, "name": "Alice", "address": {"city": "Lisbon", "zip": "1000-001"}},
        "active": True,
    }
    assert toon.decode_toon(toon.encode_toon(value)) == value


def test_uniform_array_of_objects_renders_tabular():
    value = {"items": [
        {"id": 1, "name": "Alice", "role": "admin"},
        {"id": 2, "name": "Bob", "role": "user"},
    ]}
    text = toon.encode_toon(value)
    lines = text.splitlines()
    assert lines[0] == "items[2]{id,name,role}:"
    assert lines[1] == "  1,Alice,admin"
    assert lines[2] == "  2,Bob,user"
    assert toon.decode_toon(text) == value


def test_uniform_array_with_comma_in_field_still_roundtrips():
    value = {"items": [{"id": 1, "name": "Carol, PhD"}, {"id": 2, "name": "Bob"}]}
    text = toon.encode_toon(value)
    assert toon.decode_toon(text) == value


def test_scalar_array_inline():
    value = {"tags": ["a", "b", "c"]}
    text = toon.encode_toon(value)
    assert text == "tags[3]: a,b,c"
    assert toon.decode_toon(text) == value


def test_non_uniform_array_falls_back_to_compact_json():
    # differing keys per element -> not tabular
    value = {"events": [{"type": "login", "user": "a"}, {"type": "click", "x": 1, "y": 2}]}
    text = toon.encode_toon(value)
    assert text == 'events: [{"type":"login","user":"a"},{"type":"click","x":1,"y":2}]'
    assert toon.decode_toon(text) == value


def test_array_with_nested_object_per_element_falls_back():
    # a dict element whose OWN value is a nested dict disqualifies the tabular form
    value = {"rows": [{"id": 1, "meta": {"a": 1}}, {"id": 2, "meta": {"b": 2}}]}
    text = toon.encode_toon(value)
    assert text.startswith("rows: [")
    assert toon.decode_toon(text) == value


def test_empty_array_falls_back_to_compact_json():
    value = {"tags": []}
    text = toon.encode_toon(value)
    assert text == "tags: []"
    assert toon.decode_toon(text) == value


def test_empty_object():
    value = {"config": {}}
    text = toon.encode_toon(value)
    assert text == "config: {}"
    assert toon.decode_toon(text) == value


def test_root_empty_array_and_object():
    assert toon.encode_toon([]) == "[]"
    assert toon.decode_toon("[]") == []
    assert toon.encode_toon({}) == "{}"
    assert toon.decode_toon("{}") == {}


def test_root_uniform_array_of_objects():
    value = [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
    text = toon.encode_toon(value)
    assert text.splitlines()[0] == "[2]{id,name}:"
    assert toon.decode_toon(text) == value


def test_root_scalar_array():
    value = [1, 2, 3, "four"]
    text = toon.encode_toon(value)
    assert text == "[4]: 1,2,3,four"
    assert toon.decode_toon(text) == value


def test_scalars_quoted_only_when_ambiguous():
    value = {
        "has_comma": "a,b",
        "has_colon": "a:b",
        "leading_space": " x",
        "trailing_space": "x ",
        "number_looking": "42",
        "bool_looking": "true",
        "null_looking": "null",
        "empty_string": "",
        "plain": "plain string",
        "int": 3,
        "float": 3.5,
        "none": None,
        "flag": False,
    }
    text = toon.encode_toon(value)
    lines = dict(ln.split(": ", 1) for ln in text.splitlines())
    # unambiguous scalars are NOT quoted
    assert lines["plain"] == "plain string"
    assert lines["int"] == "3"
    assert lines["float"] == "3.5"
    assert lines["none"] == "null"
    assert lines["flag"] == "false"
    # ambiguous strings ARE quoted (JSON-style)
    assert lines["has_comma"] == '"a,b"'
    assert lines["has_colon"] == '"a:b"'
    assert lines["number_looking"] == '"42"'
    assert lines["bool_looking"] == '"true"'
    assert lines["null_looking"] == '"null"'
    assert lines["empty_string"] == '""'
    assert toon.decode_toon(text) == value


def test_mixed_scalar_types_in_inline_array_roundtrip():
    # "Arrays of scalars (numbers/strings/bools) use an inline list" — mixed scalar TYPES are
    # explicitly allowed in the inline form; only dict-array non-uniformity forces a JSON fallback.
    value = {"mixed": [1, "two", True, None]}
    text = toon.encode_toon(value)
    assert text == "mixed[4]: 1,two,true,null"
    assert toon.decode_toon(text) == value


def test_drift_verdict_shaped_payload_roundtrips():
    # the shape actually wired into task_anchor.py's `check --format toon`
    value = {
        "verdict": "INCOMPLETE",
        "reason": "2/3 criteria verified — 1 still open",
        "pending": ["AC3"],
        "coverage": "2/3",
    }
    text = toon.encode_toon(value)
    assert toon.decode_toon(text) == value


def test_unquoted_bracket_looking_string_documented_ambiguity():
    # Documented, deliberate limitation (module docstring + #92 scope item 6): an unquoted string
    # scalar that happens to look like array/object JSON syntax with no comma inside — e.g. the
    # literal string "[1]" — is ambiguous with the array/object JSON-fallback form and decodes as
    # THAT structure instead of the original string. This is a REGRESSION GUARD, not a fix: if
    # this behavior ever silently changes (e.g. someone "fixes" the ambiguity without updating the
    # docstring), this test fails and flags the drift instead of it going unnoticed.
    value = {"weird": "[1]"}
    text = toon.encode_toon(value)
    assert text == "weird: [1]"  # NOT quoted — _needs_quote() doesn't special-case this shape
    decoded = toon.decode_toon(text)
    assert decoded == {"weird": [1]}  # round-trip is LOSSY here: string -> list, by design gap
    assert decoded != value
    # same gap for the plain-brace case ("{}"-looking unquoted string)
    value2 = {"weird": "{}"}
    text2 = toon.encode_toon(value2)
    assert text2 == "weird: {}"
    assert toon.decode_toon(text2) == {"weird": {}}
    assert toon.decode_toon(text2) != value2


def test_selftest_subcommand_passes():
    import subprocess
    r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "toon_codec.py"), "selftest"],
                       capture_output=True, text=True, cwd=REPO)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_toon_codec")
