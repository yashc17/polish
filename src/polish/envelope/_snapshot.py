"""The raw snapshot — the unedited copy taken before any night-shift work.

SPEC §4 criterion 2 requires the snapshot to byte-match the pre-run tree. What
it copies is the first-party file set and nothing else: those are the only
files Polish will ever edit, so they are the only files a recovery could need,
and bounding the copy that way is what stops a repo with a 2 GB dataset beside
it from costing 2 GB every night.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from pathlib import Path


def write(root: Path, files: Iterable[str], destination: Path) -> Path:
    """Copy `files` — root-relative posix paths — into `destination`.

    `copy2` rather than `copy`, so mode and timestamps survive too: a restored
    snapshot that silently drops the executable bit on a script is not the
    tree Polish was given.
    """
    destination.mkdir(parents=True, exist_ok=True)
    for relative in files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, target)
    return destination
