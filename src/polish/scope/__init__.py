"""What a run looks at, and why — the answer to SPEC §3.6.

Two public entry points, one per end of a run:

    select(root, files, workspace=...)   what this run should refactor
    record(workspace, scope, run_id=..)  the watermark the next run scopes from

    - **First run:** whole-repo deep pass.
    - **Every run after:** files changed since the last run, **plus their
      dependency closure.**

The second half is the whole point. Scoping to the changed files alone would
make every run after the first quietly less safe than the first — §3.6's own
example is a caller deleted today stranding a function three files away, which
no diff of changed files will ever surface. So the changed files are a *seed*,
not the scope, and the code graph the observer kept all day is what turns one
into the other.

Three things can make a run refuse to be incremental, and all three widen the
scope rather than narrow it:

* there is no watermark, so there is no previous run to be relative to;
* the observer's store could not be believed, so the graph was cold-parsed and
  carries no intra-day history — and a file changed and changed back since the
  last run would then read as untouched;
* `--full`, because someone asked.

That direction is the whole safety argument. Over-scoping costs a longer night
and degrades into the full sweep Polish shipped with. Under-scoping ships an
edit nobody looked at the consequences of, which is the failure SPEC §1.1 says
is not allowed to happen. Every uncertainty here resolves towards looking at
more, on the same terms the tier probe resolves downward.
"""

from __future__ import annotations

import enum
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from polish import observer
from polish.observer import Observation

from . import _closure, _ledger
from ._ledger import LEDGER

__all__ = ["LEDGER", "Kind", "Scope", "record", "select"]

#: How many paths the run log lists before summarising the rest. A closure over
#: a hub module can reach hundreds of files, and a log nobody reads to the end
#: states nothing.
LISTED = 20


class Kind(enum.Enum):
    """Which of SPEC §3.6's two passes this run is."""

    SWEEP = "full sweep"
    INCREMENTAL = "incremental"


@dataclass(frozen=True)
class Scope:
    """The files a run will refactor, and the provenance of every one of them.

    The provenance tuples are not diagnostics. SPEC §3.6 makes the scope a
    safety decision, and issue #20 requires the run log to state what was in
    scope *and why* — so "this file is here because something that imports it
    changed" has to survive to the log, not be reconstructable from it.
    """

    #: What this run refactors. Always a subset of the first-party set.
    files: tuple[str, ...]
    kind: Kind
    #: One line, for the run log and for a human at 9am.
    reason: str
    #: Seeds: files whose bytes differ from the watermark, or that are new.
    changed: tuple[str, ...] = ()
    #: Seeds: files unchanged on disk whose history has a hole in it. Absent
    #: history is not evidence of quiet — the same rule `Churn.count` enforces
    #: (ADR 0003) — so these are seeded rather than assumed untouched.
    unwitnessed: tuple[str, ...] = ()
    #: Seeds: paths the watermark knows and the tree no longer has. They cannot
    #: be refactored, but their former imports are exactly what strands code.
    deleted: tuple[str, ...] = ()
    #: Files pulled in by the closure, not by having changed.
    pulled_in: tuple[str, ...] = ()
    #: The tree as this run found it, ready to become the next watermark.
    _entries: Mapping[str, _ledger.Entry] = field(repr=False, default_factory=dict)

    @property
    def swept(self) -> bool:
        return self.kind is Kind.SWEEP

    def summary(self) -> tuple[str, ...]:
        """The scope, in the same register as the rest of the run log.

        Markers match the four provenance tuples above, in order:
        `~` changed · `?` unwitnessed · `-` deleted · `+` pulled in by the
        closure. A sweep lists nothing, because a sweep is every file and the
        count is already on the line above.
        """
        lines = [
            _line("scope", f"{self.kind.value}: {_count(len(self.files))} in scope"),
            _line("why", self.reason),
        ]
        if self.swept:
            return tuple(lines)
        listed = [
            *((f"  ~ {path}") for path in self.changed),
            *((f"  ? {path}") for path in self.unwitnessed),
            *((f"  - {path}") for path in self.deleted),
            *((f"  + {path}") for path in self.pulled_in),
        ]
        lines += listed[:LISTED]
        if len(listed) > LISTED:
            lines.append(f"  … and {len(listed) - LISTED} more")
        return tuple(lines)


