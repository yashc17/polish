"""The Tier 0 claim, written as a function so that it can be checked.

SPEC §3.1 lets Tier 0 say "provably behaviour-preserving". That sentence is
only worth something if the proof is a thing the code performs rather than a
thing the documentation asserts, so it lives here on its own terms: given the
file before, the file after, and the bindings the edit claimed to remove,
`prove()` answers whether the second file is the first minus exactly those
bindings and nothing else.

It is a backstop, not the safety analysis. `_imports` decides *which* bindings
can be lost without changing behaviour; this decides whether the edit that
reached the disk did that and only that. A codemod that mangled a
semicolon-joined line, dropped a decorator or rewrote a string literal fails
here even when the analysis behind it was right — which is the point of having
the two answer separately.
"""

from __future__ import annotations

import ast
import warnings
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


def parse(source: str) -> ast.Module:
    """`ast.parse`, without warning about somebody else's code.

    Compiling a file with a stray `"\\p"` in it raises a `SyntaxWarning`, and
    Polish parses every first-party file in the repository — plus, when
    hunting for forward references, every string literal in them. None of that
    is a diagnostic about Polish, and an unattended run's log is not where a
    developer should first hear about their own escape sequences. Their linter
    is. Raises exactly what `ast.parse` raises.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return ast.parse(source)


def parse_expression(source: str) -> ast.Expression:
    """The same, for a string being tried as a forward reference."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return ast.parse(source, mode="eval")


@dataclass(frozen=True)
class Proof:
    """Whether a Tier 0 edit holds, and the sentence that says why.

    `reason` reads as the tail of a claim in both directions, because it is
    quoted into a commit message when the proof holds and into the run log
    when it does not.
    """

    holds: bool
    reason: str

    def __bool__(self) -> bool:
        return self.holds


def bound_name(statement: ast.Import | ast.ImportFrom, alias: ast.alias) -> str:
    """The name an import alias actually binds in the importing module.

    `import os.path` binds `os`, not `os.path`; `from os import path` binds
    `path`. Getting this wrong in either direction is how an unused-import pass
    deletes a name that something still refers to, so the two forms are spelled
    out rather than approximated.
    """
    if alias.asname:
        return alias.asname
    if isinstance(statement, ast.Import):
        return alias.name.split(".")[0]
    return alias.name


def prove(before: str, after: str, *, removed: Sequence[str]) -> Proof:
    """Prove that `after` is `before` minus exactly the `removed` bindings.

    The comparison is between parse trees, not text, so comments, blank lines
    and the layout of what survives are all free to move — none of them changes
    behaviour. Everything that does is compared exactly, in place: the expected
    tree is built by deleting the named bindings from the original body, which
    keeps the surviving imports in their original positions relative to
    everything else. Import order is behaviour, so an edit that reorders one
    fails here even though the same imports survive it.
    """
    wanted = frozenset(removed)
    try:
        original = parse(before)
    except (SyntaxError, ValueError) as failure:
        return Proof(False, f"the original file does not parse ({failure})")
    try:
        edited = parse(after)
    except (SyntaxError, ValueError) as failure:
        return Proof(False, f"the edited file does not parse ({failure})")

    expected, found = _without(original.body, wanted)
    unbound = sorted(wanted - found)
    if unbound:
        return Proof(
            False, f"nothing at module level binds {_names(unbound)} in the original"
        )
    if _dump(expected) != _dump(edited.body):
        return Proof(False, "the edit changed something other than those imports")
    return Proof(True, f"the diff removes {_names(sorted(wanted))} and nothing else")


def _without(
    body: Sequence[ast.stmt], removed: frozenset[str]
) -> tuple[list[ast.stmt], frozenset[str]]:
    """The module body with `removed` deleted from its top-level imports.

    Only module-level imports are considered, because only those are what Tier
    0 offers to remove. An import nested in a function or a `try` reaching this
    set would mean the analysis and the proof disagree about what was edited,
    and the caller is told so rather than having it quietly pass.
    """
    kept: list[ast.stmt] = []
    found: set[str] = set()
    for statement in body:
        if not isinstance(statement, ast.Import | ast.ImportFrom):
            kept.append(statement)
            continue
        survivors = []
        for alias in statement.names:
            if alias.name != "*" and bound_name(statement, alias) in removed:
                found.add(bound_name(statement, alias))
            else:
                survivors.append(alias)
        if survivors:
            kept.append(_rebuilt(statement, survivors))
        elif not statement.names:  # pragma: no cover - unparseable in practice
            kept.append(statement)
    return kept, frozenset(found)


def _rebuilt(
    statement: ast.Import | ast.ImportFrom, names: list[ast.alias]
) -> ast.stmt:
    if isinstance(statement, ast.Import):
        return ast.Import(names=names)
    return ast.ImportFrom(module=statement.module, names=names, level=statement.level)


def _dump(body: Sequence[ast.stmt]) -> str:
    """The body's structure, with positions excluded.

    `ast.dump` leaves out line and column numbers by default, which is exactly
    what is wanted: removing a line moves everything below it, and none of that
    movement is behaviour.
    """
    return ast.dump(ast.Module(body=list(body), type_ignores=[]))


def _names(names: Iterable[str]) -> str:
    quoted = [f"`{name}`" for name in names]
    if len(quoted) <= 2:
        return " and ".join(quoted)
    return f"{', '.join(quoted[:-1])} and {quoted[-1]}"
