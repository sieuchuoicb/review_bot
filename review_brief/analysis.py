"""Function-level view of a Python change, from the AST.

Answers two questions the checkers share:
  - which functions did the PR add, modify or remove?
  - who calls a given function elsewhere in the repo?

Callers are matched by name (`total(...)` or `x.total(...)`), which is an
approximation: Python is dynamic and two classes can share a method name.
That is good enough to point a reviewer at the right places. A real
code-intelligence or service-graph agent would replace this module.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from review_brief.diff import ChangedFile

_SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", "build", "dist"}


@dataclass
class FuncInfo:
    qualname: str  # "Invoice.total" or "monthly_revenue"
    name: str
    class_name: str | None
    start: int  # first line (decorators included)
    end: int
    source: str

    @property
    def is_public(self) -> bool:
        return not self.name.startswith("_")


@dataclass
class FunctionChange:
    file: str
    kind: str  # new | modified | removed
    func: FuncInfo  # head version (base version for removed)
    base_func: FuncInfo | None = None


@dataclass
class CallSite:
    file: str
    line: int
    caller: str  # enclosing function qualname, or "<module>"
    code: str


def index_functions(source: str) -> dict[str, FuncInfo]:
    """Top-level functions and methods of top-level classes."""
    tree = ast.parse(source)
    lines = source.splitlines()
    out: dict[str, FuncInfo] = {}

    def add(node: ast.FunctionDef | ast.AsyncFunctionDef, class_name: str | None) -> None:
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        end = node.end_lineno or node.lineno
        qual = f"{class_name}.{node.name}" if class_name else node.name
        out[qual] = FuncInfo(qual, node.name, class_name, start, end, "\n".join(lines[start - 1 : end]))

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node, None)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add(item, node.name)
    return out


def class_source(source: str, class_name: str) -> tuple[int, str] | None:
    tree = ast.parse(source)
    lines = source.splitlines()
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            return start, "\n".join(lines[start - 1 : node.end_lineno])
    return None


def function_changes(files: list[ChangedFile]) -> list[FunctionChange]:
    """Files that do not parse are skipped; the quality checker reports them."""
    changes: list[FunctionChange] = []
    for f in files:
        if not f.path.endswith(".py") or not f.exact:
            continue
        try:
            head = index_functions(f.new_text) if f.new_text is not None else {}
            base = index_functions(f.old_text) if f.old_text is not None else {}
        except SyntaxError:
            continue
        for qual, fn in head.items():
            if qual not in base:
                changes.append(FunctionChange(f.path, "new", fn))
                continue
            old = base[qual]
            touched_head = any(fn.start <= n <= fn.end for n in f.added)
            touched_base = any(old.start <= n <= old.end for n in f.removed)
            if touched_head or touched_base:
                changes.append(FunctionChange(f.path, "modified", fn, base_func=old))
        for qual, old in base.items():
            if qual not in head:
                changes.append(FunctionChange(f.path, "removed", old, base_func=old))
    return changes


def is_test_file(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    in_test_dir = bool({"tests", "test"}.intersection(parts[:-1]))
    return in_test_dir or name.startswith("test_") or name.endswith("_test.py")


def iter_python_files(root: Path, include_tests: bool = True):
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if _SKIP_DIRS.intersection(rel.split("/")):
            continue
        if not include_tests and is_test_file(rel):
            continue
        yield rel, path


class _CallFinder(ast.NodeVisitor):
    def __init__(self, name: str):
        self.name = name
        self.stack: list[str] = []
        self.hits: list[tuple[int, str]] = []

    def _scoped(self, node: ast.AST, label: str) -> None:
        self.stack.append(label)
        self.generic_visit(node)
        self.stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scoped(node, node.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scoped(node, node.name)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Call(self, node: ast.Call) -> None:
        fn = node.func
        called = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
        if called == self.name:
            self.hits.append((node.lineno, ".".join(self.stack) or "<module>"))
        self.generic_visit(node)


def find_callers(root: Path, name: str, exclude_qualname: str | None = None) -> list[CallSite]:
    """Call sites of `name` in non-test code. Skips files that do not parse."""
    sites: list[CallSite] = []
    for rel, path in iter_python_files(root, include_tests=False):
        try:
            source = path.read_text()
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError):
            continue
        finder = _CallFinder(name)
        finder.visit(tree)
        lines = source.splitlines()
        for line, caller in finder.hits:
            if caller == exclude_qualname:
                continue  # recursion is not an external caller
            sites.append(CallSite(rel, line, caller, lines[line - 1].strip()))
    return sites
