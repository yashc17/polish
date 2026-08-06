"""Layer 1 — packaging metadata.

The most authoritative layer, because it is the one place the repo states in its
own words which directories hold its code. It contributes *declared roots*:
directories the walk must not drop on ignore-file evidence alone.

It deliberately does not try to enumerate files. Metadata describes what gets
shipped in a wheel, which is narrower than what the repo owns — `tests/`,
`noxfile.py` and `scripts/` are all first-party and none of them are declared
anywhere. Enumeration is the walk's job; this layer only supplies authority.
"""

from __future__ import annotations

import configparser
import tomllib
from pathlib import Path

PYPROJECT = "pyproject.toml"
SETUP_CFG = "setup.cfg"
SETUP_PY = "setup.py"

#: Files whose presence means "this directory is a Python project", even before
#: a single `.py` file has been found.
PROJECT_MARKERS = (PYPROJECT, SETUP_CFG, SETUP_PY)


def has_project_marker(root: Path) -> bool:
    return any((root / name).is_file() for name in PROJECT_MARKERS)


def declared_roots(root: Path) -> frozenset[str]:
    """Directories declared by packaging metadata, repo-root-relative posix."""
    found: set[str] = set()
    found |= _from_pyproject(root / PYPROJECT)
    found |= _from_setup_cfg(root / SETUP_CFG)
    return frozenset(path for path in found if path and (root / path).is_dir())


def _from_pyproject(path: Path) -> set[str]:
    data = _load_toml(path)
    if not data:
        return set()
    tools = data.get("tool", {})
    return (
        _setuptools_roots(tools.get("setuptools", {}))
        | _poetry_roots(tools.get("poetry", {}))
        | _hatch_roots(tools.get("hatch", {}))
        | _flit_roots(tools.get("flit", {}), path.parent)
    )


def _setuptools_roots(setuptools: dict) -> set[str]:
    roots: set[str] = set()
    package_dir = setuptools.get("package-dir")
    if isinstance(package_dir, dict):
        roots |= {str(value) for value in package_dir.values()}

    packages = setuptools.get("packages", {})
    if isinstance(packages, dict):
        where = packages.get("find", {}).get("where", [])
        roots |= {str(entry) for entry in _as_list(where)}
    else:
        # `packages = ["demo", "demo.sub"]` — dotted names are directories.
        roots |= {str(entry).replace(".", "/") for entry in _as_list(packages)}
    return roots


def _poetry_roots(poetry: dict) -> set[str]:
    roots: set[str] = set()
    for package in _as_list(poetry.get("packages", [])):
        if not isinstance(package, dict):
            continue
        include = str(package.get("include", ""))
        source = str(package.get("from", ""))
        roots.add(f"{source}/{include}" if source else include)
    return roots


def _hatch_roots(hatch: dict) -> set[str]:
    targets = hatch.get("build", {}).get("targets", {})
    roots: set[str] = set()
    for target in targets.values():
        if isinstance(target, dict):
            roots |= {str(entry) for entry in _as_list(target.get("packages", []))}
    return roots


def _flit_roots(flit: dict, root: Path) -> set[str]:
    name = flit.get("module", {}).get("name")
    if not name:
        return set()
    # flit accepts both a top-level module and a src-layout one.
    return {str(name), f"src/{name}"} if (root / "src").is_dir() else {str(name)}


def _from_setup_cfg(path: Path) -> set[str]:
    # `interpolation=None` because setup.cfg values are literal. The default
    # BasicInterpolation treats `%` as a directive and raises on any value
    # containing one — "100% typed" in a description would otherwise take the
    # whole file's declarations down with it.
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, UnicodeDecodeError, configparser.Error):
        return set()

    roots: set[str] = set()
    if parser.has_option("options", "package_dir"):
        for line in parser.get("options", "package_dir").splitlines():
            _, separator, value = line.partition("=")
            if separator and value.strip():
                roots.add(value.strip())
    if parser.has_option("options.packages.find", "where"):
        roots |= {
            line.strip()
            for line in parser.get("options.packages.find", "where").splitlines()
            if line.strip()
        }
    return roots


def _load_toml(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _as_list(value) -> list:
    return value if isinstance(value, list) else []
