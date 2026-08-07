"""The observer daemon — the code graph and the intra-day change history.

Two public entry points, one per side of the day:

    observe(root)   the daemon body: watch, and write what changed
    read(root)      what a night run calls: the graph, and how far to trust it

SPEC §3.7 keeps this component for history rather than speed. A cold parse is
fast; what the daemon captures is what `git diff` erases — code written at 2pm
and deleted at 5pm, the order changes happened in, which functions churned
repeatedly.

That produces the asymmetry the whole module is shaped around:

* **The graph is verifiable.** Every file carries a fingerprint, revalidated
  against the tree at read time. Drift is repaired by re-parsing, because the
  tree is the source of truth. A stale graph is always recoverable.
* **The history is not.** Nothing can reconstruct what happened while nobody was
  watching. So the daemon records its uptime windows, and a gap makes churn
  **UNKNOWN — never "no churn"**. SPEC §3.7 gates dedup on churn; absent data
  reading as quiet would produce a confident dedup of a function that was
  rewritten four times, which is an unsafe edit caused by missing data rather
  than by wrong data. `Churn.count` raises rather than let that happen.

See ADR 0002 for the storage decision.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from polish.firstparty import NotAPythonProject, scan

from . import _graph, _process, _status, _store
from ._graph import Symbol
from ._store import MAX_BYTES, RETENTION_DAYS, STORE_DIR, Gap, Trust

__all__ = [
    "MAX_BYTES",
    "RETENTION_DAYS",
    "STORE_DIR",
    "Churn",
    "ChurnUnknown",
    "Gap",
    "Observation",
    "ObserverFailedToStart",
    "Symbol",
    "Trust",
    "compact",
    "describe",
    "observe",
    "read",
    "start",
    "status_line",
    "stop",
]

ObserverFailedToStart = _process.ObserverFailedToStart
stop = _process.stop

#: Poll interval. The daemon polls rather than subscribing to filesystem events
#: on purpose: inotify watch limits, editors that save by atomic rename, and
#: network filesystems all drop events silently, and a watcher that believes it
#: saw everything is exactly the failure ADR 0002 refuses to trust.
INTERVAL_SECONDS = 2.0

#: Cycles between retention sweeps. Bounds do not need per-poll precision, and
#: the sweep takes a write lock the poll would rather not queue behind.
SWEEP_EVERY = 150


class ChurnUnknown(Exception):
    """Raised when churn is read for code whose history has a hole in it.

    Deliberately an exception and not a zero. Every caller that gates a
    transform on churn has to decide what to do with absent data, and the only
    safe default is "do not run the transform" — so the type makes silence
    impossible to mistake for evidence.
    """


@dataclass(frozen=True)
class Churn:
    """How many revisions the daemon watched a file or symbol go through.

    `count` is a **lower bound**: two edits inside one poll interval land as
    one. That is inherent to watching a filesystem rather than an editor, and it
    biases towards under-reporting, so a churn gate should be written as
    "≥ N revisions means ineligible" rather than "< N means safe".
    """

    path: str
    qualname: str | None
    _revisions: int | None

    @classmethod
    def watched(cls, path: str, qualname: str | None, revisions: int) -> Churn:
        return cls(path, qualname, revisions)

    @classmethod
    def unknown(cls, path: str, qualname: str | None) -> Churn:
        return cls(path, qualname, None)

    @property
    def known(self) -> bool:
        return self._revisions is not None

    @property
    def count(self) -> int:
        if self._revisions is None:
            raise ChurnUnknown(
                f"no history for {self.qualname or self.path}: the observer was "
                "not watching when it changed. Treat this as unknown, not as "
                "unchanged — churn-gated transforms must skip it."
            )
        return self._revisions


@dataclass(frozen=True)
class Observation:
    """What a night run gets: the graph, plus how far it may be believed."""

    root: Path
    trust: Trust
    files: tuple[str, ...]
    stale: tuple[str, ...]
    gaps: tuple[Gap, ...]
    _imports: dict[str, frozenset[str]] = field(repr=False, default_factory=dict)
    _symbols: dict[str, tuple[Symbol, ...]] = field(repr=False, default_factory=dict)
    _churn: dict[tuple[str, str | None], int] = field(repr=False, default_factory=dict)
    _unknown: frozenset[str] = field(repr=False, default=frozenset())

    @property
    def cold_parsed(self) -> bool:
        """True when the store could not be believed and the tree was re-read."""
        return self.trust is not Trust.USABLE

    def symbols(self, path: str) -> tuple[Symbol, ...]:
        return self._symbols.get(path, ())

    def dependencies(self, path: str) -> frozenset[str]:
        """The first-party files `path` imports, directly."""
        return self._imports.get(path, frozenset())

    def dependents(self, *paths: str) -> frozenset[str]:
        """`paths` plus everything that transitively imports them.

        This is the dependency closure SPEC §3.6 scopes an incremental run by:
        "deleting a caller today can strand a function three files away, and
        `git diff` will never surface that".
        """
        reverse: dict[str, set[str]] = {}
        for importer, targets in self._imports.items():
            for target in targets:
                reverse.setdefault(target, set()).add(importer)
        closure: set[str] = set()
        pending = list(paths)
        while pending:
            path = pending.pop()
            if path in closure:
                continue
            closure.add(path)
            pending.extend(reverse.get(path, ()))
        return frozenset(closure)

    def churn(self, path: str, qualname: str | None = None) -> Churn:
        """Revisions watched for a file, or for one symbol inside it."""
        if self.cold_parsed or path in self._unknown:
            return Churn.unknown(path, qualname)
        revisions = self._churn.get((path, qualname))
        if revisions is None:
            return Churn.unknown(path, qualname)
        return Churn.watched(path, qualname, revisions)

    def unknown_churn(self) -> tuple[str, ...]:
        """Every first-party file whose history has a hole in it."""
        if self.cold_parsed:
            return self.files
        return tuple(sorted(path for path in self.files if path in self._unknown))


def observe(
    root: Path,
    *,
    interval: float = INTERVAL_SECONDS,
    until: threading.Event | None = None,
) -> None:
    """Watch `root` until `until` is set, or until the process is signalled.

    Blocking, and the whole of the daemon. `polish observe start` is a thin
    wrapper that runs this in a detached child — running it in a terminal is a
    supported way to use Polish, and the way to debug it.
    """
    root = Path(root).resolve()
    # Resolve once up front, so a repo Polish would decline fails here with the
    # message that names the fix. The guard inside the sweep is for a tree that
    # goes momentarily unreadable mid-checkout, and would otherwise turn this
    # case into a daemon spinning forever over nothing.
    scan(root)
    stop_requested = until if until is not None else _stop_event()
    connection = _store.create(root)
    _process.claim(root)
    _store.write_meta(root, pid=os.getpid())

    previous = _store.last_window(connection)
    now = time.time()
    across_gap = (
        previous is not None and now - previous["until"] > _store.GAP_TOLERANCE_SECONDS
    )
    _store.open_window(connection, now)

    try:
        cycle = 0
        while True:
            _sweep(root, connection, time.time(), across_gap=across_gap)
            across_gap = False
            cycle += 1
            if cycle % SWEEP_EVERY == 0:
                _store.enforce_bounds(connection, time.time())
            if stop_requested.wait(interval):
                return
    finally:
        _store.heartbeat(connection, time.time())
        connection.close()
        _process.release(root)
        _store.write_meta(root, pid=None)


def read(root: Path) -> Observation:
    """Open the store for a night run, revalidating it against the tree.

    Never raises on a bad store. A run that cannot tell "there is no history"
    from "there was no churn" is the failure this component exists to prevent,
    so the answer is always an `Observation` that says which one it is.
    """
    root = Path(root).resolve()
    files = tuple(scan(root).files)
    index = _graph.module_index(files)

    connection, trust = _store.open_for_read(root)
    if connection is None:
        return _cold(root, trust, files, index)

    try:
        stale = _drifted(root, connection, files)
        imports, symbols = _load(connection, files, stale)
        for path in stale:
            facts = _graph.parse(root, path, index)
            imports[path] = facts.imports
            symbols[path] = facts.symbols
        churn = _load_churn(connection, files)
        unknown = _unknown_paths(connection, files) | set(stale)
        return Observation(
            root=root,
            trust=trust,
            files=files,
            stale=tuple(sorted(stale)),
            gaps=_store.gaps(connection, time.time()),
            _imports=imports,
            _symbols=symbols,
            _churn=churn,
            _unknown=frozenset(unknown),
        )
    finally:
        connection.close()


def start(root: Path) -> int:
    """Detach a daemon for `root`, returning its pid.

    The first-party set is resolved here as well as in the child, so that a repo
    Polish would decline fails immediately with the message that names the fix,
    rather than as a detached process that dies where nobody is looking.
    """
    root = Path(root).resolve()
    scan(root)
    return _process.start(root)


def compact(
    root: Path,
    *,
    retention_days: int = RETENTION_DAYS,
    max_bytes: int = MAX_BYTES,
) -> None:
    """Apply retention and reclaim the space. Run after a night run.

    Both bounds are parameters because both are guesses. ADR 0002 records 14
    days and 100 MB as starting numbers chosen without data, and a number nobody
    can move is a number nobody will ever correct.
    """
    root = Path(root).resolve()
    connection, _ = _store.open_for_read(root)
    if connection is None:
        return
    try:
        _store.compact(
            connection,
            time.time(),
            retention_days=retention_days,
            max_bytes=max_bytes,
        )
    finally:
        connection.close()


def describe(root: Path) -> str:
    """The human-readable answer to "what is this thing doing to my repo?"."""
    return _status.describe(Path(root).resolve())


def status_line(root: Path) -> str:
    return _status.one_line(Path(root).resolve())


# --- internals ------------------------------------------------------------


def _stop_event() -> threading.Event:
    """A stop flag wired to SIGTERM and SIGINT, when we own the main thread.

    `kill <pid>` has to be a first-class way to stop the daemon — SPEC §3.7's
    "trivially killable" is not satisfied by a bespoke command — and it has to
    close the uptime window on the way out, or the shutdown reads as a crash.
    """
    import signal

    event = threading.Event()
    if threading.current_thread() is threading.main_thread():
        for number in (signal.SIGTERM, signal.SIGINT):
            signal.signal(number, lambda *_: event.set())
    return event


def _sweep(root: Path, connection, now: float, *, across_gap: bool) -> None:
    """One pass over the first-party set, recording whatever moved."""
    try:
        files = tuple(scan(root).files)
    except (NotAPythonProject, OSError):
        # A repo mid-`git checkout` can momentarily look like it has no source.
        # Skipping the cycle costs one interval of history; acting on it would
        # mark the whole tree deleted.
        return
    index = _graph.module_index(files)
    known = {
        row["path"]: row
        for row in connection.execute(
            "SELECT path, mtime_ns, size, digest FROM files WHERE alive = 1"
        )
    }
    for relative in files:
        try:
            stat = (root / relative).stat()
        except OSError:
            continue
        row = known.pop(relative, None)
        if (
            row is not None
            and row["mtime_ns"] == stat.st_mtime_ns
            and row["size"] == stat.st_size
        ):
            continue
        facts = _graph.parse(root, relative, index)
        _store.record(connection, facts, stat, now, across_gap=across_gap)
    if known:
        _store.forget(connection, known, now)
    _store.heartbeat(connection, now)


def _drifted(root: Path, connection, files: tuple[str, ...]) -> set[str]:
    """Files the store's record of no longer matches the tree.

    The `(mtime, size)` fingerprint is the screen and the digest is the verdict,
    so a rebuild or a checkout that restores identical bytes does not force a
    needless re-parse — but anything the daemon genuinely missed does.
    """
    known = {
        row["path"]: row
        for row in connection.execute(
            "SELECT path, mtime_ns, size, digest FROM files WHERE alive = 1"
        )
    }
    stale = set()
    for relative in files:
        row = known.get(relative)
        if row is None:
            stale.add(relative)
            continue
        try:
            stat = (root / relative).stat()
        except OSError:
            stale.add(relative)
            continue
        if row["mtime_ns"] == stat.st_mtime_ns and row["size"] == stat.st_size:
            continue
        try:
            current = _graph.digest_of((root / relative).read_bytes())
        except OSError:
            stale.add(relative)
            continue
        if current != row["digest"]:
            stale.add(relative)
    return stale


def _load(connection, files: tuple[str, ...], stale: set[str]):
    fresh = set(files) - stale
    imports: dict[str, frozenset[str]] = {}
    collected: dict[str, set[str]] = {}
    for row in connection.execute("SELECT path, imports FROM edges"):
        if row["path"] in fresh:
            collected.setdefault(row["path"], set()).add(row["imports"])
    imports.update({path: frozenset(targets) for path, targets in collected.items()})

    symbols: dict[str, list[Symbol]] = {}
    for row in connection.execute(
        "SELECT path, qualname, kind, lineno, digest FROM symbols WHERE alive = 1"
    ):
        if row["path"] in fresh:
            symbols.setdefault(row["path"], []).append(
                Symbol(row["qualname"], row["kind"], row["lineno"], row["digest"])
            )
    return imports, {path: tuple(found) for path, found in symbols.items()}


def _load_churn(
    connection, files: tuple[str, ...]
) -> dict[tuple[str, str | None], int]:
    wanted = set(files)
    churn: dict[tuple[str, str | None], int] = {}
    for row in connection.execute("SELECT path, churn FROM files WHERE alive = 1"):
        if row["path"] in wanted:
            churn[(row["path"], None)] = row["churn"]
    for row in connection.execute(
        "SELECT path, qualname, churn FROM symbols WHERE alive = 1"
    ):
        if row["path"] in wanted:
            churn[(row["path"], row["qualname"])] = row["churn"]
    return churn


def _unknown_paths(connection, files: tuple[str, ...]) -> set[str]:
    wanted = set(files)
    return {
        row["path"]
        for row in connection.execute(
            "SELECT path FROM files WHERE unknown_at IS NOT NULL"
        )
        if row["path"] in wanted
    }


def _cold(root: Path, trust: Trust, files: tuple[str, ...], index) -> Observation:
    """Rebuild the graph from the tree, with no history at all.

    The fallback the acceptance criteria require: a run reaches this whenever the
    store is missing, version-skewed or unreadable, and gets a correct graph with
    every churn answer marked unknown.
    """
    imports = {}
    symbols = {}
    for relative in files:
        facts = _graph.parse(root, relative, index)
        imports[relative] = facts.imports
        symbols[relative] = facts.symbols
    return Observation(
        root=root,
        trust=trust,
        files=files,
        stale=files,
        gaps=(),
        _imports=imports,
        _symbols=symbols,
        _churn={},
        _unknown=frozenset(files),
    )
