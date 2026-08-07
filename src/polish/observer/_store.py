"""The store: SQLite in a `.polish/` directory that ignores itself.

Format and location are ADR 0002. The two properties that decide most of this
module:

* **Concurrency.** The daemon writes all day while a night run reads. WAL mode
  gives a writer and readers without blocking, and transactions make a torn
  write structurally impossible — which is most of crash recovery for free.
* **Inspectability.** SPEC §3.7 requires the daemon be "trivially inspectable".
  `sqlite3 .polish/graph.db .schema` is that, with no tool Polish had to ship.

Everything here is private to `polish.observer`. The public reader never hands
out a connection.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

#: Bumped whenever the schema below changes. There is no migration path by
#: design (issue #19): the graph is rebuildable from the tree in seconds, so
#: migrating it would be carrying risk for no benefit.
SCHEMA_VERSION = 1

STORE_DIR = ".polish"
DATABASE = "graph.db"
META = "meta.json"
PID_FILE = "daemon.pid"

#: Starting numbers, accepted without strong feelings and tunable once there is
#: real data from a churning repo. See ADR 0002.
RETENTION_DAYS = 14
MAX_BYTES = 100 * 1024 * 1024

#: Events evicted per pass once the ceiling is hit. Large enough that a badly
#: oversized store converges quickly, small enough that each pass stays a short
#: write the daemon's next poll is not stuck behind.
EVICTION_BATCH = 10000

#: Below this, a hole between two uptime windows is a scheduling hiccup rather
#: than downtime. Above it, history is missing and churn goes UNKNOWN.
GAP_TOLERANCE_SECONDS = 60.0

SCHEMA = """
CREATE TABLE files (
    path       TEXT PRIMARY KEY,
    digest     TEXT NOT NULL,
    mtime_ns   INTEGER NOT NULL,
    size       INTEGER NOT NULL,
    parsed     INTEGER NOT NULL,
    alive      INTEGER NOT NULL,
    churn      INTEGER NOT NULL,
    unknown_at REAL,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL
);

CREATE TABLE symbols (
    path       TEXT NOT NULL,
    qualname   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    lineno     INTEGER NOT NULL,
    digest     TEXT NOT NULL,
    churn      INTEGER NOT NULL,
    alive      INTEGER NOT NULL,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL,
    PRIMARY KEY (path, qualname)
);

CREATE TABLE edges (
    path    TEXT NOT NULL,
    imports TEXT NOT NULL,
    PRIMARY KEY (path, imports)
);
CREATE INDEX edges_imports ON edges (imports);

CREATE TABLE events (
    at       REAL NOT NULL,
    path     TEXT NOT NULL,
    qualname TEXT,
    kind     TEXT NOT NULL
);
CREATE INDEX events_at ON events (at);

