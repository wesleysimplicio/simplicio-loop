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

    def test_python_parse_failure_uses_same_safe_policy(self):
        source = '''
@route("https://user:pass@example.test/token")
def fetch(user: str = "Bearer synthetic.jwt.value"):
    return "PARSE_FAILURE_BODY"
this is intentionally invalid Python
'''
        view = signatures(source, "py")
        for literal in LITERALS + ("PARSE_FAILURE_BODY",):
            self.assertNotIn(literal, view)
        self.assertIn("@route(...)", view)
        self.assertIn("def fetch(user: str = ...)", view)

    def test_regex_signature_defaults_and_decorators_elide_values(self):
        source = '''
@route("https://user:pass@example.test/token")
export function fetch(user: string = "Bearer synthetic.jwt.value") {
  return "PARSE_FAILURE_BODY";
}
'''
        view = signatures(source, "js")
        for literal in LITERALS + ("PARSE_FAILURE_BODY",):
            self.assertNotIn(literal, view)
        self.assertIn("@route(...)", view)
        self.assertIn("fetch(user: string = ...)", view)

    def test_supported_regex_languages_preserve_signatures_without_literals(self):
        fixtures = {
            "go": 'const token = "AKIA_SYNTHETIC"\nfunc Fetch(user string) { return "BODY" }\n',
            "java": 'final String token = "AKIA_SYNTHETIC";\npublic String fetch(String user) { return "BODY"; }\n',
            "rb": 'def fetch(user = "AKIA_SYNTHETIC")\n  "BODY"\nend\n',
            "php": 'const TOKEN = "AKIA_SYNTHETIC";\nfunction fetch($user = "BODY") { return $user; }\n',
            "c": 'const char *token = "AKIA_SYNTHETIC";\nint fetch(int user) { return user; }\n',
            "cpp": 'const char *token = "AKIA_SYNTHETIC";\nint fetch(int user) { return user; }\n',
        }
        for lang, source in fixtures.items():
            with self.subTest(lang=lang):
                view = signatures(source, lang)
                self.assertNotIn("AKIA_SYNTHETIC", view)
                self.assertNotIn("BODY", view)
                self.assertIn("fetch", view.lower())


if __name__ == "__main__":
    unittest.main()
