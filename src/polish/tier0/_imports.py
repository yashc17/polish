"""Which unused imports Tier 0 will remove, and the edit that removes them.

The analysis reads `ast`, because an import's own dotted names are not `Name`
nodes there and so cannot be mistaken for uses of themselves. The edit is made
with LibCST (SPEC §3.1), because the file has to come back with its comments,
blank lines and quoting intact — a codemod that reformats the file on its way
past has stopped being a reviewable one-line diff.

The two are joined by counting: the *n*th module-level import statement in the
`ast` body is the *n*th module-level import statement in the LibCST body, in
both cases counting each statement of a semicolon-joined line separately. No
line or column number crosses between the parsers, which matters because they
do not agree on what a column is — `ast` counts UTF-8 bytes and LibCST counts
characters. `_proof.prove()` checks the result either way.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import libcst as cst

from . import _inert, _modules, _proof

#: A package's `__init__.py` is where re-exports live: a name imported and
#: never used there is usually the package's public API, and its consumer is
#: someone else's repository, which no amount of local analysis can see.
PACKAGE_FILES = frozenset({"__init__.py", "__init__.pyi"})

#: Names whose presence means the file can reach a binding without ever
#: mentioning it. Any one of them puts the whole file out of Tier 0's reach.
DYNAMIC_ACCESS = frozenset(
    {"eval", "exec", "globals", "locals", "vars", "__import__", "importlib"}
)

#: An upper bound on strings worth parsing as forward references. A module-level
#: essay is not an annotation, and trying to parse one on every file is waste.
MAX_ANNOTATION = 512

#: A noqa directive in every spelling a linter accepts: bare, with a code,
#: with several codes, with or without the space after the colon.
_NOQA = re.compile(r"#\s*noqa\b", re.IGNORECASE)


@dataclass(frozen=True)
class Removal:
    """One file's worth of unused imports, and the source it becomes."""

    path: str
    before: str
    after: str
    names: tuple[str, ...]
    #: Unused imports Tier 0 declined to remove, each with the reason. Carried
    #: into the commit message: a reviewer who sees one import go and another
    #: stay is owed the difference between them.
    left_alone: tuple[tuple[str, str], ...]


