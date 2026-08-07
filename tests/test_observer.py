"""The observer daemon, exercised through `observe()`, `read()` and `compact()`.

Following the seam agreed in #9, nothing here reaches past the public interface
into `_store`, `_graph` or `_process`.

The daemon is driven synchronously — one `observe()` call per simulated stretch
of the day, each ended by a pre-set stop event — rather than by starting a real
background process and sleeping. That keeps the tests deterministic, and it
makes downtime expressible: the gap between two `observe()` calls *is* the gap
the store has to notice.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

import pytest

from polish import observer
from polish.observer import ChurnUnknown, Trust

PROJECT = {"pyproject.toml": '[project]\nname = "demo"\n'}


def watch(root, cycles: int = 1) -> None:
    """Run the daemon for exactly `cycles` sweeps, then let it shut down.

    `observe()` sweeps before it checks the stop flag, so an event that is
    already set still yields one full pass over the tree.
    """
    for _ in range(cycles):
        stop = threading.Event()
        stop.set()
        observer.observe(root, interval=0, until=stop)


def rewrite(path, text: str) -> None:
    """Write `text` and force a distinct mtime.

    Two writes inside one filesystem timestamp tick are indistinguishable to any
    watcher, so without this the tests would be measuring clock granularity.
    """
    path.write_text(text)
    stamp = time.time() + rewrite.tick
    rewrite.tick += 1
    os.utime(path, (stamp, stamp))


rewrite.tick = 1


# --- storage: format, location, and staying out of the user's way ---------


def test_the_store_lives_in_a_directory_that_ignores_itself(make_repo):
    repo = make_repo({**PROJECT, "demo.py": "x = 1\n"})

    watch(repo)

    assert (repo / ".polish" / "graph.db").is_file()
    assert (repo / ".polish" / ".gitignore").read_text() == "*\n"
    assert not (repo / ".gitignore").exists(), "the user's gitignore must be untouched"


def test_the_store_is_readable_with_sqlite3_and_nothing_else(make_repo):
    repo = make_repo({**PROJECT, "demo.py": "def f():\n    return 1\n"})

    watch(repo)

    with sqlite3.connect(repo / ".polish" / "graph.db") as connection:
        symbols = connection.execute("SELECT qualname FROM symbols").fetchall()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert ("f",) in symbols
    assert version == 1


def test_meta_json_says_what_is_stored_and_where(make_repo):
    import json

    repo = make_repo({**PROJECT, "demo.py": "x = 1\n"})

    watch(repo)
    meta = json.loads((repo / ".polish" / "meta.json").read_text())

    assert meta["schema_version"] == 1
    assert meta["store"] == "graph.db"
    assert meta["retention_days"] == observer.RETENTION_DAYS
    assert meta["max_bytes"] == observer.MAX_BYTES
    assert "sqlite3" in meta["inspect"]


def test_status_names_the_store_the_ceiling_and_how_to_stop_it(make_repo):
    repo = make_repo({**PROJECT, "demo.py": "def f():\n    pass\n"})

    watch(repo)
    report = observer.describe(repo)

    assert ".polish/graph.db" in report
    assert "ceiling" in report
    assert "sqlite3" in report
    assert "polish observe stop" in report


# --- the graph ------------------------------------------------------------


def test_the_graph_records_symbols_and_first_party_imports(make_repo):
    repo = make_repo(
        {
            **PROJECT,
            "demo/__init__.py": "",
            "demo/core.py": "import json\n\n\ndef core():\n    return json\n",
            "demo/api.py": "from demo.core import core\n\n\nclass Api:\n    pass\n",
        }
    )

    watch(repo)
    seen = observer.read(repo)

    assert [s.qualname for s in seen.symbols("demo/api.py")] == ["Api"]
    assert "demo/core.py" in seen.dependencies("demo/api.py")
    assert "json" not in str(seen.dependencies("demo/api.py"))


def test_dependents_returns_the_transitive_closure(make_repo):
    repo = make_repo(
        {
            **PROJECT,
            "demo/__init__.py": "",
            "demo/leaf.py": "VALUE = 1\n",
            "demo/middle.py": "from demo.leaf import VALUE\n",
            "demo/top.py": "from demo.middle import VALUE\n",
            "demo/unrelated.py": "OTHER = 2\n",
        }
    )

    watch(repo)
    closure = observer.read(repo).dependents("demo/leaf.py")

    assert {"demo/leaf.py", "demo/middle.py", "demo/top.py"} <= closure
    assert "demo/unrelated.py" not in closure


def test_churn_counts_revisions_per_symbol_not_per_file(make_repo):
    repo = make_repo(
        {
            **PROJECT,
            "demo.py": "def stable():\n    return 1\n\n\ndef busy():\n    return 1\n",
        }
    )
    source = repo / "demo.py"

    watch(repo)
    for revision in range(2, 5):
        rewrite(
            source,
            f"def stable():\n    return 1\n\n\ndef busy():\n    return {revision}\n",
        )
        watch(repo)

    seen = observer.read(repo)
    assert seen.churn("demo.py", "busy").count == 3
    assert seen.churn("demo.py", "stable").count == 0


def test_code_written_and_deleted_the_same_day_is_still_remembered(make_repo):
    """The one thing `git diff` cannot tell a run — SPEC §3.7's whole argument."""
    repo = make_repo({**PROJECT, "demo.py": "def morning():\n    return 1\n"})
    source = repo / "demo.py"

    watch(repo)
    rewrite(
        source, "def morning():\n    return 1\n\n\ndef afternoon():\n    return 2\n"
    )
    watch(repo)
    rewrite(source, "def morning():\n    return 1\n")
    watch(repo)

    with sqlite3.connect(repo / ".polish" / "graph.db") as connection:
        buried = connection.execute(
            "SELECT qualname FROM symbols WHERE alive = 0"
        ).fetchall()
        events = [
            row[0]
            for row in connection.execute(
                "SELECT kind FROM events WHERE qualname = 'afternoon' ORDER BY at"
            )
        ]
    assert buried == [("afternoon",)]
    assert events == ["added", "deleted"]


