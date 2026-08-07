"""The map between first-party files and the module names imports use.

Tier 0 has to answer "what does `from .core import go` actually import?"
without executing anything, which means reconstructing the import system's
naming from the tree alone. The rule here is the one every static tool
converges on: a file's package chain is the run of directories above it that
each carry an `__init__.py`, and the first directory without one is the root
the module name is relative to. That is what makes `src/demo/core.py` the
module `demo.core` rather than `src.demo.core`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

PACKAGE_FILES = ("__init__.py", "__init__.pyi")


@dataclass(frozen=True)
class Modules:
    """Every first-party module, addressable from either side."""

    by_name: dict[str, str]
    by_path: dict[str, str]


def index(worktree: Path, files: Sequence[str]) -> Modules:
    by_name: dict[str, str] = {}
    by_path: dict[str, str] = {}
    for relative in files:
        dotted = _dotted(worktree, relative)
        if not dotted:
            continue
        by_path[relative] = dotted
        # A `.py` and a `.pyi` for the same module both map to the same name.
        # The implementation wins: it is the file whose imports actually run.
        if dotted not in by_name or relative.endswith(".py"):
            by_name[dotted] = relative
    return Modules(by_name=by_name, by_path=by_path)


def resolve(
    modules: Modules, importer: str, module: str | None, level: int
) -> str | None:
    """The absolute dotted name an import statement names, or None.

    None means Polish could not work out what is being imported — a relative
    import that climbs past the root, or one made from a file whose own module
    name is unknown. Tier 0 treats that as unprovable rather than guessing:
    the whole removal rule rests on knowing what runs when the import runs.
    """
    if level == 0:
        return module
    dotted = modules.by_path.get(importer)
    if dotted is None:
        return None
    package = dotted.rpartition(".")[0] if not _is_package(dotted, modules) else dotted
    for _ in range(level - 1):
        package = package.rpartition(".")[0]
    if not package:
        return None
    return f"{package}.{module}" if module else package


def _is_package(dotted: str, modules: Modules) -> bool:
    relative = modules.by_name.get(dotted, "")
    return Path(relative).name in PACKAGE_FILES


def _dotted(worktree: Path, relative: str) -> str:
    parts = relative.split("/")
    directories, filename = parts[:-1], parts[-1]
    leaf = [] if filename in PACKAGE_FILES else [filename.rsplit(".", 1)[0]]

    package: list[str] = []
    for depth in range(len(directories), 0, -1):
        directory = worktree.joinpath(*directories[:depth])
        if not any((directory / name).exists() for name in PACKAGE_FILES):
            break
        package.insert(0, directories[depth - 1])
    return ".".join(package + leaf)
