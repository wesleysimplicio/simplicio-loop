"""Regression fixtures shared with Runtime issue #5643."""

import unittest

from engine.simplicio_signatures import signatures


LITERALS = (
    "AKIA_SYNTHETIC",
    "sk_test_synthetic",
    "https://user:pass@example.test/token",
    "Bearer synthetic.jwt.value",
    "-----BEGIN PRIVATE KEY-----",
    "4242-4242-4242-4242",
)


class SignatureLiteralMinimizationTests(unittest.TestCase):
    def test_python_ast_preserves_shape_without_values(self):
        source = '''
"""module secret AKIA_SYNTHETIC"""
from dataclasses import dataclass

TOKEN: str = "AKIA_SYNTHETIC"

@route("https://user:pass@example.test/token")
def fetch(user: str = "Bearer synthetic.jwt.value") -> str:
    value: str = "sk_test_synthetic"
    return value

class Client:
    card: str = "4242-4242-4242-4242"
'''
        view = signatures(source, "py")
        for literal in LITERALS:
            self.assertNotIn(literal, view)
        for name in ("TOKEN", "fetch", "user", "Client", "card"):
            self.assertIn(name, view)
        self.assertIn("user: str = ...", view)
        self.assertIn("# \"docstring\"", view)

    def test_regex_languages_elide_exported_and_nested_initializers(self):
        source = '''
export const TOKEN: string = "AKIA_SYNTHETIC";
export function fetch(user: string) {
  const secret = "sk_test_synthetic";
  let url = "https://user:pass@example.test/token";
  return `Bearer synthetic.jwt.value`;
}
'''
        view = signatures(source, "ts")
        for literal in LITERALS:
            self.assertNotIn(literal, view)
        for name in ("TOKEN", "fetch", "secret", "url"):
            self.assertIn(name, view)

    def test_no_declarations_stays_empty_and_raw_is_caller_selected(self):
        source = "prose containing AKIA_SYNTHETIC\n"
        self.assertEqual(signatures(source, "ts"), "")
        # The public CLI's --raw path is intentionally separate from this
        # signature-only function; no implicit fallback can leak the source.


if __name__ == "__main__":
    unittest.main()
