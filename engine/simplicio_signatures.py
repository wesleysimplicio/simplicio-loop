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
    python simplicio_signatures.py <file> [<file2> ...] [--raw]
    python simplicio_signatures.py - --lang py   # read stdin
    python simplicio_signatures.py --selftest    # run the built-in self-test
"""

from __future__ import annotations

import ast
import copy
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

# Initializer values are always elided in signature mode. Full source is only
# emitted when the caller explicitly supplies `--raw`.


def _first_docstring_line(node: ast.AST) -> str | None:
    """Return a neutral marker when a node has a docstring.

    Docstring text is implementation content and can contain credentials or
    private URLs. Signature mode preserves the fact that documentation exists
    without copying its literal value.
    """
    try:
        doc = ast.get_docstring(node, clean=True)
    except TypeError:
        return None
    if not doc:
        return None
    return "docstring"


class _ElideConstants(ast.NodeTransformer):
    """Replace literal AST values with an ellipsis marker."""

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        return ast.copy_location(ast.Constant(value=Ellipsis), node)


def _safe_expr(node: ast.AST) -> str:
    """Unparse an expression after removing all literal values."""
    try:
        clean = _ElideConstants().visit(copy.deepcopy(node))
        ast.fix_missing_locations(clean)
        return ast.unparse(clean)
    except Exception:
        return "..."


def _safe_decorator(node: ast.AST) -> str:
    """Keep a decorator's callable shape while eliding call arguments."""
    if isinstance(node, ast.Call):
        return f"{_safe_expr(node.func)}(...)"
    return _safe_expr(node)


def _format_args(node: ast.AST) -> str:
    """Render argument names/types and replace every default with ``...``."""
    args = node.args
    positional = list(getattr(args, "posonlyargs", [])) + list(args.args)
    default_count = len(args.defaults)
    parts: list[str] = []
    for index, arg in enumerate(positional):
        item = arg.arg
        if arg.annotation is not None:
            item += ": " + _safe_expr(arg.annotation)
        if index >= len(positional) - default_count:
            item += " = ..."
        parts.append(item)
        if getattr(args, "posonlyargs", []) and index + 1 == len(args.posonlyargs):
            parts.append("/")
    if args.vararg is not None:
        item = "*" + args.vararg.arg
        if args.vararg.annotation is not None:
            item += ": " + _safe_expr(args.vararg.annotation)
        parts.append(item)
    elif args.kwonlyargs:
        parts.append("*")
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        item = arg.arg
        if arg.annotation is not None:
            item += ": " + _safe_expr(arg.annotation)
        if default is not None:
            item += " = ..."
        parts.append(item)
    if args.kwarg is not None:
        item = "**" + args.kwarg.arg
        if args.kwarg.annotation is not None:
            item += ": " + _safe_expr(args.kwarg.annotation)
        parts.append(item)
    return ", ".join(parts)


def _format_returns(node: ast.AST) -> str:
    ret = getattr(node, "returns", None)
    if ret is None:
        return ""
    try:
        return " -> " + _safe_expr(ret)
    except Exception:
        return ""


def _decorators(node: ast.AST, indent: str) -> list[str]:
    out = []
    for dec in getattr(node, "decorator_list", []):
        try:
            out.append(f"{indent}@{_safe_decorator(dec)}")
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
            bases.append(_safe_expr(b))
        except Exception:
            bases.append("...")
    for kw in node.keywords:
        try:
            bases.append(_safe_expr(kw))
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
    # Initializers are deliberately never rendered. The omission marker keeps
    # the declaration shape while guaranteeing literal minimization for short,
    # multiline and nested values alike.
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
    r"^\s*(?:public|private|protected|internal|static|final|abstract|override|virtual|async|const|readonly)\s+[\w<>,\[\]\s\*&:]+\w+\s*\([^;{]*\)\s*[{:]?.*$",
    # java/c#/c/cpp method or function signature ending in `) {` or `) ->`
    r"^\s*[\w<>,\[\]\*&:\s~]+\b\w+\s*\([^;{}]*\)\s*(?:const\s*)?(?:->[\w<>,\[\]\*&:\s]+)?\s*\{.*$",
]
_SIG_RE = re.compile("|".join(f"(?:{p})" for p in _SIG_PATTERNS))

