"""What a first-party file is importable as, and from which directory.

`polish.observer` needs this to turn `import requests` into an edge to
`src/requests/__init__.py`. `polish.probe` needs it to hand gate 3 a name the
interpreter will accept, plus a directory to find it in. Until #30 each had its
own answer and the two disagreed on the commonest packaging layout in Python: on
a `src/` root the observer indexed `src/requests/__init__.py` under the name
`src.requests`, so an absolute `import requests` resolved to nothing and the
repository's tests and its source sat in two disconnected halves of one graph.

Two rules, and the second is the one that was missing:

* **A file's name comes from the directory that reaches it**, never from its path
  relative to the repository. The import root is found by walking outwards while
  `__init__.py` keeps appearing — the interpreter's own rule — and packaging
  metadata may name roots that walk cannot see, because a namespace package
  leaves no `__init__.py` to walk over.
* **A file can have more than one legitimate name, and every one is indexed.**
  With the repository root on `sys.path`, `src/requests/__init__.py` is also
  importable as `src.requests`: PEP 420 makes `src/` an implicit namespace
  package. Both spellings occur in real repositories — a test run from the root
  writes one, the shipped package writes the other — so indexing one and
  dropping the other would trade the old blind spot for a new one.

The first name of each path is its **address**: the single name the interpreter
reaches on its own terms, and the only one the probe uses. Gate 3 is
demote-only, so offering it a second spelling of a file it already imported
could take a tier away and could never grant one.

A name is a claim about what `import x` means, and two files can make the same
claim — `scripts/build.py` and `tools/build.py` are both `build` from their own
directory. Addresses are claimed before alternates and ties break on sorted path
order, so the answer is deterministic; the loser is not lost, it keeps every
other name it has. Over-generating here is the safe direction: a name nothing
imports costs nothing, a spurious edge widens a scope, and a *missing* edge is
the bug this module exists to fix (ADR 0009).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from polish.firstparty import declared_roots

#: A directory is a package if it carries one of these. The stub counts: a
#: `py.typed` distribution can ship `__init__.pyi` beside no `__init__.py`, and
#: the directory is still a package to everything that reads types.
PACKAGE_FILES = ("__init__.py", "__init__.pyi")

SOURCE_SUFFIXES = (".py", ".pyi")

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class Module:
    """A first-party file addressed the way an interpreter would address it."""

    name: str
    root: Path
    path: str


@dataclass(frozen=True)
class Modules:
    """Every name every first-party file is importable by, and from where."""

    root: Path
    found: tuple[Module, ...]
    _by_name: dict[str, str] = field(init=False, repr=False)
    _addressed: tuple[Module, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_name", _claim(self.found))
        object.__setattr__(self, "_addressed", _addresses(self.found))

    @property
    def by_name(self) -> Mapping[str, str]:
        """Every importable name, to the first-party path it names.

        What the code graph resolves an import statement against, so it holds
        every spelling. `src.requests` and `requests` both land on
        `src/requests/__init__.py` when both are legitimate.
        """
        return self._by_name

    @property
    def addressed(self) -> tuple[Module, ...]:
        """One module per path: the name the interpreter reaches on its own.

        What anything that has to *execute* an import wants — one name per file,
        no second spelling of a module already imported.
        """
        return self._addressed

    def names(self, path: str) -> tuple[str, ...]:
        """Every name `path` is importable by, its address first."""
        return tuple(module.name for module in self.found if module.path == path)


def index(root: Path, files: Iterable[str]) -> Modules:
    """Name every file in `files`, relative to the roots that reach it.

    `files` is the first-party set: naming is only ever asked about the user's
    own code, because an edge to a dependency is not an edge the graph wants and
    a name for one is not a name the probe would import.
    """
    root = Path(root)
    declared = _declared_depths(root)
    found: list[Module] = []
    for relative in sorted(files):
        found.extend(_address(root, relative, declared))
    return Modules(root=root, found=tuple(found))


def roots(found: Sequence[Module]) -> tuple[Path, ...]:
    """The import roots reaching `found`, in first-seen order and deduplicated."""
    return tuple(dict.fromkeys(module.root for module in found))


# --- internals ------------------------------------------------------------


def _address(root: Path, relative: str, declared: frozenset[int]) -> list[Module]:
    """Every `(name, import root)` pair for one file, its address first.

    Ordered rather than a set, because the order *is* the precedence: the
    interpreter's own answer outranks the one packaging metadata implies, which
    outranks running from the repository root.
    """
    if not relative.endswith(SOURCE_SUFFIXES):
        return []
    parts = relative.split("/")
    directories, filename = parts[:-1], parts[-1]
    leaf = [] if filename in PACKAGE_FILES else [filename.rsplit(".", 1)[0]]

    depths = [_package_depth(root, directories)]
    depths += sorted(depth for depth in declared if depth <= len(directories))
    depths.append(0)

    found: list[Module] = []
    seen: set[int] = set()
    for depth in depths:
        if depth in seen:
            continue
        seen.add(depth)
        dotted = [*directories[depth:], *leaf]
        # A hyphenated script or a directory no `import` statement could name is
        # dropped for that root and kept for any other: `my-scripts/tool.py` is
        # unreachable from the repository root and perfectly importable from its
        # own directory.
        if not dotted or not all(IDENTIFIER.match(part) for part in dotted):
            continue
        found.append(
            Module(
                name=".".join(dotted),
                root=root.joinpath(*directories[:depth]),
                path=relative,
            )
        )
    return found


def _package_depth(root: Path, directories: Sequence[str]) -> int:
    """How many leading directories are *above* the file's package chain.

    The interpreter's rule, and the reason `src/demo/core.py` is `demo.core`
    rather than `src.demo.core`: the package chain is the run of directories
    above the file that each carry an `__init__.py`, and the first directory
    without one is the root the name is relative to.
    """
    depth = len(directories)
    for end in range(len(directories), 0, -1):
        if not _is_package(root.joinpath(*directories[:end])):
            break
        depth = end - 1
    return depth


def _declared_depths(root: Path) -> frozenset[int]:
    """Import-root depths implied by packaging metadata.

    A declared root is either a directory *holding* packages (`package-dir`,
    `packages.find.where`) or a package directory itself (poetry's `packages`,
    hatch, flit). The two mean different things about `sys.path` — `src` puts
    `src` on it, `src/demo` puts `src` on it — so the `__init__.py` decides which
    reading applies rather than the table it was written in.

    Depths rather than paths: what a root contributes is *where the name starts*,
    and every file below it starts in the same place.
    """
    depths: set[int] = set()
    for relative in declared_roots(root):
        parts = [part for part in relative.split("/") if part]
        if not parts:
            continue
        depths.add(len(parts) - 1 if _is_package(root.joinpath(*parts)) else len(parts))
    return frozenset(depth for depth in depths if depth >= 0)


def _is_package(directory: Path) -> bool:
    return any((directory / name).is_file() for name in PACKAGE_FILES)


def _claim(found: Sequence[Module]) -> dict[str, str]:
    """Resolve every name to one path, addresses before alternates.

    Two passes, because the precedence is across files rather than within one: a
    file's address must not be shadowed by another file's second spelling,
    however the sort order happened to fall.
    """
    addressed, alternates = _partition(found)
    claimed: dict[str, str] = {}
    for module in (*addressed, *alternates):
        _claim_one(claimed, module)
    return claimed


def _addresses(found: Sequence[Module]) -> tuple[Module, ...]:
    return _partition(found)[0]


def _partition(found: Sequence[Module]) -> tuple[tuple[Module, ...], ...]:
    """Split into one address per path, and every other name those paths have."""
    seen: set[str] = set()
    addressed: list[Module] = []
    alternates: list[Module] = []
    for module in found:
        (alternates if module.path in seen else addressed).append(module)
        seen.add(module.path)
    return tuple(addressed), tuple(alternates)


def _claim_one(claimed: dict[str, str], module: Module) -> None:
    """First claim wins, except that an implementation outranks its own stub.

    A `.py` and a `.pyi` for the same module are the same module, and the one
    whose imports actually run at runtime is the one the graph wants an edge to.
    """
    held = claimed.get(module.name)
    if held is None or (held.endswith(".pyi") and module.path.endswith(".py")):
        claimed[module.name] = module.path
