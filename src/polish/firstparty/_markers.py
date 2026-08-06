"""Layer 3 — environment, vendor and build markers.

These identify directories whose contents are, by construction, not the user's
code. This layer overrides packaging metadata: a declared package that turns out
to live inside a virtualenv is nonsense, and the marker is the stronger evidence.

The bar for adding a name here is that it is *unambiguous* — that no reasonable
person would name a real package that. Precision matters more than recall in
this module (refactoring vendored code is far worse than skipping a source
file), but a name matched too eagerly costs recall on every repo that uses it
innocently, so names that are merely common are handled by anchoring instead.
"""

from __future__ import annotations

from itertools import islice
from pathlib import Path

#: Names that are decisive wherever they appear in the tree.
EXCLUDED_ANYWHERE = frozenset(
    {
        # Installed dependencies
        "site-packages",
        "dist-packages",
        "node_modules",
        "__pypackages__",
        # Task-runner environments
        ".tox",
        ".nox",
        ".eggs",
        # Vendored code — checked in, git-tracked, still not the user's
        "_vendor",
        "vendored",
        "third_party",
        "thirdparty",
        # Caches and tool state
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".ipynb_checkpoints",
        # Version control
        ".git",
        ".hg",
        ".svn",
    }
)

#: Filename suffixes produced by code generators.
GENERATED_SUFFIXES = ("_pb2.py", "_pb2_grpc.py", "_pb2.pyi", ".g.py")

#: The conventional machine-written banner. `@generated` is the cross-ecosystem
#: standard; `DO NOT EDIT` is near-universal alongside it.
GENERATED_BANNERS = ("@generated", "DO NOT EDIT")

#: How far into a file a banner still counts as a banner. Past the header, the
#: same words are prose — a docstring warning readers off, not a generator.
BANNER_SCAN_LINES = 5

#: Names that are only decisive directly under the repo root. `build`, `dist`
#: and `docs/_build` are conventional artifact directories there, but all are
#: plausible names for a real subpackage deeper in a tree.
EXCLUDED_AT_ROOT = frozenset({"build", "dist", "_build", "htmlcov", "site"})


def is_environment_dir(path: Path) -> bool:
    """True if `path` is the root of a Python environment.

    Identified by marker file rather than by name: every virtualenv since 3.3
    carries `pyvenv.cfg`, and people name them `.venv`, `venv`, `env`, `myenv`
    and worse. Naming a directory `venv` is not evidence; the marker is.
    Conda environments carry `conda-meta/` instead.
    """
    return (path / "pyvenv.cfg").is_file() or (path / "conda-meta").is_dir()


def is_excluded_dir(path: Path, *, at_root: bool) -> bool:
    """True if `path` is a directory whose whole subtree is not first-party."""
    name = path.name
    if name in EXCLUDED_ANYWHERE:
        return True
    if at_root and name in EXCLUDED_AT_ROOT:
        return True
    if name.endswith(".egg-info"):
        return True
    return is_environment_dir(path)


def is_generated_file(path: Path) -> bool:
    """True if `path` looks machine-written.

    Generated code is not the user's to refactor, and editing it is worse than
    useless: the next generator run silently reverts the change.
    """
    if path.name.endswith(GENERATED_SUFFIXES):
        return True
    return _has_generated_banner(path)


def _has_generated_banner(path: Path) -> bool:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            header = list(islice(handle, BANNER_SCAN_LINES))
    except OSError:
        return False
    return any(
        banner in line
        for line in header
        if line.lstrip().startswith("#")
        for banner in GENERATED_BANNERS
    )
