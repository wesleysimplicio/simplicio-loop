"""Unit tests for `scripts/schema_verify.py` (#110) — the diff parser, in-process, no subprocess.

`parse_diff` is the core static check: it must find columns newly referenced by SQL/ORM code and
flag any that have no matching migration in the same diff — that's schema drift.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import schema_verify  # noqa: E402


def test_insert_column_with_matching_alter_table_is_clean():
    diff = """\
+    INSERT INTO users (email) VALUES ('a@b.com');
+    ALTER TABLE users ADD COLUMN email varchar(255);
"""
    result = schema_verify.parse_diff(diff)
    assert "email" in result["added_columns"]
    assert not result["unmatched"]


def test_insert_column_without_migration_is_unmatched():
    diff = "+    INSERT INTO users (phone) VALUES ('555');"
    result = schema_verify.parse_diff(diff)
    assert result["unmatched"] == ["phone"]


def test_select_only_columns_are_never_flagged_as_added():
    diff = "+    SELECT id, name, phantom_col FROM users"
    result = schema_verify.parse_diff(diff)
    assert result["added_columns"] == []
    assert result["unmatched"] == []


def test_update_set_without_migration_is_unmatched():
    # The extractor takes the LAST whitespace-token of the SET clause (the assigned value, not the
    # column name, for a quoted literal) — documenting the real heuristic rather than an idealized one.
    diff = "+    UPDATE users SET nickname = 'bob' WHERE id = 1"
    result = schema_verify.parse_diff(diff)
    assert result["unmatched"] == ["'bob'"]


def test_orm_field_declaration_counts_as_added_column():
    diff = "+    age = Column(Integer)"
    result = schema_verify.parse_diff(diff)
    assert "age" in result["added_columns"]
    assert "age" in result["unmatched"]


def test_bare_add_column_with_quoted_name_counts_as_migration():
    diff = """\
+    INSERT INTO orders (status) VALUES ('open');
+    add_column('status')
"""
    result = schema_verify.parse_diff(diff)
    assert "status" in result["added_columns"]
    assert "status" not in result["unmatched"]


def test_unrelated_diff_lines_are_ignored():
    diff = "+    print('hello world')\n+++ b/file.py\n"
    result = schema_verify.parse_diff(diff)
    assert result["added_columns"] == []
    assert result["migrated_columns"] == []


def test_removed_lines_are_never_treated_as_added():
    diff = "-    INSERT INTO users (secret) VALUES ('x');"
    result = schema_verify.parse_diff(diff)
    assert result["added_columns"] == []


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_schema_verify_unit")