def read_source(path: Path) -> str:
    """Read a source file without translating its line endings.

    `newline=""` on both sides of the edit is what keeps a CRLF file a CRLF
    file. Python's default universal-newline handling would read `\\r\\n` as
    `\\n` and write it back as `\\n`, turning a one-line import removal into a
    diff of every line in the file — which the AST-equivalence proof would
    happily accept, because line endings are not behaviour. They are, however,
    the whole of whether the commit is reviewable.
    """
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def write_source(path: Path, source: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(source)


def candidates(worktree: Path, files: Sequence[str]) -> tuple[Removal, ...]:
    """Every file that has something removable, with the edit already computed.

    Nothing is written here. The caller applies one file at a time so that each
    edit can be proven and type-checked on its own before the next is tried.
    """
    modules = _modules.index(worktree, files)
    inertness = _inert.Inertness(worktree, modules)
    found = (_examine(worktree, relative, modules, inertness) for relative in files)
    return tuple(removal for removal in found if removal is not None)


def _examine(
    worktree: Path,
    relative: str,
    modules: _modules.Modules,
    inertness: _inert.Inertness,
) -> Removal | None:
    if Path(relative).name in PACKAGE_FILES or relative.endswith(".pyi"):
        # A stub file's imports exist to be re-exported; none of them is "used".
        return None
    try:
        before = read_source(worktree / relative)
        tree = _proof.parse(before)
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
        # Unreadable or unparseable: not Polish's file to touch, and saying so
        # by declining is the whole of the handling it needs.
        return None
    if _reaches_names_dynamically(tree):
        return None

    referenced = _referenced_names(tree)
    bindings = Counter(_bound_names(tree))
    lines = before.splitlines()
    targets: dict[int, set[str]] = {}
    removing: list[str] = []
    left_alone: list[tuple[str, str]] = []

    for ordinal, statement in enumerate(_module_level_imports(tree)):
        if _out_of_reach(statement):
            continue
        declared = _declared_deliberate(statement, lines)
        for alias in statement.names:
            name = _proof.bound_name(statement, alias)
            if name in referenced:
                continue
            reason = declared or _unprovable(
                statement, alias, relative, modules, inertness, bindings[name]
            )
            if reason is not None:
                left_alone.append((name, reason))
                continue
            targets.setdefault(ordinal, set()).add(name)
            removing.append(name)

    if not removing:
        return None
    after = _rewrite(before, targets)
    if after is None:
        return None
    return Removal(
        path=relative,
        before=before,
        after=after,
        names=tuple(removing),
        left_alone=tuple(left_alone),
    )


def _module_level_imports(tree: ast.Module) -> Iterator[ast.Import | ast.ImportFrom]:
    """The imports Tier 0 considers: direct children of the module body.

    An import inside a function, a `try` or an `if` is left alone regardless of
    use. Those are deferred imports, optional-dependency probes and
    `TYPE_CHECKING` blocks, and every one of them is a deliberate structure
    whose removal means something other than tidying.
    """
    for statement in tree.body:
        if isinstance(statement, ast.Import | ast.ImportFrom):
            yield statement


def _out_of_reach(statement: ast.Import | ast.ImportFrom) -> bool:
    if isinstance(statement, ast.ImportFrom) and statement.module == "__future__":
        # A future import is never "used" by name and changes how the file is
        # compiled. It is the one import whose removal is always a behaviour change.
        return True
    return any(alias.name == "*" for alias in statement.names)


def _declared_deliberate(
    statement: ast.Import | ast.ImportFrom, lines: Sequence[str]
) -> str | None:
    """Whether the author has already said this unused import is on purpose.

    Two conventions say it in a form a machine can read, and both outrank
    anything Polish can work out for itself:

    * **a noqa directive** on the statement. It exists precisely to silence an
      unused-import warning, so a repository carrying one has a maintainer on
      record saying the import stays. `requests` marks two this way — one of
      them `import encodings.idna`, which is there to register a codec before
      threads can race on it. Both looked perfectly removable to every rule
      below, and removing either would have been a bug.
    * **`import x as x`** — PEP 484's explicit re-export. The redundant alias
      is the whole message.

    Deferring here is not timidity. An unattended agent that overrides an
    explicit human decision has broken the thing that lets it be unattended.
    """
    if any(_NOQA.search(line) for line in _statement_lines(statement, lines)):
        return "the import carries a noqa directive, so its author kept it on purpose"
    if any(alias.asname == alias.name for alias in statement.names):
        return "`import x as x` is an explicit re-export (PEP 484), not an accident"
    return None


def _statement_lines(
    statement: ast.Import | ast.ImportFrom, lines: Sequence[str]
) -> Sequence[str]:
    end = statement.end_lineno or statement.lineno
    return lines[statement.lineno - 1 : end]


def _unprovable(
    statement: ast.Import | ast.ImportFrom,
    alias: ast.alias,
    relative: str,
    modules: _modules.Modules,
    inertness: _inert.Inertness,
    binding_count: int,
) -> str | None:
    """Why this unused binding stays, or None when it can go."""
    name = _proof.bound_name(statement, alias)
    if binding_count > 1:
        return f"`{name}` is bound more than once in this file"
    for dotted in _inert.imported_modules(statement, relative, modules):
        if dotted is None:
            return f"`{name}` comes from a module Polish could not resolve"
        if not inertness.holds(dotted):
            return _why_not(dotted, modules)
    return None


def _why_not(dotted: str, modules: _modules.Modules) -> str:
    if dotted.split(".")[0] in modules.by_name or dotted in modules.by_name:
        return f"importing `{dotted}` runs code, so removing it could change behaviour"
    return (
        f"`{dotted}` is neither first-party nor standard library, and Tier 0 "
        "cannot rule out an import side effect without executing the repository"
    )


def _reaches_names_dynamically(tree: ast.Module) -> bool:
    """True when the file can look a binding up by string rather than by name.

    `globals()["json"]` and `eval("json.dumps(x)")` both reach an import that
    no static reading of the file mentions, and the type check will not see it
    either. One occurrence anywhere puts the file out of reach.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in DYNAMIC_ACCESS:
            return True
        if isinstance(node, ast.FunctionDef) and node.name == "__getattr__":
            return True
    return False


def _referenced_names(tree: ast.Module) -> frozenset[str]:
    """Every name the file mentions, including inside string annotations.

    String constants are parsed as expressions and their names counted, which
    covers forward references (`def f() -> "Decimal"`) and `__all__` entries
    without either needing a special case. It over-counts: a docstring that
    happens to be a bare identifier marks that name used. That direction is
    free — an import kept is an import nobody notices.

    Being written to is not a reference. `json = None` after `import json`
    reads the imported name nowhere, and treating the assignment as a use
    would hide the far more interesting fact that the name is bound twice.
    """
    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if not isinstance(node.ctx, ast.Store):
                referenced.add(node.id)
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            # `x += 1` stores, but it reads first.
            referenced.add(node.target.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            referenced |= _names_in(node.value)
    return frozenset(referenced)


def _names_in(text: str) -> frozenset[str]:
    if not text or len(text) > MAX_ANNOTATION:
        return frozenset()
    try:
        expression = _proof.parse_expression(text)
    except (SyntaxError, ValueError):
        return frozenset()
    return frozenset(
        node.id for node in ast.walk(expression) if isinstance(node, ast.Name)
    )


def _bound_names(tree: ast.Module) -> Iterator[str]:
    """Every name the file binds, at any depth.

    Used only to count: a name bound twice — imported and then assigned, or
    imported in two places — is one whose removal Polish cannot reason about
    locally, so it is left alone.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            yield node.id
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            yield node.name
        elif isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    yield _proof.bound_name(node, alias)


def _rewrite(source: str, targets: dict[int, set[str]]) -> str | None:
    """Delete the named aliases from the *n*th module-level import statements.

    Rebuilding the body rather than visiting it keeps "module level" a
    structural fact — compound statements are copied through untouched, so an
    import nested inside one can never be counted or reached.
    """
    try:
        module = cst.parse_module(source)
    except cst.ParserSyntaxError:
        # LibCST and CPython do not always agree about the newest syntax. A
        # file only one of them can read is a file Polish leaves alone.
        return None

    body: list[cst.BaseStatement] = []
    orphaned: list[cst.EmptyLine] = []
    ordinal = 0

    for item in module.body:
        if not isinstance(item, cst.SimpleStatementLine):
            body.append(_with_leading(item, orphaned))
            orphaned = []
            continue
        kept: list[cst.BaseSmallStatement] = []
        for small in item.body:
            if isinstance(small, cst.Import | cst.ImportFrom):
                dropping = targets.get(ordinal)
                ordinal += 1
                if dropping:
                    survivor = _without(small, dropping)
                    if survivor is None:
                        continue
                    small = survivor
            kept.append(small)
        if not kept:
            # The line is going, but a comment or a shebang above it is not:
            # `#!/usr/bin/env python` on line 1 belongs to whatever follows it.
            orphaned.extend(item.leading_lines)
            continue
        kept[-1] = kept[-1].with_changes(semicolon=cst.MaybeSentinel.DEFAULT)
        body.append(_with_leading(item.with_changes(body=kept), orphaned))
        orphaned = []

    rewritten = module.with_changes(body=body, footer=[*orphaned, *module.footer])
    return rewritten.code


def _with_leading(
    item: cst.BaseStatement, orphaned: Sequence[cst.EmptyLine]
) -> cst.BaseStatement:
    if not orphaned:
        return item
    return item.with_changes(leading_lines=[*orphaned, *item.leading_lines])


def _without(
    statement: cst.Import | cst.ImportFrom, dropping: set[str]
) -> cst.Import | cst.ImportFrom | None:
    if isinstance(statement.names, cst.ImportStar):  # pragma: no cover - never targeted
        return statement
    survivors = [
        alias
        for alias in statement.names
        if _cst_bound_name(statement, alias) not in dropping
    ]
    if not survivors:
        return None
    survivors[-1] = survivors[-1].with_changes(comma=cst.MaybeSentinel.DEFAULT)
    return statement.with_changes(names=survivors)


def _cst_bound_name(
    statement: cst.Import | cst.ImportFrom, alias: cst.ImportAlias
) -> str:
    if alias.asname is not None:
        target = alias.asname.name
        return target.value if isinstance(target, cst.Name) else ""
    dotted = alias.evaluated_name
    return dotted.split(".")[0] if isinstance(statement, cst.Import) else dotted