# --- staleness: the graph is verifiable -----------------------------------


def test_an_edit_the_daemon_missed_is_detected_and_cold_parsed(make_repo):
    repo = make_repo({**PROJECT, "demo.py": "def old():\n    return 1\n"})

    watch(repo)
    rewrite(repo / "demo.py", "def brand_new():\n    return 1\n")

    seen = observer.read(repo)
    assert seen.stale == ("demo.py",)
    assert [s.qualname for s in seen.symbols("demo.py")] == ["brand_new"]


def test_a_file_created_while_the_daemon_was_down_is_detected(make_repo):
    repo = make_repo({**PROJECT, "demo/__init__.py": "", "demo/core.py": "X = 1\n"})

    watch(repo)
    (repo / "demo" / "late.py").write_text("from demo.core import X\n")

    seen = observer.read(repo)
    assert "demo/late.py" in seen.stale
    assert "demo/core.py" in seen.dependencies("demo/late.py")


def test_a_touched_but_unchanged_file_is_not_stale(make_repo):
    """The fingerprint is the screen; the digest is the verdict."""
    repo = make_repo({**PROJECT, "demo.py": "def f():\n    return 1\n"})

    watch(repo)
    stamp = time.time() + 500
    os.utime(repo / "demo.py", (stamp, stamp))

    assert observer.read(repo).stale == ()


def test_a_version_skewed_store_is_discarded_and_cold_parsed(make_repo):
    repo = make_repo({**PROJECT, "demo.py": "def f():\n    return 1\n"})
    watch(repo)

    with sqlite3.connect(repo / ".polish" / "graph.db") as connection:
        connection.execute("PRAGMA user_version = 99")

    seen = observer.read(repo)
    assert seen.trust is Trust.SCHEMA_MISMATCH
    assert seen.cold_parsed
    assert [s.qualname for s in seen.symbols("demo.py")] == ["f"], (
        "the fallback must still produce a correct graph, not an empty one"
    )

    # And the next daemon start rebuilds rather than migrates.
    watch(repo)
    assert observer.read(repo).trust is Trust.USABLE


def test_an_unreadable_store_falls_back_rather_than_raising(make_repo):
    repo = make_repo({**PROJECT, "demo.py": "def f():\n    return 1\n"})
    watch(repo)
    (repo / ".polish" / "graph.db").write_bytes(b"this is not a database")

    seen = observer.read(repo)
    assert seen.trust is Trust.UNREADABLE
    assert seen.cold_parsed
    assert [s.qualname for s in seen.symbols("demo.py")] == ["f"]


def test_a_missing_store_falls_back_rather_than_raising(make_repo):
    repo = make_repo({**PROJECT, "demo.py": "def f():\n    return 1\n"})

    seen = observer.read(repo)

    assert seen.trust is Trust.MISSING
    assert seen.cold_parsed
    assert [s.qualname for s in seen.symbols("demo.py")] == ["f"]


def test_a_file_that_will_not_parse_stays_in_the_graph(make_repo):
    repo = make_repo({**PROJECT, "demo.py": "def f(:\n"})

    watch(repo)
    seen = observer.read(repo)

    assert seen.stale == (), "a syntax error is not staleness — the file is known"
    assert seen.symbols("demo.py") == ()


# --- the history is not verifiable ----------------------------------------


def test_downtime_is_reported_as_a_gap(make_repo):
    repo = make_repo({**PROJECT, "demo.py": "x = 1\n"})

    watch(repo)
    _rewind_window(repo, seconds=3600)
    watch(repo)

    gaps = observer.read(repo).gaps
    assert len(gaps) == 1
    assert gaps[0].seconds == pytest.approx(3600, abs=60)


def test_churn_is_unknown_never_zero_for_a_file_changed_during_downtime(make_repo):
    repo = make_repo(
        {**PROJECT, "demo.py": "def f():\n    return 1\n", "quiet.py": "Y = 1\n"}
    )

    watch(repo)
    _rewind_window(repo, seconds=3600)
    rewrite(repo / "demo.py", "def f():\n    return 2\n")
    watch(repo)

    seen = observer.read(repo)
    churn = seen.churn("demo.py")
    assert not churn.known
    with pytest.raises(ChurnUnknown):
        _ = churn.count
    assert seen.unknown_churn() == ("demo.py",)
    assert seen.churn("quiet.py").count == 0, (
        "a file provably unchanged across the gap keeps its history"
    )


