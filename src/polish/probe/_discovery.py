"""What the probe reads off a repository before it runs anything.

Everything here is static: parsing metadata, naming modules, noticing a test
suite. It executes nothing, which is what lets gate 1 honestly claim it runs no
repository code — the gates below only ever act on strings this module produced
and the allowlist already vetted.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

#: Optional-dependency and dependency-group names worth installing. A probe
#: wants the suite's own requirements, not `docs` or `lint`, and installing
#: every extra a repository declares is how gate 1 fails on repositories that
#: would otherwise have run.
TEST_EXTRAS = frozenset({"test", "tests", "testing", "dev", "develop", "development"})

#: Requirement files gate 1 installs, by stem. Deliberately a list rather than
#: `requirements*.txt`: the greedy glob was tried first and it swept up
#: `requirements-doc.txt` and `requirements-examples.txt` on a real repository,
#: whose mkdocs pins then conflicted and failed gate 1. That is a false Tier 0
#: caused by Polish rather than by the repository — safe, but wrong, and it
#: makes the verdict's stated reason a lie about which repository was at fault.
#: What gate 1 needs is enough installed for the suite to run, nothing more.
REQUIREMENT_STEMS = frozenset(
    {
        "requirements",
        "requirements-dev",
        "requirements_dev",
        "requirements-test",
        "requirements_test",
        "requirements-tests",
        "requirements-testing",
        "requirements-ci",
        # Inside a `requirements/` directory the prefix is the directory.
        "base",
        "main",
        "dev",
        "test",
        "tests",
        "testing",
        "ci",
    }
)

#: Directories whose contents are the suite rather than the code under test.
TEST_DIRECTORIES = frozenset({"tests", "test"})

#: Never imported by gate 3. `__main__` and `setup.py` *run* their repository on
#: import rather than merely defining it, and test modules were already executed
#: by gate 2 — importing them again outside pytest just invents failures.
UNIMPORTABLE = frozenset({"__main__.py", "setup.py", "conftest.py"})

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: A PEP 508 requirement begins with a name. Anything that begins with a dash,
#: a dot or a slash is an option or a local path, and gate 1 forwards neither.
REQUIREMENT = re.compile(r"^[A-Za-z0-9]")

CONFIG_FILENAME = "polish.toml"


@dataclass(frozen=True)
class Module:
    """A first-party file addressed the way an interpreter would address it."""

    name: str
    root: Path
    path: str


def requirements(root: Path) -> tuple[str, ...]:
    """Every dependency the repository declares, as bare requirement specifiers.

    Parsed here rather than handed to `pip -r` on purpose. A requirements file
    is repository-authored content that pip reads as *options* — `--index-url`
    would redirect the install, `-e .` would build the project — so the probe
    reads the files itself and forwards only the specifier lines. That is what
    lets the allowlist refuse any pip argument starting with a dash.
    """
    found: list[str] = []
    found += _from_pyproject(root / "pyproject.toml")
    found += _from_setup_cfg(root / "setup.cfg")
    for pattern in ("requirements*.txt", "requirements/*.txt"):
        for path in sorted(root.glob(pattern)):
            if path.stem.lower() in REQUIREMENT_STEMS:
                found += _from_requirements_file(path)
    return tuple(dict.fromkeys(item for item in found if REQUIREMENT.match(item)))


def test_files(first_party: Iterable[str]) -> tuple[str, ...]:
    """The files that make a repository look like it has a suite.

    Deliberately narrower than `is_test_module`: a lone `conftest.py` is not a
    test suite, and gate 2 must not report one as discovered when pytest will
    collect nothing from it.
    """
    return tuple(
        sorted(
            path
            for path in first_party
            if path.endswith(".py") and _looks_like_a_test(path.rsplit("/", 1)[-1])
        )
    )


def is_test_module(relative: str) -> bool:
    """Broad: anything the suite owns, for excluding from import smoke."""
    *directories, name = relative.split("/")
    return (
        name in UNIMPORTABLE
        or _looks_like_a_test(name)
        or any(directory in TEST_DIRECTORIES for directory in directories)
    )


def modules(root: Path, first_party: Iterable[str]) -> tuple[Module, ...]:
    """The first-party files gate 3 can import, with the roots that reach them.

    A file's import root is found by walking outwards while `__init__.py` keeps
    appearing, which is exactly how the interpreter would have resolved it. A
    file that cannot be addressed as a module — a hyphenated script, a stub — is
    dropped rather than guessed at: gate 3 demotes on failure, so a name the
    probe invented would pin a repository to Tier 0 for a reason that was never
    about the repository.
    """
    found: dict[str, Module] = {}
    for relative in sorted(first_party):
        if not relative.endswith(".py") or is_test_module(relative):
            continue
        module = _address(root, relative)
        if module is not None:
            found.setdefault(module.name, module)
    return tuple(found.values())


def import_roots(found: Sequence[Module]) -> tuple[Path, ...]:
    return tuple(dict.fromkeys(module.root for module in found))


def entrypoint(root: Path) -> str | None:
    """Gate 4's opt-in target, from `[probe] entrypoint` in `polish.toml`.

    Absent by design. SPEC §3.3 makes entry points the largest risk surface the
    probe has, so nothing here is inferred from the repository — a `__main__.py`
    or a `[project.scripts]` table is not consent.
    """
    section = _read_section(root / CONFIG_FILENAME, "probe")
    value = section.get("entrypoint")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _looks_like_a_test(name: str) -> bool:
    return name.endswith("_test.py") or (
        name.startswith("test_") and name.endswith(".py")
    )


def _address(root: Path, relative: str) -> Module | None:
    *directories, filename = relative.split("/")
    stem = filename.removesuffix(".py")

    depth = 0
    for end in range(len(directories), 0, -1):
        if not (root.joinpath(*directories[:end]) / "__init__.py").is_file():
            break
        depth += 1

    parts = directories[len(directories) - depth :]
    if stem != "__init__":
        parts.append(stem)
    if not parts or not all(IDENTIFIER.match(part) for part in parts):
        return None
    return Module(
        name=".".join(parts),
        root=root.joinpath(*directories[: len(directories) - depth]),
        path=relative,
    )


def _from_pyproject(path: Path) -> list[str]:
    data = _read_toml(path)
    project = data.get("project", {})
    found = list(_strings(project.get("dependencies")))

    for table, names in (
        (project.get("optional-dependencies"), TEST_EXTRAS),
        (data.get("dependency-groups"), TEST_EXTRAS),
    ):
        if isinstance(table, dict):
            for name, entries in table.items():
                if str(name).lower() in names:
                    found += _strings(entries)

    poetry = data.get("tool", {}).get("poetry", {})
    if isinstance(poetry, dict):
        found += _from_poetry(poetry.get("dependencies"))
        found += _from_poetry(poetry.get("dev-dependencies"))
    return found


def _from_poetry(table) -> list[str]:
    """Poetry names its dependencies with carets, which are not PEP 508.

    The version constraint is dropped rather than translated: the probe wants to
    know whether the repository's dependencies can be installed at all, and a
    mistranslated caret would fail gate 1 for a reason belonging to the
    translation rather than to the repository.
    """
    if not isinstance(table, dict):
        return []
    return [str(name) for name in table if str(name).lower() != "python"]


def _from_setup_cfg(path: Path) -> list[str]:
    """`[options] install_requires`, which is an indented block, not a list.

    Hand-parsed rather than via `configparser` so that a malformed section
    somewhere else in the file cannot cost the probe every dependency in it.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: list[str] = []
    inside = collecting = False
    for raw in text.splitlines():
        stripped = raw.split("#", 1)[0].strip()
        if raw.lstrip().startswith("["):
            inside, collecting = raw.strip() == "[options]", False
        elif not inside or not stripped:
            continue
        elif stripped.startswith("install_requires"):
            found += _strings(stripped.split("=", 1)[-1].splitlines())
            collecting = True
        elif collecting and raw[:1].isspace():
            found += _strings([stripped])
        else:
            collecting = False
    return found


def _from_requirements_file(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return _strings(line.split("#", 1)[0] for line in text.splitlines())


def _strings(entries) -> list[str]:
    if isinstance(entries, str) or not isinstance(entries, Iterable):
        return []
    return [
        str(entry).strip()
        for entry in entries
        if isinstance(entry, str) and str(entry).strip()
    ]


def _read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_section(path: Path, name: str) -> dict:
    section = _read_toml(path).get(name, {})
    return section if isinstance(section, dict) else {}
