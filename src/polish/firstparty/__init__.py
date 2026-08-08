"""The first-party file set — the files Polish is willing to refactor.

`scan()` answers which files are the user's own. `declared_roots()` answers the
one question about them that packaging metadata, and nothing else, can settle:
which directories the repository says its code lives under. Everything else is
private.

The second is public because module naming needs it (`polish.modules`) and a
namespace package leaves no `__init__.py` for a walk to find, so the metadata is
the only evidence there is. It stays here rather than moving: this is the layer
that reads packaging metadata, and a second reader would be a second answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import _config, _markers, _metadata, _walk

SOURCE_SUFFIXES = frozenset({".py", ".pyi"})


class NotAPythonProject(Exception):
    """Raised when `root` holds nothing Polish could refactor.

    Carries an actionable message: every case that reaches here has a next step
    the user can take, and saying which one is the whole point of failing loudly
    instead of returning an empty set.
    """


@dataclass(frozen=True)
class FirstPartySet:
    """The files Polish considers first-party source, relative to `root`.

    Paths are POSIX-style and sorted. Membership is O(1) and accepts either a
    string or a `Path`, because the callers downstream — scope selection, the
    tier probe, the code graph — ask "is this file mine?" once per file per run
    over sets this size.
    """

    root: Path
    files: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "_index", frozenset(self.files))

    def __iter__(self):
        return iter(self.files)

    def __len__(self) -> int:
        return len(self.files)

    def __contains__(self, path: object) -> bool:
        if isinstance(path, Path):
            path = path.as_posix()
        return str(path) in self._index


def declared_roots(root: Path) -> frozenset[str]:
    """Directories packaging metadata names as holding the repo's code.

    Repo-root-relative posix paths, each one an existing directory. Both
    readings appear in the wild — a directory *holding* packages
    (`package-dir`, `packages.find.where`) and a package directory itself
    (poetry, hatch, flit) — and this layer reports what was declared without
    deciding which is which, because the two are told apart by the tree rather
    than by the table the entry was written in.
    """
    return _metadata.declared_roots(Path(root).resolve())


def scan(root: Path) -> FirstPartySet:
    """Resolve the first-party file set for the repo at `root`."""
    root = Path(root)
    if not root.is_dir():
        raise NotAPythonProject(f"{root} does not exist, or is not a directory.")
    root = root.resolve()

    declared = _metadata.declared_roots(root)
    overrides = _config.load(root)

    inferred = set(_infer(root, declared))
    resurrected = set(_resurrect(root, overrides))
    files = overrides.apply(inferred, resurrected)

    if not files:
        _explain_empty_result(root)
    return FirstPartySet(root=root, files=tuple(sorted(files)))


def _explain_empty_result(root: Path) -> None:
    """Raise unless an empty set is genuinely what the repo looks like.

    A project that simply has no source yet is a legitimate empty answer. A repo
    full of Python where every file was thrown away is not — that is the
    virtualenv case going wrong, and it has to be loud.
    """
    candidates = sum(
        1
        for path in root.rglob("*")
        if path.suffix in SOURCE_SUFFIXES
        and path.is_file()
        and ".git" not in path.relative_to(root).parts
    )
    if candidates:
        raise NotAPythonProject(
            f"{root} contains {candidates} Python files, but Polish classified "
            "every one of them as virtualenv, vendored, generated, or build "
            "output — so there is nothing it is willing to refactor.\n"
            "If that is wrong, name the real source in polish.toml:\n"
            '\n    [scan]\n    include = ["your_package/**"]\n'
        )
    if not _metadata.has_project_marker(root):
        raise NotAPythonProject(
            f"{root} is not a Python project — it has no pyproject.toml, "
            "setup.py or setup.cfg, and no Python source anywhere beneath it.\n"
            "Run polish from the root of a Python repository."
        )


def _resurrect(root: Path, overrides: _config.Overrides):
    """Yield source files named by `include`, wherever they are in the tree.

    An include has to be able to reach into a subtree the walk pruned, so this
    is a second traversal that ignores every marker. It only runs when the repo
    actually has include patterns, which keeps the cost off the common path.
    """
    if not overrides.has_include:
        return
    for path in root.rglob("*"):
        if path.suffix not in SOURCE_SUFFIXES or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative.startswith(".git/"):
            continue
        if overrides.includes(relative):
            yield relative


def _infer(root: Path, declared: frozenset[str]):
    """Yield source paths, pruning whole subtrees that markers or ignores rule out.

    Pruning rather than filtering is what makes the venv case cheap: a repo
    where 99.6% of `.py` files are virtualenv costs one `pyvenv.cfg` stat, not
    thirty thousand path decisions.
    """
    stack = [(root, "", _walk.root_rules(root))]
    while stack:
        directory, base, rules = stack.pop()
        for entry in sorted(directory.iterdir()):
            relative = f"{base}/{entry.name}" if base else entry.name
            if entry.is_dir():
                if _markers.is_excluded_dir(entry, at_root=not base):
                    continue
                # Git cannot re-include anything beneath an excluded directory,
                # so an ignored directory ends the walk for its whole subtree.
                if rules.ignores(f"{relative}/") and not _declared(relative, declared):
                    continue
                stack.append((entry, relative, rules.extend(entry, relative)))
            elif (
                entry.suffix in SOURCE_SUFFIXES
                and (not rules.ignores(relative) or _declared(relative, declared))
                and not _markers.is_generated_file(entry)
            ):
                yield relative


def _declared(relative: str, declared: frozenset[str]) -> bool:
    """True if `relative` lies on the path to, or inside, a declared root.

    Both directions matter: `lib` must survive so the walk can reach
    `lib/demo/core.py`, and `lib/demo/core.py` must survive because `lib` was
    declared. Only the ignore layer is overridden — markers still prune first.
    """
    return any(
        root == relative
        or root.startswith(f"{relative}/")
        or relative.startswith(f"{root}/")
        for root in declared
    )