# Keep these standalone structural lines too.
_KEEP_RE = re.compile(
    r"^\s*(?:import\b|from\s+\S+\s+import\b|#include\b|package\b|use\b|using\b"
    r"|export\s+(?:default\s+)?(?:\{|\*|const|class|function|interface|type|enum)"
    r"|(?:const|let|var|static|final)\b"
    r"|@\w+)"
)


def signatures_regex(source: str) -> str:
    """Signature view via regex with lexical literal/body minimization."""
    out: list[str] = []
    for raw in source.splitlines():
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        if _KEEP_RE.match(line) or _SIG_RE.match(line):
            out.append(_sanitize_regex_line(line))
    return "\n".join(out) + ("\n" if out else "")


def _assignment_index(line: str) -> int | None:
    """Locate a top-level assignment outside strings/comments."""
    quote: str | None = None
    escaped = False
    paren = bracket = brace = 0
    i = 0
    while i < len(line):
        char = line[i]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            i += 1
            continue
        if char in "'\"`":
            quote = char
            i += 1
            continue
        if line.startswith("//", i):
            break
        if char == "#":
            break
        if char == "(":
            paren += 1
        elif char == ")":
            paren = max(0, paren - 1)
        elif char == "[":
            bracket += 1
        elif char == "]":
            bracket = max(0, bracket - 1)
        elif char == "{":
            brace += 1
        elif char == "}":
            brace = max(0, brace - 1)
        elif char == "=" and not (paren or bracket or brace):
            prev = line[i - 1] if i else ""
            nxt = line[i + 1] if i + 1 < len(line) else ""
            if prev not in "=!<>" and nxt not in "=>":
                return i
        i += 1
    return None


_REGEX_STRING_LITERAL_RE = re.compile(r"(['\"`])(?:\\.|(?!\1).)*\1")
_REGEX_DEFAULT_LITERAL_RE = re.compile(
    r"(\b[\w$]+\s*=\s*)(?:[-+]?(?:\d+(?:\.\d*)?|\.\d+)|"
    r"(['\"`])(?:\\.|(?!\2).)*\2)"
)


def _elide_regex_literals(line: str) -> str:
    """Replace literals visible in retained regex lines with omission markers."""
    line = _REGEX_STRING_LITERAL_RE.sub("...", line)
    return _REGEX_DEFAULT_LITERAL_RE.sub(r"\1...", line)


def _sanitize_regex_line(line: str) -> str:
    """Preserve declaration shape while removing initializer/body literals."""
    stripped = line.lstrip()
    value_decl = (
        stripped.startswith(("const ", "static ", "let ", "var ", "final "))
        or " const " in stripped
        or " static " in stripped
        or " final " in stripped
    )
    if value_decl:
        index = _assignment_index(line)
        if index is not None:
            head = line[:index].rstrip()
            if line[index:].rstrip().endswith(";"):
                head += ";"
            return head

    # Remove an inline body only when the brace is outside argument/type
    # brackets and quoted text. Object literals in an initializer were handled
    # above, so they cannot leak values into the signature view.
    quote: str | None = None
    escaped = False
    paren = bracket = 0
    for index, char in enumerate(line):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "(":
            paren += 1
        elif char == ")":
            paren = max(0, paren - 1)
        elif char == "[":
            bracket += 1
        elif char == "]":
            bracket = max(0, bracket - 1)
        elif char == "{" and not (paren or bracket):
            return _elide_regex_literals(line[:index].rstrip() + " { ... }")
    return _elide_regex_literals(line)


def signatures(source: str, lang: str | None) -> str:
    """Dispatch to Python AST or the regex fallback.

    Python syntax errors intentionally use the same sanitized regex path as
    non-Python sources, so parse failure cannot downgrade signature mode into
    a raw-source read.
    """
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


def _process(name: str, source: str, lang: str | None, raw: bool = False) -> str:
    """Build the signature view and emit the savings report to stderr."""
    view = source if raw else signatures(source, lang)
    orig = _count_lines(source)
    sig = _count_lines(view)
    pct = (1 - (sig / orig)) * 100 if orig else 0.0
    mode = "raw" if raw else "signatures"
    sys.stderr.write(
        f"# {mode}[{name}]: {orig} -> {sig} lines ({pct:.0f}% saved)\n"
    )
    return view


def run_cli(argv: list[str]) -> int:
    """CLI entry point. Returns a process exit code."""
    args = list(argv)
    raw = "--raw" in args
    if raw:
        args.remove("--raw")
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
        chunks.append(_process(name, source, lang, raw=raw))

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
