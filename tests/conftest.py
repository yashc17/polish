"""Shared fixtures.

`make_repo` materialises a repo from a `{path: contents}` mapping. It is the
same shape the corpus manifests use, so a hand-written test case and a captured
real repo go through identical machinery. `make_git_repo` adds a git history to
one, which the safety envelope needs.
"""

from __future__ import annotations

import os
import subprocess
import venv
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

#: Polish shells out to git, which means git's *environment* is a test input.
#: Committing needs an identity, and CI runners have none configured; a
#: developer's global config, meanwhile, can carry a signing key or hooks that
#: would fail a commit for reasons that have nothing to do with Polish. Both
#: are settled here so the suite runs identically everywhere.
GIT_ENVIRONMENT = {
    "GIT_AUTHOR_NAME": "Polish Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Polish Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
}


@pytest.fixture(scope="session", autouse=True)
def git_environment():
    """Apply `GIT_ENVIRONMENT` to every git subprocess the suite causes.

    Session-scoped and applied to `os.environ` directly, because the code under
    test spawns git itself — `monkeypatch` would not reach a module-scoped
    fixture, and the envelope's own `git init` has to see the identity too.
    """
    previous = {name: os.environ.get(name) for name in GIT_ENVIRONMENT}
    os.environ.update(GIT_ENVIRONMENT)
    yield
    for name, value in previous.items():
        if value is None:
            del os.environ[name]
        else:
            os.environ[name] = value


def git(cwd: Path, *args: str) -> str:
    """Run git in `cwd` and return its stdout, raising on failure."""
    completed = subprocess.run(
        ("git", *args), cwd=cwd, capture_output=True, check=True, text=True
    )
    return completed.stdout


def materialise(root: Path, files: Mapping[str, str]) -> Path:
    """Write `files` under `root`. A path ending in `/` becomes an empty dir."""
    for relative, contents in files.items():
        target = root / relative
        if relative.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)
    return root


@pytest.fixture
def make_repo(tmp_path: Path) -> Callable[[Mapping[str, str]], Path]:
    counter = 0

    def _make(files: Mapping[str, str]) -> Path:
        nonlocal counter
        counter += 1
        root = tmp_path / f"repo{counter}"
        root.mkdir()
        return materialise(root, files)

    return _make


def commit_repo(root: Path) -> Path:
    """Turn a materialised tree into a git repository with one commit."""
    git(root, "init", "--quiet", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "--quiet", "-m", "initial")
    return root


@pytest.fixture
def make_git_repo(make_repo) -> Callable[[Mapping[str, str]], Path]:
    def _make(files: Mapping[str, str]) -> Path:
        return commit_repo(make_repo(files))

    return _make


@pytest.fixture(scope="session")
def sandbox_venv(tmp_path_factory) -> Path:
    """One real virtualenv, shared. Building one costs seconds; sharing is safe.

    `system_site_packages` is what keeps the suite offline: whatever runs in it
    finds pytest already importable and never reaches for the network. It is a
    test fixture rather than something the envelope does —
    `_sandbox._build_venv` deliberately builds an isolated one.

    Shared by the probe (#12) and the characterization harness (#13), which both
    execute repositories for real. Two session virtualenvs would be two builds
    and a second thing to keep offline.
    """
    path = tmp_path_factory.mktemp("sandbox") / "venv"
    venv.EnvBuilder(
        with_pip=True, symlinks=os.name != "nt", system_site_packages=True
    ).create(path)
    return path