CREATE TABLE windows (
    started_at REAL PRIMARY KEY,
    until      REAL NOT NULL
);
"""


class Trust(Enum):
    """Whether a run may believe the stored graph.

    Anything but `USABLE` means cold parse. The distinction between the three
    failure values is for the operator reading `polish observe status`; to a run
    they are the same instruction.
    """

    USABLE = "usable"
    MISSING = "missing"
    SCHEMA_MISMATCH = "schema-mismatch"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Gap:
    """A stretch of the day the daemon did not watch."""

    start: float
    end: float

    @property
    def seconds(self) -> float:
        return self.end - self.start


def store_dir(root: Path) -> Path:
    return root / STORE_DIR


def database_path(root: Path) -> Path:
    return store_dir(root) / DATABASE


def pid_path(root: Path) -> Path:
    return store_dir(root) / PID_FILE


def create(root: Path) -> sqlite3.Connection:
    """Create (or reopen) the store, leaving `.polish/` fit to ignore itself."""
    directory = store_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    _write_self_ignore(directory)

    connection = _connect(database_path(root))
    if _user_version(connection) != SCHEMA_VERSION:
        connection.close()
        _discard(root)
        directory.mkdir(parents=True, exist_ok=True)
        _write_self_ignore(directory)
        connection = _connect(database_path(root))
        connection.executescript(SCHEMA)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    write_meta(root, pid=None)
    return connection


def open_for_read(root: Path) -> tuple[sqlite3.Connection | None, Trust]:
    """Open the store without writing to it, and say whether to believe it.

    A reader that repairs what it finds would be a second writer competing with
    the daemon, and — worse — would hide the fact that the store was broken from
    the run that most needs to know.
    """
    if not database_path(root).is_file():
        return None, Trust.MISSING
    try:
        connection = _connect(database_path(root))
        version = _user_version(connection)
        connection.execute("SELECT count(*) FROM files").fetchone()
    except sqlite3.Error:
        return None, Trust.UNREADABLE
    if version != SCHEMA_VERSION:
        connection.close()
        return None, Trust.SCHEMA_MISMATCH
    return connection, Trust.USABLE


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    # Set before any table exists, so that `PRAGMA incremental_vacuum` can
    # actually return pages to the filesystem later. A ceiling enforced only by
    # deleting rows is not a ceiling: SQLite keeps the free pages.
    connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _user_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _discard(root: Path) -> None:
    """Delete the database and its WAL sidecars. Used on version skew."""
    for suffix in ("", "-wal", "-shm"):
        Path(str(database_path(root)) + suffix).unlink(missing_ok=True)


def _write_self_ignore(directory: Path) -> None:
    """Make `.polish/` invisible to git without touching the user's `.gitignore`.

    A `.gitignore` containing `*` inside the directory excludes the directory
    from `git status` and from Polish's own first-party walk. The same trick
    `uv` uses for `.venv`. Editing the user's own `.gitignore` was rejected: it
    is a file they curated, and the edit would surface as a surprise in their
    next diff.
    """
    ignore = directory / ".gitignore"
    if not ignore.is_file() or ignore.read_text(encoding="utf-8").strip() != "*":
        ignore.write_text("*\n", encoding="utf-8")


def write_meta(root: Path, *, pid: int | None) -> None:
    """Restate, in plain text, what is stored and where.

    Duplicates what the database already knows. That is the point: it answers
    "what is this directory?" with `cat`, for someone who has just found
    `.polish/` in their repo and has no reason yet to trust it.
    """
    meta = {
        "tool": "polish observer",
        "schema_version": SCHEMA_VERSION,
        "store": DATABASE,
        "pid": pid,
        "pid_file": PID_FILE,
        "retention_days": RETENTION_DAYS,
        "max_bytes": MAX_BYTES,
        "stores": (
            "the code graph (files, symbols, imports) and the intra-day change "
            "history for this repository's first-party Python files"
        ),
        "inspect": f"sqlite3 {STORE_DIR}/{DATABASE} .schema",
        "written_at": time.time(),
    }
    path = store_dir(root) / META
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


# --- uptime windows -------------------------------------------------------
#
# The graph is verifiable against the tree; the history is not. So the daemon
# records exactly when it was watching, and everything downstream treats an
# unwatched stretch as absent data rather than as quiet.


def open_window(connection: sqlite3.Connection, now: float) -> None:
    connection.execute(
        "INSERT INTO windows (started_at, until) VALUES (?, ?)", (now, now)
    )


def heartbeat(connection: sqlite3.Connection, now: float) -> None:
    connection.execute(
        "UPDATE windows SET until = ? "
        "WHERE started_at = (SELECT max(started_at) FROM windows)",
        (now,),
    )


def last_window(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT started_at, until FROM windows ORDER BY started_at DESC LIMIT 1"
    ).fetchone()


def gaps(connection: sqlite3.Connection, now: float) -> tuple[Gap, ...]:
    """Every stretch since the first window in which nobody was watching.

    Includes the trailing gap up to `now`, because a daemon that stopped at 6pm
    leaves the evening unobserved just as surely as one that crashed at noon.
    """
    rows = connection.execute(
        "SELECT started_at, until FROM windows ORDER BY started_at"
    ).fetchall()
    if not rows:
        return ()
    found = [
        Gap(previous["until"], nxt["started_at"])
        for previous, nxt in zip(rows, rows[1:], strict=False)
        if nxt["started_at"] - previous["until"] > GAP_TOLERANCE_SECONDS
    ]
    if now - rows[-1]["until"] > GAP_TOLERANCE_SECONDS:
        found.append(Gap(rows[-1]["until"], now))
    return tuple(found)


# --- writes ---------------------------------------------------------------


def record(
    connection: sqlite3.Connection,
    facts,
    stat: os.stat_result,
    now: float,
    *,
    across_gap: bool,
) -> None:
    """Fold one file's current facts into the store, in a single transaction.

    `across_gap` is the safety-critical parameter. When the daemon comes back up
    and finds a file changed, it knows the file changed but not how many times,
    nor in what order relative to anything else. Recording that as "+1 revision"
    would be inventing history; the file is flagged churn-unknown instead.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        previous = connection.execute(
            "SELECT digest FROM files WHERE path = ?", (facts.path,)
        ).fetchone()
        changed = previous is not None and previous["digest"] != facts.digest
        # A file first seen after a gap is unknown too: "created during the
        # downtime" and "created and then rewritten six times during the
        # downtime" leave the identical trace.
        unknown = across_gap and (previous is None or changed)
        connection.execute(
            """
            INSERT INTO files (path, digest, mtime_ns, size, parsed, alive, churn,
                               unknown_at, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, 1, 0, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                digest = excluded.digest,
                mtime_ns = excluded.mtime_ns,
                size = excluded.size,
                parsed = excluded.parsed,
                alive = 1,
                churn = churn + ?,
                unknown_at = coalesce(excluded.unknown_at, unknown_at),
                last_seen = excluded.last_seen
            """,
            (
                facts.path,
                facts.digest,
                stat.st_mtime_ns,
                stat.st_size,
                int(facts.parsed),
                now if unknown else None,
                now,
                now,
                int(changed and not across_gap),
            ),
        )
        if previous is None:
            _log(connection, now, facts.path, None, "added")
        elif changed:
            _log(connection, now, facts.path, None, "unknown" if unknown else "changed")
        _record_symbols(connection, facts, now, across_gap=across_gap)
        _record_edges(connection, facts)
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def _record_symbols(connection, facts, now: float, *, across_gap: bool) -> None:
    previous = {
        row["qualname"]: row
        for row in connection.execute(
            "SELECT qualname, digest, alive FROM symbols WHERE path = ?", (facts.path,)
        )
    }
    for symbol in facts.symbols:
        was = previous.pop(symbol.qualname, None)
        # A symbol coming back from the dead is a revision like any other — the
        # deleted-and-restored cycle is precisely the churn signal being counted.
        restored = was is not None and not was["alive"]
        changed = was is not None and (was["digest"] != symbol.digest or restored)
        connection.execute(
            """
            INSERT INTO symbols (path, qualname, kind, lineno, digest, churn, alive,
                                 first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, 0, 1, ?, ?)
            ON CONFLICT(path, qualname) DO UPDATE SET
                kind = excluded.kind,
                lineno = excluded.lineno,
                digest = excluded.digest,
                churn = churn + ?,
                alive = 1,
                last_seen = excluded.last_seen
            """,
            (
                facts.path,
                symbol.qualname,
                symbol.kind,
                symbol.lineno,
                symbol.digest,
                now,
                now,
                int(changed and not across_gap),
            ),
        )
        if was is None:
            _log(connection, now, facts.path, symbol.qualname, "added")
        elif changed:
            kind = "restored" if restored else "changed"
            _log(connection, now, facts.path, symbol.qualname, kind)
    # Whatever is left defined yesterday and gone now. The row stays: a function
    # written at 2pm and deleted at 5pm is the single clearest example SPEC §3.7
    # gives of what `git diff` erases, and deleting the row would erase it again.
    for qualname in previous:
        if previous[qualname]["alive"]:
            connection.execute(
                "UPDATE symbols SET alive = 0, last_seen = ? "
                "WHERE path = ? AND qualname = ?",
                (now, facts.path, qualname),
            )
            _log(connection, now, facts.path, qualname, "deleted")


