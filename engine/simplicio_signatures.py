#!/usr/bin/env python3
"""simplicio_signatures — stdlib-only "signatures-only reads" for token economy.

Given a source file, emit ONLY its structural skeleton: imports, class/function/
method signatures (with the FIRST line of each docstring), and top-level
constants/assignments. All function and method bodies are stripped to `...`.

Goal: turn a 600-line file into ~40 lines, saving 80-95% of the tokens needed
to read and navigate it, while keeping the structure intact.

Python (.py): uses the `ast` module for robust extraction.
Other langs: regex fallback that keeps signature-like lines.

CLI:
    python simplicio_signatures.py <file> [<file2> ...]
    python simplicio_signatures.py - --lang py   # read stdin
    python simplicio_signatures.py --selftest    # run the built-in self-test
"""

from __future__ import annotations

import ast
import os
import re
import sys

# Extension -> language tag for the regex fallback.
_LANG_BY_EXT = {
    ".py": "py",
    ".js": "js",
    ".jsx": "js",
    ".mjs": "js",
    ".cjs": "js",
    ".ts": "ts",
    ".tsx": "ts",
    ".go": "go",
    ".java": "java",
    ".rb": "rb",
    ".php": "php",
    ".c": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".h": "c",
    ".hpp": "cpp",
}

# Keep top-level assignments whose value is short or a simple literal; otherwise
# the value is elided to `...` to avoid leaking large bodies/data.
_MAX_ASSIGN_REPR = 60


def _first_docstring_line(node: ast.AST) -> str | None:
    """Return the first non-empty line of a node's docstring, or None."""
    try:
        doc = ast.get_docstring(node, clean=True)
    except TypeError:
        return None
    if not doc:
        return None
    for line in doc.splitlines():
        line = line.strip()
        if line:
            return line
    return None


def _format_args(node: ast.AST) -> str:
    """Render the argument list of a function via ast.unparse (real signature)."""
    try:
        return ast.unparse(node.args)
    except Exception:
        return "..."


def _format_returns(node: ast.AST) -> str:
    ret = getattr(node, "returns", None)
    if ret is None:
        return ""
    try:
        return " -> " + ast.unparse(ret)
    except Exception:
        return ""


def _decorators(node: ast.AST, indent: str) -> list[str]:
    out = []
    for dec in getattr(node, "decorator_list", []):
        try:
            out.append(f"{indent}@{ast.unparse(dec)}")
        except Exception:
            out.append(f"{indent}@<decorator>")
    return out


def _body_line_count(node: ast.AST) -> int:
    """Approximate source line span of a function body for the `# <N lines>` note."""
    body = getattr(node, "body", None)
    if not body:
        return 0
    first = body[0]
    last = body[-1]
    start = getattr(first, "lineno", None)
    end = getattr(last, "end_lineno", None) or getattr(last, "lineno", None)
    if start is None or end is None:
        return 0
    return max(0, end - start + 1)


def _func_lines(node: ast.AST, indent: str) -> list[str]:
    """Emit a function/method signature block (decorators, def, docstring, body)."""
    lines: list[str] = []
    lines.extend(_decorators(node, indent))
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    sig = f"{indent}{prefix} {node.name}({_format_args(node)}){_format_returns(node)}:"
    lines.append(sig)
    body_indent = indent + "    "
    doc = _first_docstring_line(node)
    if doc:
        lines.append(f'{body_indent}# "{doc}"')
    n = _body_line_count(node)
    if n > 1:
        lines.append(f"{body_indent}...  # {n} lines")
    else:
        lines.append(f"{body_indent}...")
    return lines


def _class_lines(node: ast.ClassDef, indent: str) -> list[str]:
    """Emit a class signature block: bases, docstring, nested members."""
    lines: list[str] = []
    lines.extend(_decorators(node, indent))
    bases = []
    for b in node.bases:
        try:
            bases.append(ast.unparse(b))
        except Exception:
            bases.append("...")
    for kw in node.keywords:
        try:
            bases.append(ast.unparse(kw))
        except Exception:
            pass
    base_str = f"({', '.join(bases)})" if bases else ""
    lines.append(f"{indent}class {node.name}{base_str}:")
    body_indent = indent + "    "
    doc = _first_docstring_line(node)
    if doc:
        lines.append(f'{body_indent}# "{doc}"')

    member_lines = _members(node.body, body_indent)
    if member_lines:
        lines.extend(member_lines)
    else:
        lines.append(f"{body_indent}...")
    return lines


def _assign_targets(node: ast.AST) -> list[str]:
    """Return target names for an assignment (only plain Name targets)."""
    names: list[str] = []
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                names.append(t.id)
    elif isinstance(node, ast.AnnAssign):
        if isinstance(node.target, ast.Name):
            names.append(node.target.id)
    return names


