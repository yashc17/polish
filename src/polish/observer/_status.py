"""What `polish observe status` prints.

SPEC §3.7 accepts an always-on process only if it is trivially inspectable, and
issue #19 makes "says clearly what it is storing and where" an acceptance
criterion. So this deliberately over-shares: the pid, the exact path, the size
against its ceiling, what is in there, where the history has holes, and the two
commands that inspect and stop it without going through Polish at all.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from . import _process, _store


def one_line(root: Path) -> str:
    pid = _process.running_pid(root)
    return f"running (pid {pid})" if pid else "not running"


def describe(root: Path) -> str:
    lines = [f"polish observer — {root}"]
    pid = _process.running_pid(root)
    lines.append(f"  status    {one_line(root)}")

    database = _store.database_path(root)
    if not database.is_file():
        lines.append(f"  store     none yet (would be {database})")
        lines.append(f"  start     polish observe start {root}")
        return "\n".join(lines)

    connection, trust = _store.open_for_read(root)
    size = database.stat().st_size
    lines.append(
        f"  store     {database} ({_bytes(size)} of {_bytes(_store.MAX_BYTES)} ceiling)"
    )
    if connection is None:
        lines.append(f"  schema    {trust.value} — a run will cold-parse instead")
        lines.append(f"  fix       polish observe start {root}   (rebuilds the store)")
        return "\n".join(lines)

    try:
        lines.append(f"  schema    version {_store.SCHEMA_VERSION}")
        lines.extend(_contents(connection))
        lines.extend(_uptime(connection))
    finally:
        connection.close()

    lines.append(f"  inspect   sqlite3 {database} .schema")
    lines.append(
        f"  stop      polish observe stop {root}"
        + (f"   (or: kill {pid})" if pid else "")
    )
    return "\n".join(lines)


def _contents(connection) -> list[str]:
    files = connection.execute("SELECT count(*) FROM files WHERE alive = 1").fetchone()[
        0
    ]
    symbols = connection.execute(
        "SELECT count(*) FROM symbols WHERE alive = 1"
    ).fetchone()[0]
    buried = connection.execute(
        "SELECT count(*) FROM symbols WHERE alive = 0"
    ).fetchone()[0]
    events = connection.execute("SELECT count(*), min(at) FROM events").fetchone()
    since = f" since {_when(events[1])}" if events[1] else ""
    return [
        f"  tracking  {files} first-party files, {symbols} symbols "
        f"({buried} since deleted)",
        f"  history   {events[0]} change events{since}; "
        f"{_store.RETENTION_DAYS} days retained",
    ]


def _uptime(connection) -> list[str]:
    now = time.time()
    windows = connection.execute("SELECT count(*) FROM windows").fetchone()[0]
    gaps = _store.gaps(connection, now)
    unknown = connection.execute(
        "SELECT count(*) FROM files WHERE alive = 1 AND unknown_at IS NOT NULL"
    ).fetchone()[0]
    if not gaps:
        return [f"  uptime    {windows} window(s), no gaps — churn is known"]
    lines = [f"  uptime    {windows} window(s), {len(gaps)} gap(s):"]
    for gap in gaps[-3:]:
        lines.append(f"              {_when(gap.start)}–{_when(gap.end)}")
    lines.append(
        f"            churn UNKNOWN for {unknown} file(s) — never read as 'no churn'"
    )
    return lines


def _when(at: float) -> str:
    return datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M")


def _bytes(count: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024
    raise AssertionError  # pragma: no cover