def test_churn_is_unknown_for_a_file_created_during_downtime(make_repo):
    repo = make_repo({**PROJECT, "demo.py": "x = 1\n"})

    watch(repo)
    _rewind_window(repo, seconds=3600)
    (repo / "late.py").write_text("def f():\n    return 1\n")
    watch(repo)

    assert not observer.read(repo).churn("late.py").known


def test_churn_is_unknown_for_an_edit_the_daemon_has_not_seen_yet(make_repo):
    """The daemon is down *now*: its count is behind by an unknown amount."""
    repo = make_repo({**PROJECT, "demo.py": "def f():\n    return 1\n"})

    watch(repo)
    rewrite(repo / "demo.py", "def f():\n    return 2\n")

    assert not observer.read(repo).churn("demo.py").known


def test_a_cold_parsed_run_reports_every_file_as_unknown_churn(make_repo):
    repo = make_repo({**PROJECT, "a.py": "A = 1\n", "b.py": "B = 1\n"})

    seen = observer.read(repo)

    assert seen.unknown_churn() == ("a.py", "b.py")
    for path in seen.files:
        assert not seen.churn(path).known


def test_status_says_churn_is_unknown_rather_than_absent(make_repo):
    repo = make_repo({**PROJECT, "demo.py": "def f():\n    return 1\n"})

    watch(repo)
    _rewind_window(repo, seconds=3600)
    rewrite(repo / "demo.py", "def f():\n    return 2\n")
    watch(repo)

    report = observer.describe(repo)
    assert "UNKNOWN" in report
    assert "no churn" in report


# --- bounds ---------------------------------------------------------------


def test_compact_evicts_history_past_the_retention_window(make_repo):
    repo = make_repo({**PROJECT, "demo.py": "def f():\n    return 1\n"})
    watch(repo)
    stale_by = (observer.RETENTION_DAYS + 1) * 86400

    with sqlite3.connect(repo / ".polish" / "graph.db") as connection:
        connection.execute("UPDATE events SET at = at - ?", (stale_by,))
        connection.execute(
            "UPDATE symbols SET alive = 0, last_seen = last_seen - ?", (stale_by,)
        )
    observer.compact(repo)

    with sqlite3.connect(repo / ".polish" / "graph.db") as connection:
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM symbols").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM files").fetchone()[0] == 1, (
            "the graph is bounded by the repo, not by retention"
        )


def test_compact_enforces_the_hard_ceiling_by_evicting_oldest_first(make_repo):
    """The backstop for a repo that churns past the ceiling inside 14 days."""
    repo = make_repo({**PROJECT, "demo.py": "def f():\n    return 1\n"})
    watch(repo)
    source = repo / "demo.py"
    for revision in range(2, 60):
        rewrite(source, f"def f():\n    return {revision}\n")
        watch(repo)

    database = repo / ".polish" / "graph.db"
    with sqlite3.connect(database) as connection:
        before = connection.execute("SELECT count(*), min(at) FROM events").fetchone()
    assert before[0] > 50

    observer.compact(repo, max_bytes=1)

    with sqlite3.connect(database) as connection:
        after = connection.execute("SELECT count(*), min(at) FROM events").fetchone()
        files = connection.execute("SELECT count(*) FROM files").fetchone()[0]
    assert after[0] < before[0]
    assert after[1] is None or after[1] > before[1], "eviction must run oldest-first"
    assert files == 1, "the ceiling evicts history, never the graph a run scopes by"


def test_compact_leaves_the_graph_intact_even_under_an_impossible_ceiling(make_repo):
    """A ceiling smaller than the graph is a bad number, not a licence to lie.

    Truncating the graph would leave a run scoping itself from half a dependency
    closure — an oversized store is by far the cheaper failure.
    """
    repo = make_repo({**PROJECT, "a.py": "A = 1\n", "b.py": "from a import A\n"})
    watch(repo)

    observer.compact(repo, max_bytes=1)
    seen = observer.read(repo)

    assert seen.trust is Trust.USABLE
    assert "a.py" in seen.dependents("a.py") and "b.py" in seen.dependents("a.py")


def test_compact_on_a_repo_with_no_store_is_a_no_op(make_repo):
    repo = make_repo({**PROJECT, "demo.py": "x = 1\n"})

    observer.compact(repo)

    assert not (repo / ".polish").exists()


# --- helpers --------------------------------------------------------------


def _rewind_window(repo, *, seconds: float) -> None:
    """Age the store's uptime record, so the next start looks like a restart.

    Sleeping for real would make the suite take longer than the gap tolerance;
    moving the recorded window backwards produces the identical situation.
    """
    with sqlite3.connect(repo / ".polish" / "graph.db") as connection:
        connection.execute(
            "UPDATE windows SET started_at = started_at - ?, until = until - ?",
            (seconds, seconds),
        )