def _assign_line(node: ast.AST, indent: str) -> str | None:
    """Render a simple top-level/class-level assignment, eliding big values."""
    names = _assign_targets(node)
    if not names:
        return None
    annotation = ""
    if isinstance(node, ast.AnnAssign):
        try:
            annotation = ": " + ast.unparse(node.annotation)
        except Exception:
            annotation = ""
    value = getattr(node, "value", None)
    if value is None:
        return f"{indent}{names[0]}{annotation}"
    try:
        rendered = ast.unparse(value)
    except Exception:
        rendered = "..."
    if len(rendered) > _MAX_ASSIGN_REPR or "\n" in rendered:
        rendered = "..."
    target = ", ".join(names) if len(names) > 1 else names[0]
    return f"{indent}{target}{annotation} = {rendered}"


def _members(body: list[ast.AST], indent: str) -> list[str]:
    """Render the relevant members of a class/module body in source order."""
    lines: list[str] = []
    for child in body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines.extend(_func_lines(child, indent))
        elif isinstance(child, ast.ClassDef):
            lines.extend(_class_lines(child, indent))
        elif isinstance(child, (ast.Assign, ast.AnnAssign)):
            line = _assign_line(child, indent)
            if line is not None:
                lines.append(line)
    return lines


def _imports(tree: ast.Module) -> list[str]:
    """Collect module-level import / from-import statements, in order."""
    lines: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            try:
                lines.append(ast.unparse(node))
            except Exception:
                continue
    return lines


def signatures_python(source: str) -> str:
    """Produce the signature view of Python source via the ast module."""
    tree = ast.parse(source)
    out: list[str] = []

    mod_doc = _first_docstring_line(tree)
    if mod_doc:
        out.append(f'# "{mod_doc}"')

    imports = _imports(tree)
    if imports:
        out.extend(imports)
        out.append("")

    members = _members(tree.body, "")
    out.extend(members)

    # Drop a trailing blank line if present.
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"