def select(
    root: Path,
    files: Sequence[str],
    *,
    workspace: Path,
    observation: Observation | None = None,
    full: bool = False,
) -> Scope:
    """Decide what this run looks at, given the first-party set `files`.

    `workspace` is `.polish/`, where the watermark lives. It is a parameter
    rather than a constant so that scoping stays a function of what it is told,
    and so a test can point two runs at the same repository from different
    workspaces without either seeing the other's watermark.

    `observation` replaces the observer read. It is public for the same reason
    the probe's `runner` is: the graph is the input that decides how far the
    closure reaches, and a seam only tests can reach is a seam nobody can
    inspect.
    """
    root = Path(root)
    files = tuple(files)
    if observation is None:
        observation = observer.read(root)
    entries = _fingerprint(root, files, observation)

    if full:
        return Scope(files, Kind.SWEEP, _ASKED, _entries=entries)

    previous = _ledger.read(workspace)
    if previous is None:
        return Scope(files, Kind.SWEEP, _FIRST.format(ledger=LEDGER), _entries=entries)

    # A cold parse produces a correct graph and no history at all. The graph is
    # enough to walk a closure; the history is what says whether an unchanged
    # file was actually quiet, and §3.7 keeps the daemon for exactly that. With
    # it missing, "the digest matches the watermark" cannot be read as "nothing
    # happened here", so nothing is trusted to be out of scope.
    if observation.cold_parsed:
        return Scope(
            files,
            Kind.SWEEP,
            _COLD.format(trust=observation.trust.value),
            _entries=entries,
        )

    present = set(files)
    changed = tuple(
        path
        for path in files
        if path not in previous.entries
        or previous.entries[path].digest != entries[path].digest
    )
    already = set(changed)
    unwitnessed = tuple(
        path
        for path in observation.unknown_churn()
        if path in present and path not in already
    )
    deleted = tuple(sorted(path for path in previous.entries if path not in present))

    seeds = (*changed, *unwitnessed, *deleted)
    if not seeds:
        return Scope(
            (), Kind.INCREMENTAL, _QUIET.format(run=previous.run_id), _entries=entries
        )

    edges = _closure.merge(
        {path: entry.imports for path, entry in previous.entries.items()},
        {path: entry.imports for path, entry in entries.items()},
    )
    reached = _closure.closure(seeds, edges) & present
    pulled_in = tuple(sorted(reached - set(seeds)))

    return Scope(
        files=tuple(sorted(reached)),
        kind=Kind.INCREMENTAL,
        reason=_why(previous.run_id, changed, unwitnessed, deleted, pulled_in),
        changed=changed,
        unwitnessed=unwitnessed,
        deleted=deleted,
        pulled_in=pulled_in,
        _entries=entries,
    )


def record(workspace: Path, scope: Scope, *, run_id: str) -> None:
    """Write the watermark the next run will scope from.

    Called at the end of a run, never at the start: a run that died halfway
    through has not looked at what it claimed, and the next one should sweep
    rather than believe it. Advancing the watermark is the last thing that
    happens, and only on the path where the work actually happened.

    What is recorded is what `select()` saw, not a re-read of the tree. Those
    are the same bytes — SPEC §4.9 leaves the working tree untouched — unless
    the developer edited during the run, in which case recording the fresher
    read would silently drop their edit out of tomorrow's scope.
    """
    _ledger.write(workspace, run_id, scope._entries)


# --- internals ------------------------------------------------------------


def _fingerprint(
    root: Path, files: tuple[str, ...], observation: Observation
) -> dict[str, _ledger.Entry]:
    """Digest and import edges for every first-party file, as it is right now.

    An unreadable file gets an empty digest rather than being dropped, so it
    compares unequal to any real one and stays in scope. Same reason
    `observer._graph.parse` records one: a file that exists and cannot be read
    is a fact, and forgetting it is how it silently leaves the scope.
    """
    entries: dict[str, _ledger.Entry] = {}
    for relative in files:
        try:
            digest = hashlib.sha256((root / relative).read_bytes()).hexdigest()
        except OSError:
            digest = ""
        entries[relative] = _ledger.Entry(digest, observation.dependencies(relative))
    return entries


def _why(
    run_id: str,
    changed: tuple[str, ...],
    unwitnessed: tuple[str, ...],
    deleted: tuple[str, ...],
    pulled_in: tuple[str, ...],
) -> str:
    since = [f"{len(changed)} changed"]
    if unwitnessed:
        since.append(f"{len(unwitnessed)} unwitnessed")
    if deleted:
        since.append(f"{len(deleted)} deleted")
    return (
        f"{_join(since)} since run {run_id}; "
        f"{len(pulled_in)} pulled in by the dependency closure"
    )


def _join(parts: list[str]) -> str:
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


def _count(total: int) -> str:
    return f"{total} first-party file" + ("" if total == 1 else "s")


def _line(label: str, value: str) -> str:
    return f"{label:<10}{value}"


_ASKED = "--full: a whole-repo sweep was asked for"

_FIRST = (
    "first run: no record of a previous run at .polish/{ledger}, so there is "
    "nothing to be incremental against"
)

_COLD = (
    "the observer store was {trust}, so the graph was cold-parsed: with no "
    "intra-day history, a file changed and changed back since the last run "
    "would read as untouched"
)

_QUIET = "nothing has changed since run {run}"
