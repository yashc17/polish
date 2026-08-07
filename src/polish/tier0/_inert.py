"""Whether importing a module can be proven to do nothing.

This is the rule that keeps Tier 0's claim literally true. "Unused" is not
"removable": importing a module runs it, and running it can register a plugin,
patch a builtin, set a matplotlib backend or open a socket. None of that is
visible in the file being edited, and Tier 0 is by definition the tier that
cannot execute the repository to find out.

So an unused import is removable only when the module behind it is *provably*
inert:

* the standard library, minus the handful of modules whose import **is** the
  point (`readline` rebinds `input()`, `site` runs arbitrary startup code);
* a first-party module whose top level is only definitions, literals and
  further inert imports.

Everything else — every third-party import, every first-party module that runs
a decorator or calls something at import time — is left alone with the reason
recorded. Skipping a removable import costs nothing; removing a load-bearing
one costs the product, which is the same asymmetry first-party detection is
built around (SPEC §1.1).
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path

from . import _modules, _proof
from ._modules import Modules

#: Standard-library modules that do something observable when imported. The bar
#: is that the import itself changes the process, not that the module can.
#:
#: This is a denylist, and a denylist is the wrong shape for a
#: precision-first rule: it is only as good as what is on it. It is here
#: because the alternative — proving inertness from the standard library's own
#: source, the way first-party modules are proven — rejects `os` and `json`
#: along with everything else, which would leave Tier 0 with nothing to do.
#: The noqa rule in `_imports` is the real backstop, since an import kept
#: for its side effect is nearly always marked as one.
SIDE_EFFECTING_STDLIB = frozenset(
    {
        "antigravity",  # opens a web browser
        "this",  # prints to stdout
        "readline",  # rebinds input() and installs a history hook
        "rlcompleter",  # installs a completer into readline
        "site",  # runs .pth files and site customisation
        "sitecustomize",
        "usercustomize",
        # Every `encodings.*` submodule exists to register a codec. `requests`
        # imports `encodings.idna` at module level for exactly that reason,
        # and dogfooding #11 against it removed the import before this entry
        # existed. See docs/adr/0005.
        "encodings",
    }
)


class Inertness:
    """Proves — or declines to prove — that importing a module runs nothing.

    Answers are memoised per file, and a module that is already being examined
    counts as inert while that examination runs. Circular imports are ordinary
    in real packages, and the assumption is sound because every statement of
    every module in the cycle is still examined exactly once: a side effect
    anywhere in the cycle is still found.
    """

    def __init__(self, worktree: Path, modules: Modules) -> None:
        self._worktree = worktree
        self._modules = modules
        self._proven: dict[str, bool] = {}
        self._examining: set[str] = set()

    def holds(self, dotted: str | None) -> bool:
        """True when importing `dotted`, and every package above it, runs nothing.

        The parents matter: `import demo.plugins.slack` executes `demo`,
        `demo.plugins` and `demo.plugins.slack`, so a registration sitting in
        any of the three is a registration the removal would undo.
        """
        if dotted is None:
            return False
        parts = dotted.split(".")
        return all(
            self._one(".".join(parts[:depth])) for depth in range(1, len(parts) + 1)
        )

    def _one(self, dotted: str) -> bool:
        relative = self._modules.by_name.get(dotted)
        if relative is not None:
            return self._source_is_inert(relative)
        root = dotted.split(".")[0]
        if root in self._modules.by_name:
            # First-party package, submodule Polish cannot see. Refusing here
            # is the difference between "no evidence of a side effect" and
            # "evidence of no side effect", and only the second is a proof.
            return False
        return root in sys.stdlib_module_names and root not in SIDE_EFFECTING_STDLIB

    def _source_is_inert(self, relative: str) -> bool:
        if relative in self._proven:
            return self._proven[relative]
        if relative in self._examining:
            return True
        self._examining.add(relative)
        try:
            verdict = self._examine(relative)
        finally:
            self._examining.discard(relative)
        self._proven[relative] = verdict
        return verdict

    def _examine(self, relative: str) -> bool:
        try:
            source = (self._worktree / relative).read_text(encoding="utf-8")
            tree = _proof.parse(source)
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
            return False
        return all(
            self._statement_is_inert(statement, relative) for statement in tree.body
        )

    def _statement_is_inert(self, statement: ast.stmt, relative: str) -> bool:
        if isinstance(statement, ast.Expr):
            # A docstring, or any other bare literal. A bare call is not one.
            return isinstance(statement.value, ast.Constant)
        if isinstance(statement, ast.Pass):
            return True
        if isinstance(statement, ast.Import | ast.ImportFrom):
            return all(
                self.holds(dotted)
                for dotted in imported_modules(statement, relative, self._modules)
            )
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            # Decorators run at import time, and decorating is how registration
            # is usually spelled: `@app.route`, `@register`, `@pytest.fixture`.
            return not statement.decorator_list
        if isinstance(statement, ast.ClassDef):
            # Bases and `metaclass=` are evaluated when the class is created,
            # which is how ORMs and plugin registries collect their subclasses.
            return not (
                statement.decorator_list or statement.bases or statement.keywords
            )
        if isinstance(statement, ast.Assign | ast.AnnAssign):
            return _binds_a_literal(statement)
        if isinstance(statement, ast.If):
            # Neither branch runs on import, so its body needs no examination.
            return _never_runs_on_import(statement.test)
        return False


def imported_modules(
    statement: ast.Import | ast.ImportFrom, importer: str, modules: Modules
) -> Iterator[str | None]:
    """Every module executing this statement would run.

    `from demo import core` runs `demo` and, if `demo.core` is a module rather
    than an attribute of it, `demo.core` as well. Yielding None means the
    statement could not be resolved, which callers treat as unprovable.
    """
    if isinstance(statement, ast.Import):
        for alias in statement.names:
            yield alias.name
        return
    base = _modules.resolve(modules, importer, statement.module, statement.level)
    yield base
    if base is None:
        return
    for alias in statement.names:
        if alias.name == "*":
            continue
        submodule = f"{base}.{alias.name}"
        if submodule in modules.by_name:
            yield submodule


def _binds_a_literal(statement: ast.Assign | ast.AnnAssign) -> bool:
    """True for `X = 1`, false for `X = compute()` and for `obj.attr = 1`.

    Attribute and subscript targets are excluded because assigning to one runs
    someone else's `__setattr__` or `__setitem__`, which is a call like any
    other.
    """
    targets = (
        statement.targets if isinstance(statement, ast.Assign) else [statement.target]
    )
    if not all(isinstance(target, ast.Name) for target in targets):
        return False
    return statement.value is None or _is_literal(statement.value)


def _is_literal(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return all(_is_literal(element) for element in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            key is not None and _is_literal(key) and _is_literal(value)
            for key, value in zip(node.keys, node.values, strict=True)
        )
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
        return _is_literal(node.operand)
    return False


def _never_runs_on_import(test: ast.expr) -> bool:
    """`if __name__ == "__main__":` and `if TYPE_CHECKING:`, and nothing else.

    Both are false during an import — the first because an imported module's
    `__name__` is its dotted name, the second because `typing.TYPE_CHECKING` is
    a runtime False that only type checkers pretend otherwise about.
    """
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )
