"""Incremental scoping and the dependency closure — SPEC §3.6, issue #20.

Everything here goes through `polish.scope.select()` / `record()` and
`polish.envelope.run()`, on the seam agreed in #9: nothing reaches past a
public interface into `_ledger` or `_closure`.

The daemon is driven the way `test_observer.py` drives it — one synchronous
`observe()` per simulated stretch of the day — because an incremental scope is
only reachable with a store the run can believe, and a cold parse is
deliberately not one.

Two things this file is trying hard to be able to fail on, because both are
negatives and a broken implementation still leaves a green suite otherwise:

* **The closure is not decoration.** If the closure were dropped entirely, the
  scope would still contain every file that changed, every acceptance criterion
  about *what changed* would still pass, and the run would still refactor
  something. `test_a_function_stranded_three_files_away...` is what fails.
* **The closure must not zig-zag.** Walking up an edge and then back down one
  is the undirected connected component, which in any package with an
  `__init__.py` is the whole package — an incremental run that is correctly and
  uselessly a full sweep. `test_a_sibling_that_shares_a_dependency...` is what
  fails.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from conftest import commit_repo, materialise
from polish import envelope, observer, scope, tier0
from polish.firstparty import scan

PROJECT = {"pyproject.toml": '[project]\nname = "demo"\n'}

#: A three-link import chain, plus a module connected to nothing. `a` imports
#: `b` imports `c`, so `c` is the "function three files away" SPEC §3.6 names,
#: and `far` is the file that proves the scope is a scope and not a sweep.
CHAIN = {
    **PROJECT,
    "demo/__init__.py": "",
    "demo/a.py": "from demo import b\n\n\ndef top():\n    return b.middle()\n",
    "demo/b.py": "from demo import c\n\n\ndef middle():\n    return c.bottom()\n",
    "demo/c.py": "def bottom():\n    return 1\n",
    "demo/far.py": "UNRELATED = 1\n",
}

#: The same chain with a suite, so the tier probe reaches gate 3 and reports how
#: many first-party modules it looked at. The assertion is `a.top()` truthy
#: rather than equal to anything, so the day's edit does not turn the fixture
#: into a test about pytest's exit code.
RUNNABLE = {
    **CHAIN,
    "tests/test_chain.py": (
        "from demo import a\n\n\ndef test_top():\n    assert a.top()\n"
    ),
}

#: The chain with a removable unused import in two places: one inside the
#: closure of the day's edit, one outside it. Tier 0 would take both, so what
#: it leaves alone is the evidence that the scope was obeyed and not merely
#: computed and logged.
LITTERED = {
    **CHAIN,
    "demo/c.py": "import json\n\n\ndef bottom():\n    return 1\n",
    "demo/far.py": "import string\n\nUNRELATED = 1\n",
}

#: `shared.py` imports `hashlib` and never uses it; `consumer.py` imports it
#: back out of `shared`. Removing it from `shared` alone breaks a file that
#: never mentions `hashlib` — the cross-file reference only Tier 0's type check
#: can see.
CROSS_FILE = {
    **PROJECT,
    "demo/__init__.py": "",
    "demo/shared.py": "import hashlib\n\n\ndef digest(text):\n    return text\n",
    "demo/consumer.py": (
        "from demo.shared import hashlib\n\n\n"
        "def sha(text):\n    return hashlib.sha256(text).hexdigest()\n"
    ),
}

#: A `src/` layout with the suite outside it, importing the package absolutely
#: — `psf/requests` in miniature, and the shape #30 was found on. The boundary
#: between `src/` and `tests/` is crossed by exactly one edge, and only if the
#: graph names `src/mypkg/__init__.py` the way an interpreter would.
SRC_LAYOUT = {
    "pyproject.toml": '[project]\nname = "mypkg"\n',
    "src/mypkg/__init__.py": "from mypkg.core import go\n",
    "src/mypkg/core.py": "def go():\n    return 1\n",
    "tests/__init__.py": "",
    "tests/test_go.py": "import mypkg\n\n\ndef test_go():\n    assert mypkg.go()\n",
}

#: Two top-level modules that import the same third one and never each other.
#: Flat rather than a package on purpose: a package `__init__.py` is imported by
#: every module under it, which would connect the siblings for a real reason and
#: hide the zig-zag this shape exists to catch.
SIBLINGS = {
    **PROJECT,
    "shared.py": "VALUE = 1\n",
    "sibling_one.py": "import shared\n\n\ndef one():\n    return shared.VALUE\n",
    "sibling_two.py": "import shared\n\n\ndef two():\n    return shared.VALUE\n",
}


def watch(root: Path, cycles: int = 1) -> None:
    """Run the daemon for exactly `cycles` sweeps, then let it shut down.

    `observe()` sweeps before it checks the stop flag, so an event that is
    already set still yields one full pass over the tree.
    """
    for _ in range(cycles):
        stop = threading.Event()
        stop.set()
        observer.observe(root, interval=0, until=stop)


def rewrite(root: Path, relative: str, text: str) -> None:
    """Write `text` and force a distinct mtime.

    Two writes inside one filesystem timestamp tick are indistinguishable to any
    watcher, so without this the daemon would miss the edit, mark the file stale
    and hand back an observation with a hole in it — which is a different test.
    """
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    stamp = time.time() + rewrite.tick
    rewrite.tick += 1
    os.utime(path, (stamp, stamp))


rewrite.tick = 1


def workspace_of(root: Path) -> Path:
    return root / ".polish"


def select(root: Path, **kwargs) -> scope.Scope:
    return scope.select(root, scan(root).files, workspace=workspace_of(root), **kwargs)


def settled(root: Path, run_id: str = "yesterday") -> scope.Scope:
    """Watch the tree, scope it, and leave a watermark behind. The day before."""
    watch(root)
    previous = select(root)
    scope.record(workspace_of(root), previous, run_id=run_id)
    return previous


# --- the first run: a whole-repo sweep ------------------------------------


def test_the_first_run_sweeps_the_whole_repository(make_repo):
    repo = make_repo(CHAIN)
    watch(repo)

    selected = select(repo)

    assert selected.kind is scope.Kind.SWEEP
    assert set(selected.files) == set(scan(repo).files)
    assert "first run" in selected.reason
    assert scope.LEDGER in selected.reason


def test_a_sweep_leaves_a_watermark_the_next_run_can_scope_from(make_repo):
    repo = make_repo(CHAIN)
    settled(repo)

    assert (workspace_of(repo) / scope.LEDGER).is_file()
    assert select(repo).kind is scope.Kind.INCREMENTAL


# --- the closure: what the changed files drag in with them ----------------


def test_a_function_stranded_three_files_away_is_brought_into_scope(make_repo):
    """Issue #20's motivating case, in the form SPEC §3.6 states it.

    `a` stops importing `b`, which strands `c.bottom()` three files down a chain
    that no longer exists. Today's graph has no `a -> b` edge left to walk, so
    the only place that path survives is the previous run's edges — which is why
    the watermark carries them.
    """
    repo = make_repo(CHAIN)
    settled(repo)

    rewrite(repo, "demo/a.py", "def top():\n    return 1\n")
    watch(repo)

    selected = select(repo)

    assert selected.kind is scope.Kind.INCREMENTAL
    assert selected.changed == ("demo/a.py",)
    assert "demo/c.py" in selected.files, "the stranded function is out of scope"
    assert "demo/c.py" in selected.pulled_in
    assert "demo/b.py" in selected.files


def test_the_closure_reaches_forward_along_imports_that_still_exist(make_repo):
    repo = make_repo(CHAIN)
    settled(repo)

    rewrite(repo, "demo/a.py", "from demo import b\n\n\ndef top():\n    return 2\n")
    watch(repo)

    assert "demo/c.py" in select(repo).files


def test_the_closure_reaches_back_to_the_callers_of_a_changed_file(make_repo):
    """The other direction: change a leaf, and what imports it comes with it."""
    repo = make_repo(CHAIN)
    settled(repo)

    rewrite(repo, "demo/c.py", "def bottom():\n    return 2\n")
    watch(repo)

    selected = select(repo)

    assert selected.changed == ("demo/c.py",)
    assert {"demo/a.py", "demo/b.py"} <= set(selected.files)


def test_the_closure_crosses_a_src_layout_boundary(make_repo):
    """#30: an edit under `src/` reaches the suite that imports it.

    The whole claim of an incremental scope is that it is not quietly less safe
    than a sweep, and on this layout it was: the graph indexed
    `src/mypkg/__init__.py` as `src.mypkg`, `import mypkg` resolved to nothing,
    and code stranded on the test side of the boundary was never looked at.
    """
    repo = make_repo(SRC_LAYOUT)
    settled(repo)

    rewrite(repo, "src/mypkg/core.py", "def go():\n    return 2\n")
    watch(repo)

    selected = select(repo)

    assert selected.kind is scope.Kind.INCREMENTAL
    assert selected.changed == ("src/mypkg/core.py",)
    assert "tests/test_go.py" in selected.files, "the suite is out of scope"
    assert "tests/test_go.py" in selected.pulled_in


def test_a_file_connected_to_nothing_that_changed_stays_out_of_scope(make_repo):
    repo = make_repo(CHAIN)
    settled(repo)

    rewrite(repo, "demo/c.py", "def bottom():\n    return 2\n")
    watch(repo)

    selected = select(repo)

    assert "demo/far.py" not in selected.files
    assert set(selected.files) != set(scan(repo).files), "this is a sweep, not a scope"


def test_a_sibling_that_shares_a_dependency_is_not_pulled_in(make_repo):
    """The closure goes out from the seeds; it does not turn around.

    `sibling_one` and `sibling_two` both import `shared` and neither imports the
    other. Reaching `sibling_two` from a change to `sibling_one` means the walk
    went forward to `shared` and then back up — the undirected component, which
    in a real package is every file there is.
    """
    repo = make_repo(SIBLINGS)
    settled(repo)

    rewrite(repo, "sibling_one.py", "import shared\n\n\ndef one():\n    return 2\n")
    watch(repo)

    selected = select(repo)

    assert "shared.py" in selected.files, "the forward walk did not happen"
    assert "sibling_two.py" not in selected.files, "the closure zig-zagged"


def test_a_deleted_file_pulls_in_what_it_used_to_import(make_repo):
    """A caller that is gone strands what it called, and has no node to walk."""
    repo = make_repo(CHAIN)
    settled(repo)

    (repo / "demo" / "a.py").unlink()
    watch(repo)

    selected = select(repo)

    assert selected.deleted == ("demo/a.py",)
    assert "demo/a.py" not in selected.files, "a deleted file cannot be refactored"
    assert {"demo/b.py", "demo/c.py"} <= set(selected.files)


def test_a_new_file_is_in_scope_along_with_what_it_imports(make_repo):
    repo = make_repo(CHAIN)
    settled(repo)

    rewrite(repo, "demo/written_today.py", "from demo import c\n\nX = c.bottom\n")
    watch(repo)

    selected = select(repo)

    assert "demo/written_today.py" in selected.changed
    assert "demo/c.py" in selected.files


def test_a_quiet_day_scopes_to_nothing_rather_than_to_everything(make_repo):
    repo = make_repo(CHAIN)
    settled(repo, run_id="2026-08-06")

    selected = select(repo)

    assert selected.files == ()
    assert selected.kind is scope.Kind.INCREMENTAL
    assert selected.reason == "nothing has changed since run 2026-08-06"


# --- widening: every uncertainty resolves towards looking at more ---------


def test_a_cold_parsed_graph_falls_back_to_a_full_sweep(make_repo):
    """No store, no intra-day history — and a file changed and changed back
    would then read as untouched. The scope refuses to be narrowed by it."""
    repo = make_repo(CHAIN)
    settled(repo)
    for name in ("graph.db", "graph.db-wal", "graph.db-shm"):
        (workspace_of(repo) / name).unlink(missing_ok=True)

    selected = select(repo)

    assert selected.kind is scope.Kind.SWEEP
    assert set(selected.files) == set(scan(repo).files)
    assert observer.Trust.MISSING.value in selected.reason


def test_a_store_the_run_cannot_read_falls_back_to_a_full_sweep(make_repo):
    repo = make_repo(CHAIN)
    settled(repo)
    (workspace_of(repo) / "graph.db").write_bytes(b"not a database")

    assert select(repo).kind is scope.Kind.SWEEP


def test_a_watermark_that_cannot_be_read_falls_back_to_a_full_sweep(make_repo):
    repo = make_repo(CHAIN)
    settled(repo)
    (workspace_of(repo) / scope.LEDGER).write_text("{ truncated")

    selected = select(repo)

    assert selected.kind is scope.Kind.SWEEP
    assert set(selected.files) == set(scan(repo).files)


def test_a_watermark_from_another_version_falls_back_to_a_full_sweep(make_repo):
    repo = make_repo(CHAIN)
    settled(repo)
    ledger = workspace_of(repo) / scope.LEDGER
    ledger.write_text(ledger.read_text().replace('"version": 1', '"version": 99'))

    assert select(repo).kind is scope.Kind.SWEEP


def test_full_sweeps_a_repository_that_would_otherwise_be_incremental(make_repo):
    repo = make_repo(CHAIN)
    settled(repo)
    rewrite(repo, "demo/far.py", "UNRELATED = 2\n")
    watch(repo)

    assert select(repo).kind is scope.Kind.INCREMENTAL
    assert select(repo, full=True).kind is scope.Kind.SWEEP
    assert set(select(repo, full=True).files) == set(scan(repo).files)


def test_a_file_whose_history_has_a_hole_is_scoped_in_despite_matching_bytes(
    make_repo,
):
    """ADR 0003's rule, one layer up: absent history is not evidence of quiet.

    `demo/far.py` is byte-identical to the watermark. The daemon was down while
    it was written, so nothing can say whether it was rewritten four times in
    between — and `select()` may not read that silence as "nothing happened".
    """
    repo = make_repo(CHAIN)
    settled(repo)
    _rewind_window(repo, seconds=600)
    rewrite(repo, "demo/far.py", "UNRELATED = 2\n")
    watch(repo)
    rewrite(repo, "demo/far.py", "UNRELATED = 1\n")
    watch(repo)

    selected = select(repo)

    assert "demo/far.py" not in selected.changed, "the bytes match the watermark"
    assert "demo/far.py" in selected.unwitnessed
    assert "demo/far.py" in selected.files


# --- the watermark --------------------------------------------------------


def test_the_watermark_records_the_tree_the_run_looked_at(make_repo):
    repo = make_repo(CHAIN)
    settled(repo)

    rewrite(repo, "demo/far.py", "UNRELATED = 2\n")
    watch(repo)
    assert select(repo).changed == ("demo/far.py",)

    scope.record(workspace_of(repo), select(repo), run_id="today")
    assert select(repo).changed == ()


def test_a_watermark_is_only_written_when_it_is_recorded(make_repo):
    """`select()` never advances it. A run that died halfway through has not
    looked at what its scope claimed, so the next run must sweep."""
    repo = make_repo(CHAIN)
    watch(repo)

    select(repo)

    assert not (workspace_of(repo) / scope.LEDGER).exists()


def test_two_repositories_do_not_share_a_watermark(make_repo):
    first, second = make_repo(CHAIN), make_repo(CHAIN)
    settled(first)

    assert select(second).kind is scope.Kind.SWEEP


# --- the run log ----------------------------------------------------------


def test_the_scope_summary_states_the_kind_the_count_and_the_reason(make_repo):
    repo = make_repo(CHAIN)
    settled(repo, run_id="2026-08-06")
    rewrite(repo, "demo/c.py", "def bottom():\n    return 2\n")
    watch(repo)

    lines = select(repo).summary()

    assert lines[0].startswith("scope")
    assert "incremental" in lines[0]
    assert "3 first-party files in scope" in lines[0]
    assert lines[1].startswith("why")
    assert "1 changed since run 2026-08-06" in lines[1]
    assert "2 pulled in by the dependency closure" in lines[1]


def test_the_scope_summary_names_every_file_and_why_it_is_there(make_repo):
    repo = make_repo(CHAIN)
    settled(repo)
    rewrite(repo, "demo/c.py", "def bottom():\n    return 2\n")
    watch(repo)

    lines = select(repo).summary()

    assert "  ~ demo/c.py" in lines
    assert "  + demo/a.py" in lines
    assert "  + demo/b.py" in lines
    assert not [line for line in lines if "far.py" in line]


def test_a_sweep_says_so_without_listing_every_file(make_repo):
    repo = make_repo(CHAIN)
    watch(repo)

    lines = select(repo).summary()

    assert "full sweep" in lines[0]
    assert len(lines) == 2, "a sweep is every file; the count is the whole story"


def test_a_long_listing_is_truncated_rather_than_flooding_the_log(make_repo):
    repo = make_repo(
        {
            **PROJECT,
            **{f"mod{index}.py": "X = 1\n" for index in range(scope.LISTED + 5)},
        }
    )
    settled(repo)
    for index in range(scope.LISTED + 5):
        rewrite(repo, f"mod{index}.py", "X = 2\n")
    watch(repo)

    lines = select(repo).summary()

    assert len(lines) == scope.LISTED + 3
    assert lines[-1] == "  … and 5 more"


# --- end to end, through `polish run` -------------------------------------


@pytest.fixture(scope="module")
def nights(tmp_path_factory) -> tuple[envelope.RunRecord, envelope.RunRecord]:
    """Two consecutive night runs over one repository, with a day in between.

    Module-scoped because each run builds a real virtualenv and probes a real
    repository, which is the cost of not stubbing the thing under test. The
    suite is here so that the probe's third gate actually evaluates, which is
    what makes "the probe still saw the whole repository" observable.
    """
    root = commit_repo(materialise(tmp_path_factory.mktemp("nights"), RUNNABLE))
    watch(root)
    first = envelope.run(root)

    rewrite(root, "demo/c.py", "def bottom():\n    return 2\n")
    watch(root)
    second = envelope.run(root)

    return first, second


def test_the_first_night_sweeps_and_the_second_does_not(nights):
    first, second = nights

    assert first.scope is not None and first.scope.kind is scope.Kind.SWEEP
    assert set(first.scope.files) == set(first.files)

    assert second.scope is not None and second.scope.kind is scope.Kind.INCREMENTAL
    assert set(second.scope.files) < set(second.files)
    assert "demo/far.py" not in second.scope.files


def test_the_second_night_still_pulls_in_the_callers_of_what_changed(nights):
    _, second = nights

    assert second.scope is not None
    assert second.scope.changed == ("demo/c.py",)
    assert {"demo/a.py", "demo/b.py"} <= set(second.scope.files)


def test_the_run_log_states_what_was_in_scope_and_why(nights):
    first, second = nights

    assert any(
        line.startswith("scope") and "full sweep" in line for line in first.summary()
    )
    assert any(
        line.startswith("scope") and "incremental" in line for line in second.summary()
    )
    assert any("since run" in line for line in second.summary())
    assert "  ~ demo/c.py" in second.summary()


def test_the_probe_still_sees_the_whole_repository_on_an_incremental_night(nights):
    """The scope narrows what is refactored, never what the tier is judged on.

    The verdict is per-repository (#28 makes it per-symbol); computing it from a
    subset would be a narrower claim wearing the same name, and gate 3 is
    demote-only, so the only effect would be to make demotion less likely.
    """
    first, second = nights

    assert second.scope is not None
    assert set(second.scope.files) < set(second.files), "the night was not scoped"

    swept, scoped = _gate(first, "imports"), _gate(second, "imports")
    assert swept.status == "pass", "gate 3 never ran; the assertion below is vacuous"
    assert scoped.detail == swept.detail, (
        "the scoped night's import smoke covered fewer modules than the swept "
        "one's, which can only make a demotion less likely"
    )


def test_scoping_the_work_does_not_scope_the_proof(make_repo):
    """The scope may narrow what Polish edits. It may not narrow what it checks.

    Tier 0's type check is the only gate that can see a binding referenced from
    another file, so its surface is what "provably behaviour-preserving" means.
    The closure is supposed to bring every referencing file into scope — but the
    import graph misses what it cannot resolve (a dynamic import, a re-export
    through a path shape it does not address), and a proof that silently
    narrows with the scope would ship the removal anyway. So the proof does not
    narrow: it always covers the whole first-party set.
    """
    unguarded, guarded = make_repo(CROSS_FILE), make_repo(CROSS_FILE)
    scoped = ("demo/shared.py",)
    everything = ("demo/shared.py", "demo/consumer.py")

    without = list(tier0.refactor(unguarded, scoped))
    with_proof = list(tier0.refactor(guarded, scoped, verify_against=everything))

    assert [change.kept for change in without] == [True], (
        "the fixture no longer demonstrates a removal the scope alone would ship"
    )
    assert [change.kept for change in with_proof] == [False]
    assert "type check" in with_proof[0].evidence[0]


@pytest.fixture(scope="module")
def scoped_night(tmp_path_factory) -> envelope.RunRecord:
    """One incremental night over a repository with litter on both sides.

    The watermark is seeded with `record()` rather than by running a first
    night, because a first night would also have tidied `demo/far.py` — and an
    already-tidy file out of scope proves nothing. `record()` is exactly what a
    run leaves behind, so the state is the state a first night produces.
    """
    root = commit_repo(materialise(tmp_path_factory.mktemp("scoped"), LITTERED))
    watch(root)
    scope.record(workspace_of(root), select(root), run_id="2026-08-06")

    rewrite(root, "demo/c.py", "import json\n\n\ndef bottom():\n    return 2\n")
    watch(root)
    # Kept, so the type check's own response file survives the run and the
    # surface it covered can be read rather than inferred.
    return envelope.run(root, keep_sandbox=True)


def test_the_night_refactors_what_is_in_scope_and_leaves_the_rest(scoped_night):
    """The scope has to govern the edits, not just the log.

    `demo/far.py` carries an unused `import string` that Tier 0 can prove is
    removable, and it was left alone — which is only true if the scope reached
    the refactor engine.
    """
    assert scoped_night.scope is not None
    assert scoped_night.scope.kind is scope.Kind.INCREMENTAL
    assert "demo/far.py" not in scoped_night.scope.files

    assert scoped_night.changes == (
        "refactor(imports): drop 1 unused import from demo/c.py",
    )
    assert not [note for note in scoped_night.dropped if "far.py" in note]


def test_the_night_proves_its_changes_against_the_whole_repository(scoped_night):
    """The scoped night's type check still covered every first-party file.

    Read from mypy's own response file rather than inferred: the surface that
    file lists *is* what "provably behaviour-preserving" was checked over, and
    the point of the assertion is that it did not shrink with the scope.
    """
    listed = (
        (scoped_night.worktree.parent / tier0.CACHE_DIR / "files.txt")
        .read_text()
        .split()
    )

    assert scoped_night.scope is not None
    assert set(listed) == set(scoped_night.files)
    assert set(listed) > set(scoped_night.scope.files), "the night was not scoped"


def test_a_run_that_died_inside_the_refactor_leaves_no_watermark(
    make_git_repo, monkeypatch
):
    """The watermark is a claim: "this tree has been looked at".

    A run that crashed partway through has not looked at it, so writing the
    watermark is the last thing that happens rather than a bookkeeping detail
    at the top. Advancing it early costs tomorrow the whole of today's work,
    silently, which is the under-scoping SPEC §1.1 does not allow.
    """
    repo = make_git_repo(CHAIN)
    watch(repo)

    def crash(*_args, **_kwargs):
        raise OSError("the disk went away mid-refactor")

    monkeypatch.setattr(tier0, "refactor", crash)
    with pytest.raises(OSError):
        envelope.run(repo)

    assert not (workspace_of(repo) / scope.LEDGER).exists()
    assert select(repo).kind is scope.Kind.SWEEP


def test_a_night_that_never_ran_leaves_the_next_one_sweeping(make_git_repo):
    repo = make_git_repo(CHAIN)
    watch(repo)

    with pytest.raises(envelope.EnvelopeError):
        envelope.run(repo / "demo")

    assert not (workspace_of(repo) / scope.LEDGER).exists()
    assert select(repo).kind is scope.Kind.SWEEP


def _gate(record: envelope.RunRecord, name: str):
    assert record.tier is not None
    found = [gate for gate in record.tier.gates if gate.name == name]
    assert found, f"the run log carries no gate named {name}"
    return found[0]


def _rewind_window(repo: Path, *, seconds: float) -> None:
    """Backdate the daemon's last uptime window, so the next start reads a gap.

    Reaching into the store directly is the one thing this file does that the
    public interface cannot: downtime is a stretch of wall-clock time, and the
    alternative is a test that sleeps for it.
    """
    import sqlite3

    with sqlite3.connect(repo / ".polish" / "graph.db") as connection:
        connection.execute("UPDATE windows SET until = until - ?", (seconds,))
