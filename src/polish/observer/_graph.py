"""Turning a first-party file into the facts the store keeps about it.

Three facts per file, and no more: a content digest, the symbols it defines
with a digest each, and the first-party files it imports.

The per-symbol digest is what makes function-level churn possible at all.
SPEC §3.7 justifies the daemon by history — "a function rewritten four times
today is not a dedup candidate" — and that question cannot be answered from
file mtimes, because a file is touched by every keystroke-save and says nothing
about which function moved.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path

SYMBOL_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


@dataclass(frozen=True)
class Symbol:
    """A definition, identified by its dotted path within the file."""

    qualname: str
    kind: str
    lineno: int
    digest: str


@dataclass(frozen=True)
class FileFacts:
    """Everything the store records about one first-party file."""

    path: str
    digest: str
    symbols: tuple[Symbol, ...]
    imports: frozenset[str]
    parsed: bool


def digest_of(data: bytes) -> str:
    """Content hash, used everywhere a "did this actually change?" is asked.

    Kept distinct from the `(mtime, size)` fingerprint on purpose: the
    fingerprint is the cheap screen, the digest is the answer. A rebuild, a
    `touch`, or a checkout that restores identical bytes all move the
    fingerprint without changing the code, and counting those as churn would
    make churn mean "how often was this file written", not "how often was this
    code rewritten".
    """
    return hashlib.sha256(data).hexdigest()


def parse(root: Path, relative: str, module_index: dict[str, str]) -> FileFacts:
    """Read and analyse one file. Never raises on bad input.

    A file that will not parse is still a file the graph has to know about — it
    exists, it has a digest, and it may be imported. Losing the whole record
    because today's save is mid-edit would make the graph flap all day.
    """
    path = root / relative
    try:
        source = path.read_bytes()
    except OSError:
        return FileFacts(relative, "", (), frozenset(), parsed=False)

    digest = digest_of(source)
    try:
        text = source.decode("utf-8")
        tree = ast.parse(text)
    except (SyntaxError, ValueError, UnicodeDecodeError):
        return FileFacts(relative, digest, (), frozenset(), parsed=False)

    lines = text.splitlines()
    symbols = tuple(_symbols(tree, lines))
    imports = frozenset(_imports(tree, relative, module_index))
    return FileFacts(relative, digest, symbols, imports, parsed=True)


def module_index(paths: tuple[str, ...]) -> dict[str, str]:
    """Map importable module names to first-party paths.

    Both `demo/core.py` → `demo.core` and `demo/__init__.py` → `demo`, so that
    `from demo import core` and `import demo` both land somewhere real. Only
    first-party files are indexed, which is what makes the edge set a graph of
    the user's own code rather than of their dependencies.
    """
    index: dict[str, str] = {}
    for path in paths:
        if not path.endswith(".py") and not path.endswith(".pyi"):
            continue
        parts = path.rsplit(".", 1)[0].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            continue
        index.setdefault(".".join(parts), path)
    return index


def _symbols(tree: ast.Module, lines: list[str]):
    """Walk definitions depth-first, carrying the enclosing names.

    Nested definitions get their own entry rather than folding into the parent:
    a helper closure rewritten repeatedly is exactly the churn signal §3.7 is
    after, and it would vanish inside its parent's digest.
    """
    stack: list[tuple[ast.AST, tuple[str, ...]]] = [(tree, ())]
    while stack:
        node, prefix = stack.pop()
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, SYMBOL_NODES):
                continue
            qualname = ".".join((*prefix, child.name))
            yield Symbol(
                qualname=qualname,
                kind="class" if isinstance(child, ast.ClassDef) else "function",
                lineno=child.lineno,
                digest=digest_of("\n".join(_source_lines(child, lines)).encode()),
            )
            stack.append((child, (*prefix, child.name)))


def _source_lines(node: ast.AST, lines: list[str]) -> list[str]:
    """The definition's own text, decorators included.

    `node.lineno` points at the `def`, so a change to a decorator — swapping
    `@lru_cache` for `@cache`, say — would otherwise not register as the symbol
    changing at all.
    """
    start = min((d.lineno for d in getattr(node, "decorator_list", [])), default=None)
    first = min(start, node.lineno) if start else node.lineno
    last = getattr(node, "end_lineno", None) or first
    return lines[first - 1 : last]


def _imports(tree: ast.Module, relative: str, index: dict[str, str]):
    """Yield the first-party paths this file imports.

    Third-party and stdlib imports are dropped rather than recorded as unresolved
    nodes: the graph exists to answer "what else of mine breaks if this changes",
    and nothing of the user's breaks because `json` changed.
    """
    package = _package_of(relative)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield from _resolve(alias.name, index)
        elif isinstance(node, ast.ImportFrom):
            module = _absolute(node, package)
            if module is None:
                continue
            yield from _resolve(module, index)
            for alias in node.names:
                yield from _resolve(
                    f"{module}.{alias.name}" if module else alias.name, index
                )


def _resolve(module: str, index: dict[str, str]):
    """Yield the path `module` names, plus the packages on the way to it.

    `import demo.core.util` makes this file depend on `demo/__init__.py` too:
    that package body runs on import, so a change there reaches this file.
    """
    parts = module.split(".")
    for depth in range(1, len(parts) + 1):
        path = index.get(".".join(parts[:depth]))
        if path is not None:
            yield path


def _absolute(node: ast.ImportFrom, package: str) -> str | None:
    """Resolve `from . import x` / `from ..pkg import y` against the importer."""
    if not node.level:
        return node.module or ""
    parts = package.split(".") if package else []
    climb = node.level - 1
    if climb > len(parts):
        return None
    base = parts[: len(parts) - climb] if climb else parts
    return ".".join((*base, node.module)) if node.module else ".".join(base)


def _package_of(relative: str) -> str:
    parts = relative.rsplit(".", 1)[0].split("/")
    return ".".join(parts[:-1])
