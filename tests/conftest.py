"""Shared fixtures.

`make_repo` materialises a repo from a `{path: contents}` mapping. It is the
same shape the corpus manifests use, so a hand-written test case and a captured
real repo go through identical machinery.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

import pytest


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