# Regex fallback for non-Python (and Python on parse error).
_SIG_PATTERNS = [
    # def / async def (python-ish)
    r"^\s*(?:async\s+)?def\s+\w+\s*\(",
    # class / interface / struct / enum / type / trait / impl
    r"^\s*(?:export\s+)?(?:default\s+)?(?:public\s+|private\s+|protected\s+|internal\s+|abstract\s+|final\s+|sealed\s+|static\s+|pub\s+)*"
    r"(?:class|interface|struct|enum|trait|impl|type|namespace|module|record)\b",
    # function declarations (js/ts/php) and go funcs
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*\w*\s*\(",
    r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+\w+",
    r"^\s*func\s+(?:\([^)]*\)\s*)?\w+\s*\(",
    # method-ish / arrow funcs assigned to a name
    r"^\s*(?:export\s+)?(?:const|let|var)\s+\w+\s*[:=].*=>\s*\{?\s*$",
    r"^\s*(?:public|private|protected|internal|static|final|abstract|override|virtual|async|const|readonly)\s+[\w<>,\[\]\s\*&:]+\w+\s*\([^;{]*\)\s*[{:]?\s*$",
    # java/c#/c/cpp method or function signature ending in `) {` or `) ->`
    r"^\s*[\w<>,\[\]\*&:\s~]+\b\w+\s*\([^;{}]*\)\s*(?:const\s*)?(?:->[\w<>,\[\]\*&:\s]+)?\s*\{\s*$",
]
_SIG_RE = re.compile("|".join(f"(?:{p})" for p in _SIG_PATTERNS))

# Keep these standalone structural lines too.
_KEEP_RE = re.compile(
    r"^\s*(?:import\b|from\s+\S+\s+import\b|#include\b|package\b|use\b|using\b"
    r"|export\s+(?:default\s+)?(?:\{|\*|const|class|function|interface|type|enum)"
    r"|@\w+)"
)


def signatures_regex(source: str) -> str:
    """Signature view via regex: keep signature-like + structural lines only."""
    out: list[str] = []
    for raw in source.splitlines():
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        if _KEEP_RE.match(line) or _SIG_RE.match(line):
            out.append(line)
    return "\n".join(out) + ("\n" if out else "")


def signatures(source: str, lang: str | None) -> str:
    """Dispatch to the python or regex extractor. Fail-open on parse errors."""
    if lang == "py":
        try:
            return signatures_python(source)
        except SyntaxError:
            return signatures_regex(source)
        except Exception:
            return signatures_regex(source)
    return signatures_regex(source)


def _lang_for(path: str) -> str | None:
    return _LANG_BY_EXT.get(os.path.splitext(path)[1].lower())


def _count_lines(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _process(name: str, source: str, lang: str | None) -> str:
    """Build the signature view and emit the savings report to stderr."""
    view = signatures(source, lang)
    orig = _count_lines(source)
    sig = _count_lines(view)
    pct = (1 - (sig / orig)) * 100 if orig else 0.0
    sys.stderr.write(
        f"# signatures[{name}]: {orig} -> {sig} lines ({pct:.0f}% saved)\n"
    )
    return view


def run_cli(argv: list[str]) -> int:
    """CLI entry point. Returns a process exit code."""
    args = list(argv)
    forced_lang: str | None = None
    if "--lang" in args:
        i = args.index("--lang")
        try:
            forced_lang = args[i + 1]
            del args[i : i + 2]
        except IndexError:
            sys.stderr.write("error: --lang needs a value\n")
            return 2

    files = [a for a in args if not a.startswith("--")] or ["-"]
    chunks: list[str] = []
    for path in files:
        if path == "-":
            source = sys.stdin.read()
            lang = forced_lang
            if lang is None:
                sys.stderr.write("error: reading stdin needs --lang <py|js|...>\n")
                return 2
            name = "<stdin>"
        else:
            if not os.path.isfile(path):
                sys.stderr.write(f"error: not a file: {path}\n")
                return 2
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                source = fh.read()
            lang = forced_lang or _lang_for(path)
            name = path
        chunks.append(_process(name, source, lang))

    sys.stdout.write("\n".join(chunks))
    return 0


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #

_TEMP_TEMPLATE = '''\
"""Generated module for the signatures self-test."""
import os
import sys
from collections import OrderedDict

MODULE_CONST = 42
LONG_DATA = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]


class Base:
    """Base class docstring."""

    attr = "x"

    def method_base(self, a, b=1):
        """Base method."""
        UNIQUE_BODY_MARKER_ZZZ = a + b
        return UNIQUE_BODY_MARKER_ZZZ


def func_{n}(arg, *args, kw=None, **kwargs) -> int:
    """Docstring for func_{n}."""
    UNIQUE_BODY_MARKER_ZZZ = arg
    for _ in range(10):
        UNIQUE_BODY_MARKER_ZZZ += 1
    return UNIQUE_BODY_MARKER_ZZZ


class Klass_{n}(Base):
    """Class docstring {n}."""

    def m_a(self):
        UNIQUE_BODY_MARKER_ZZZ = 1
        return UNIQUE_BODY_MARKER_ZZZ

    async def m_b(self, x: int) -> str:
        UNIQUE_BODY_MARKER_ZZZ = str(x)
        return UNIQUE_BODY_MARKER_ZZZ
'''


def _make_temp_module() -> str:
    """Generate a ~300-line module with ~25 funcs/classes for the fallback test."""
    blocks = [_TEMP_TEMPLATE.split("\n\n\n", 1)[0] + "\n"]
    body = _TEMP_TEMPLATE.split("\n\n\n", 1)[1]
    for n in range(12):
        blocks.append(body.replace("{n}", str(n)))
    return "\n\n".join(blocks)


def selftest() -> int:
    """Run assertions against the real dashboard file (or a temp module)."""
    real = "/Users/wesleysimplicio/Projetos/ai/simplicio-loop/hooks/simplicio_dashboard.py"
    failures: list[str] = []

    if os.path.isfile(real):
        with open(real, "r", encoding="utf-8", errors="replace") as fh:
            source = fh.read()
        target_name = real
    else:
        source = _make_temp_module()
        target_name = "<generated temp module>"

    orig_lines = _count_lines(source)
    view = signatures(source, "py")
    sig_lines = _count_lines(view)
    pct = (1 - (sig_lines / orig_lines)) * 100 if orig_lines else 0.0

    # (1) output < 45% of original
    if not (sig_lines < 0.45 * orig_lines):
        failures.append(
            f"line ratio {sig_lines}/{orig_lines} = {sig_lines / orig_lines:.0%} "
            f"is not < 45%"
        )

    # (2) every top-level def/class name from the original appears in the output
    tree = ast.parse(source)
    top_names = [
        n.name
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    missing = [name for name in top_names if name not in view]
    if missing:
        failures.append(f"missing top-level names in output: {missing}")

    # (3) no function-body statement leaks
    if "UNIQUE_BODY_MARKER_ZZZ" in source:
        if "UNIQUE_BODY_MARKER_ZZZ" in view:
            failures.append("temp body marker leaked into signature view")
    if os.path.isfile(real):
        if "self.wfile.write" in view:
            failures.append("body-only token 'self.wfile.write' leaked")
        if "def do_GET" not in view:
            failures.append("expected signature 'def do_GET' missing from output")

    status = "PASS" if not failures else "FAIL"
    sys.stderr.write(
        f"# selftest: {status} target={target_name} "
        f"{orig_lines} -> {sig_lines} lines ({pct:.0f}% saved)\n"
    )
    for f in failures:
        sys.stderr.write(f"#   - {f}\n")

    if not failures:
        sys.stdout.write(
            f"selftest PASS: {orig_lines} -> {sig_lines} lines ({pct:.0f}% saved)\n"
        )
        return 0
    sys.stdout.write(f"selftest FAIL: {failures}\n")
    return 1


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    if not argv:
        sys.stderr.write(__doc__ or "")
        return 2
    return run_cli(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
