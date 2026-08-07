"""The watermark: what the tree looked like when the previous run saw it.

SPEC §3.6 scopes every run after the first to "files changed since the last
run", which requires a record of what the last run found. That record is this
file, at `.polish/last-run.json`.

It holds a digest **and the import edges** for every first-party file. The
digests are the obvious half; the edges are the half that makes the motivating
case work.

    §3.6: "deleting a caller today can strand a function three files away, and
    `git diff` will never surface that."

The edge that stranded the function is the edge that was *deleted*. A closure
walked over today's graph alone cannot see it — the import is gone, so the
graph no longer connects the caller to the code it orphaned. Keeping yesterday's
edges here means the closure can be walked over the union of the two graphs,
which is the only place that edge still exists. The same record is what lets a
file that has since been deleted still pull its former neighbours into scope.

Plain JSON rather than a second SQLite store: this is small, written once per
run, read once per run, and — like `meta.json` next to it — answers "why did
Polish only look at three files last night?" with `cat`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

#: Bumped whenever the shape below changes. There is no migration path, for the
#: same reason the observer's store has none (ADR 0003): an unreadable watermark
#: costs one full sweep, and a full sweep is the behaviour Polish shipped with.
VERSION = 1

LEDGER = "last-run.json"


@dataclass(frozen=True)
class Entry:
    """One file, as the previous run found it."""

    digest: str
    imports: frozenset[str]


@dataclass(frozen=True)
class Ledger:
    """The previous run's view of the whole first-party set."""

    run_id: str
    entries: Mapping[str, Entry]


def path_in(workspace: Path) -> Path:
    return workspace / LEDGER


def read(workspace: Path) -> Ledger | None:
    """The previous run's watermark, or None if there is not a usable one.

    Never raises. Every failure — absent, truncated, hand-edited, written by a
    future version — resolves to None, and None means the run sweeps. That is
    the same downward resolution the probe uses: the safe answer to "I cannot
    tell what changed" is "look at everything", so no corrupt file on disk can
    talk a run into a narrower scope than it has earned.
    """
    try:
        raw = json.loads(path_in(workspace).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != VERSION:
        return None
    files = raw.get("files")
    if not isinstance(files, dict):
        return None
    entries: dict[str, Entry] = {}
    for path, record in files.items():
        if not isinstance(record, dict):
            return None
        digest = record.get("digest")
        imports = record.get("imports")
        if not isinstance(digest, str) or not isinstance(imports, list):
            return None
        entries[path] = Entry(digest, frozenset(str(target) for target in imports))
    return Ledger(run_id=str(raw.get("run_id", "")), entries=entries)


def write(workspace: Path, run_id: str, entries: Mapping[str, Entry]) -> None:
    """Replace the watermark with what this run saw, atomically.

    Written through a temporary file and `os.replace` because the alternative to
    a whole watermark is not a partial one — a half-written file reads as no
    watermark at all, and costs the next run a full sweep it did not need.
    """
    payload = {
        "version": VERSION,
        "run_id": run_id,
        "files": {
            path: {"digest": entry.digest, "imports": sorted(entry.imports)}
            for path, entry in sorted(entries.items())
        },
    }
    workspace.mkdir(parents=True, exist_ok=True)
    path = path_in(workspace)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
