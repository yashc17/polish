"""Coverage at the granularity question #4 settled on: per symbol, per path.

Issue #13 asks for coverage "at a granularity finer than the whole repo", and
question #4 turned that from a nicety into the input to the tiering rule itself:
*an edit's tier is the lowest coverage tier among the symbols it touches*. So
what this module produces is not a report — it is the table that rule is looked
up in. The tier probe's single per-repository verdict is a separate and known
gap, tracked in #28, and deliberately not closed here.

The denominator comes from parsing, and the numerator from the trace. That split
matters: a symbol nothing ever called has to be *visible* as uncovered, and a
table built only from what executed would have quietly dropped it and reported
100% of a fraction.

Symbols are enumerated the way `co_qualname` spells them — `Class.method`,
`Outer.Inner.method` — because the tracer reports what CPython says and the two
sides have to meet. Functions defined inside other functions are skipped on both
sides: CPython spells them with `<locals>`, and there is no name an oracle could
call them by.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Sequence
from pathlib import Path

from polish.probe import modules

from ._result import Coverage, Oracle, SymbolCoverage


def measure(
    root: Path,
    first_party: Sequence[str],
    *,
    executed: Iterable[str],
    oracles: Sequence[Oracle],
) -> Coverage:
    """Build the per-symbol table from the tree, the trace and the survivors."""
    root = Path(root)
    ran = set(executed)
    counts: dict[str, int] = {}
    for oracle in oracles:
        counts[oracle.symbol] = counts.get(oracle.symbol, 0) + 1

    paths: dict[str, str] = {}
    found: dict[str, SymbolCoverage] = {}

    for module in modules(root, first_party):
        paths[module.name] = module.path
        for qualname in _defined(root / module.path):
            name = f"{module.name}.{qualname}"
            found[name] = SymbolCoverage(
                path=module.path,
                module=module.name,
                qualname=qualname,
                executed=name in ran,
                oracles=counts.get(name, 0),
            )

    # Anything the trace saw that the parse did not. A symbol built by a
    # decorator or a metaclass is still a symbol that ran, and dropping it here
    # would let `tier_of` answer Tier 0 about code that has a live oracle.
    for name in sorted(ran | set(counts)):
        if name in found:
            continue
        module = _module_of(name, paths)
        found[name] = SymbolCoverage(
            path=paths.get(module, ""),
            module=module,
            qualname=name[len(module) + 1 :] if module else name,
            executed=name in ran,
            oracles=counts.get(name, 0),
        )

    return Coverage(symbols=tuple(found[name] for name in sorted(found)))


def _defined(path: Path) -> list[str]:
    """Every addressable function in one file, spelled as `co_qualname` does.

    A file that will not parse contributes nothing rather than raising: it is
    still a first-party file, the trace may still have executed something in it,
    and an unattended run must not end because one file is mid-edit.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return []

    found: list[str] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in getattr(node, "body", []):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                found.append(prefix + child.name)
            elif isinstance(child, ast.ClassDef):
                # Into classes, never into functions: a nested function is
                # `outer.<locals>.inner` and cannot be called from a test.
                walk(child, f"{prefix}{child.name}.")

    walk(tree, "")
    return found


def _module_of(name: str, paths: dict[str, str]) -> str:
    """The longest known module that prefixes a dotted symbol name."""
    parts = name.split(".")
    for end in range(len(parts) - 1, 0, -1):
        candidate = ".".join(parts[:end])
        if candidate in paths:
            return candidate
    return ""