def _record_edges(connection, facts) -> None:
    connection.execute("DELETE FROM edges WHERE path = ?", (facts.path,))
    connection.executemany(
        "INSERT INTO edges (path, imports) VALUES (?, ?)",
        [
            (facts.path, target)
            for target in sorted(facts.imports)
            if target != facts.path
        ],
    )


def forget(connection: sqlite3.Connection, paths: Iterable[str], now: float) -> None:
    """Mark files that have left the tree as dead, keeping their history."""
    for path in paths:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "UPDATE files SET alive = 0, last_seen = ? "
                "WHERE path = ? AND alive = 1",
                (now, path),
            )
            connection.execute(
                "UPDATE symbols SET alive = 0, last_seen = ? "
                "WHERE path = ? AND alive = 1",
                (now, path),
            )
            connection.execute("DELETE FROM edges WHERE path = ?", (path,))
            _log(connection, now, path, None, "deleted")
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise


def _log(connection, at: float, path: str, qualname: str | None, kind: str) -> None:
    connection.execute(
        "INSERT INTO events (at, path, qualname, kind) VALUES (?, ?, ?, ?)",
        (at, path, qualname, kind),
    )


# --- bounds ---------------------------------------------------------------


def enforce_bounds(
    connection: sqlite3.Connection,
    now: float,
    *,
    retention_days: int = RETENTION_DAYS,
    max_bytes: int = MAX_BYTES,
) -> None:
    """Apply the retention policy, then the hard ceiling. Oldest first.

    Retention is the policy; the ceiling is the backstop for a repo that churns
    hard enough to blow past 100 MB inside the retention window. Only history is
    evicted — the graph is proportional to repo size, is what a run's scope
    depends on, and evicting it would trade a bounded disk cost for an unbounded
    correctness one.
    """
    cutoff = now - retention_days * 86400
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DELETE FROM events WHERE at < ?", (cutoff,))
        connection.execute("DELETE FROM windows WHERE until < ?", (cutoff,))
        connection.execute(
            "DELETE FROM symbols WHERE alive = 0 AND last_seen < ?", (cutoff,)
        )
        connection.execute(
            "DELETE FROM files WHERE alive = 0 AND last_seen < ?", (cutoff,)
        )
        connection.execute(
            "UPDATE files SET unknown_at = NULL WHERE unknown_at < ?", (cutoff,)
        )
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise

    while size_bytes(connection) > max_bytes:
        deleted = connection.execute(
            "DELETE FROM events WHERE rowid IN "
            "(SELECT rowid FROM events ORDER BY at LIMIT ?)",
            (EVICTION_BATCH,),
        ).rowcount
        connection.execute("PRAGMA incremental_vacuum")
        if not deleted:
            # Nothing left to evict: the graph alone is over the ceiling. Better
            # an oversized store than a silently truncated one — a run would
            # rather cold-parse a big repo than scope itself from half a graph.
            break
    connection.execute("PRAGMA incremental_vacuum")


def compact(
    connection: sqlite3.Connection,
    now: float,
    *,
    retention_days: int = RETENTION_DAYS,
    max_bytes: int = MAX_BYTES,
) -> None:
    """Bounds plus a full VACUUM. Runs after a night run, not during the day."""
    enforce_bounds(connection, now, retention_days=retention_days, max_bytes=max_bytes)
    connection.execute("VACUUM")


def size_bytes(connection: sqlite3.Connection) -> int:
    page_count = connection.execute("PRAGMA page_count").fetchone()[0]
    page_size = connection.execute("PRAGMA page_size").fetchone()[0]
    return int(page_count) * int(page_size)
