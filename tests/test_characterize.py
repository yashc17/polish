"""The characterization harness, against repositories that really execute.

Every repository below is built on disk, and every suite run is a real
subprocess in a real virtualenv. That is not incidental. This ticket's product
is a *negative* — a determinism gate that never quarantines anything leaves a
green suite, and so does one that quarantines correctly — so the only tests
worth having are ones where the non-determinism is real and the runs really
happen. A stubbed runner replaying recorded outcomes would pass identically
whether the harness ran the suite three times or zero.

The non-determinism is therefore built rather than simulated, one repository per
source issue #13 names: **time**, **randomness**, **network** and **iteration
order**. Two of the four are naturally reliable; the other two are made reliable
without being made fake — the flaky *test* counts its own runs on disk so that
"non-identical across N runs" is a fact rather than a probability, and the
iteration-order case varies dict insertion order rather than relying on
`PYTHONHASHSEED` happening to shuffle a set differently three times running.

`FAST` shrinks N and R for the repositories where the count is not what is under
test. `test_the_baseline_really_runs_three_times` deliberately does not use it.
"""

from __future__ import annotations

import pytest

from polish import envelope
from polish.characterize import Misconfigured, Policy, characterize, verify
from polish.probe import Tier

#: Smaller N and R, for the tests whose subject is not N and R. Every run here
#: is a real interpreter start and a real pytest collection.
FAST = Policy(baseline_runs=2, replays=2, retries=2)

PROJECT = '[project]\nname = "sample"\nversion = "0.1.0"\n'

MATHS = '''\
"""Deterministic by construction — every call here replays identically."""


def add(a, b):
    return a + b


def scale(values, factor=2):
    return [value * factor for value in values]


def label(name, *, upper=False):
    return name.upper() if upper else name


def never_called(value):
    """Executed by nothing, so it must show up as covered by nothing."""
    return value * 3
'''

MATHS_SUITE = """\
from sample.maths import add, label, scale


def test_add():
    assert add(1, 2) == 3


def test_scale():
    assert scale([1, 2]) == [2, 4]


def test_label():
    assert label("polish", upper=True) == "POLISH"
"""

#: One function per source of non-determinism in issue #13's criterion 6.
#: `port` is the network case and needs no network: an ephemeral port is
#: assigned by the kernel and differs every time, which is the property that
#: matters. Every return value here is a plain `int`, `float` or `dict` — that
#: is deliberate, because it means each one *is* recordable and the replay gate
#: is the only thing standing between it and a generated test.
DRIFT = '''\
"""Ways to be non-deterministic: one per source issue #13 names, plus drift."""

import pathlib
import random
import socket
import time

_COUNT = pathlib.Path(__file__).with_name("flip.count")


def stamp():
    return time.time_ns()


def roll():
    return random.random()


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def ordering():
    """A set of strings, whose `repr` order is decided by the hash seed.

    Genuinely per-process: `PYTHONHASHSEED` is not set in the sandbox
    environment, deliberately, so every run orders this differently. Ten
    elements makes two runs agreeing by chance vanishingly unlikely — measured
    at twelve independent interpreters, twelve distinct orders.
    """
    return {"k1", "k2", "k3", "k4", "k5", "k6", "k7", "k8", "k9", "k10"}


def flip():
    """State on disk that advances every time it is read.

    The only one here that *alternates* rather than simply never reproducing,
    which is what makes it the case that exercises the "varied across replays"
    branch instead of the "did not reproduce" one.
    """
    count = int(_COUNT.read_text()) if _COUNT.exists() else 0
    _COUNT.write_text(str(count + 1))
    return count % 2 == 0
'''

DRIFT_SUITE = """\
from sample.drift import flip, ordering, port, roll, stamp


def test_drift():
    assert stamp() > 0
    assert 0.0 <= roll() <= 1.0
    assert port() > 0
    assert len(ordering()) == 10
    assert flip() in (True, False)
"""


#: Calls at the edge of what is recordable at all — the guards in `_harness`
#: that the replay gate does *not* back up, and which would therefore be
#: invisible without tests aimed straight at them.
EDGES = '''\
"""Three calls: one refused, one refused, one ordinary."""


def push(items, value):
    """Rewrites what it was handed, so an oracle would re-run the side effect."""
    items.append(value)
    return len(items)


def explode(value):
    """Raises out of the frame, where the `return` event still fires with None."""
    raise ValueError(value)


def parse(text):
    """Raises *inside* the frame and handles it — an ordinary recordable call."""
    try:
        return int(text)
    except ValueError:
        return 0
'''

EDGES_SUITE = """\
import pytest

from sample.edges import explode, parse, push


def test_push():
    assert push([1, 2], 3) == 3


def test_explode():
    with pytest.raises(ValueError):
        explode(7)


def test_parse():
    assert parse("12") == 12
    assert parse("nope") == 0
"""


def counting(name: str, fails_on: str) -> str:
    """A test that counts its own runs on disk, and fails on the ones named.

    The counter is what makes "non-identical across N runs" deterministic rather
    than likely. `fails_on` is a Python expression over `n`, the zero-based
    index of this run.
    """
    return (
        "import pathlib\n\n"
        f"COUNTER = pathlib.Path(__file__).with_name({name + '.count'!r})\n\n\n"
        "def _tick():\n"
        "    n = int(COUNTER.read_text()) if COUNTER.exists() else 0\n"
        "    COUNTER.write_text(str(n + 1))\n"
        "    return n\n\n\n"
        f"def test_{name}():\n"
        "    n = _tick()\n"
        f"    assert not ({fails_on}), f'run {{n}}'\n"
    )


def repo(make_repo, **extra: str):
    return make_repo(
        {
            "pyproject.toml": PROJECT,
            "sample/__init__.py": "",
            "sample/maths.py": MATHS,
            "tests/test_maths.py": MATHS_SUITE,
            **extra,
        }
    )


@pytest.fixture
def characterized(make_repo, sandbox_venv):
    def _characterize(policy=FAST, **extra: str):
        root = repo(make_repo, **extra)
        return root, characterize(root, venv=sandbox_venv, policy=policy)

    return _characterize


def named(items, symbol: str):
    return next((item for item in items if item.symbol == symbol), None)


def symbol(coverage, name: str):
    return coverage.index[name]


# --- real execution becomes real tests ----------------------------------------


def test_a_traced_call_becomes_a_generated_test(characterized):
    """Criterion 1: traced from real execution, and the claim is legible.

    The assertion is a `repr` comparison rather than `==`, which is what makes
    dict and set ordering a determinism signal instead of something a tolerant
    equality would forgive.
    """
    _, found = characterized()

    oracle = named(found.oracles, "sample.maths.add")
    assert oracle is not None
    assert oracle.call == "sample.maths.add(1, 2)"
    assert oracle.expected == "3"

    assert named(found.oracles, "sample.maths.scale").call == (
        "sample.maths.scale([1, 2], 2)"
    )
    # A keyword-only argument is replayed as a keyword, or the call would not
    # be the call that was observed.
    assert named(found.oracles, "sample.maths.label").call == (
        "sample.maths.label('polish', upper=True)"
    )


def test_the_baseline_really_runs_three_times_before_anything_else(characterized):
    """Criterion 7, and the order it insists on: N runs *before* any edit.

    Pinned through the command log rather than by counting subprocesses,
    because the command log is also what the user reads. Note this test uses the
    default policy — the whole point of #5's decision is the number 3, and a
    test that shrank it would be testing something else.
    """
    _, found = characterized(policy=Policy())

    suite = "python -P -c import _polish_harness as _h; _h.suite()"
    trace = "python -P -c import _polish_harness as _h; _h.trace()"
    oracles = "python -P -c import _polish_harness as _h; _h.oracles()"

    assert found.commands[:3] == (suite, suite, suite)
    assert found.commands[3] == trace
    assert found.commands[4:] == (oracles, oracles, oracles)


# --- coverage is per symbol, and that is what tiers an edit -------------------


def test_coverage_is_reported_per_symbol_not_per_repository(characterized):
    """Criterion 2. A symbol nothing called has to be visible as uncovered."""
    _, found = characterized()

    assert symbol(found.coverage, "sample.maths.add").executed
    assert symbol(found.coverage, "sample.maths.add").oracles == 1

    never = symbol(found.coverage, "sample.maths.never_called")
    assert not never.executed
    assert never.oracles == 0
    assert never.tier is Tier.ZERO

    assert set(found.coverage.paths) == {"sample/maths.py"}


def test_an_edits_tier_is_the_lowest_among_the_symbols_it_touches(characterized):
    """Question #4's rule, which is the reason coverage is shaped this way."""
    _, found = characterized()
    coverage = found.coverage

    assert coverage.tier_for(["sample.maths.add"]) is Tier.ONE
    assert coverage.tier_for(["sample.maths.add", "sample.maths.never_called"]) is (
        Tier.ZERO
    )
    # One uncharacterised symbol in the file is enough to hold the whole file
    # down, which is what makes the rule safe for a per-file `Change`.
    assert coverage.tier_for_path("sample/maths.py") is Tier.ZERO


def test_a_symbol_polish_has_never_heard_of_is_tier_zero(characterized):
    """Downward resolution, unchanged: no record is not the same as no problem."""
    _, found = characterized()

    assert found.coverage.tier_of("sample.maths.invented") is Tier.ZERO
    assert found.coverage.tier_for([]) is Tier.ZERO
    assert found.coverage.tier_for_path("sample/nothing.py") is Tier.ZERO


# --- the determinism gate ------------------------------------------------------


def test_a_nondeterministic_oracle_is_discarded_and_leaves_no_oracle(characterized):
    """Criteria 3, 6 and 8, over time, randomness, network and iteration order.

    Each of these four functions returns a value Polish *can* record — an int, a
    float, a dict of ints — so the recording succeeds and the replay gate is the
    only thing that stops the oracle being emitted. If the gate is removed, four
    oracles appear here rather than none.
    """
    _, found = characterized(
        **{"sample/drift.py": DRIFT, "tests/test_drift.py": DRIFT_SUITE}
    )

    for name in ("stamp", "roll", "port", "ordering", "flip"):
        full = f"sample.drift.{name}"
        assert named(found.oracles, full) is None, f"{full} should not have an oracle"
        assert named(found.discarded, full) is not None, f"{full} was never recorded"

        # "the code it covered is reported as having no oracle" — executed, and
        # still Tier 0, which is what makes it ineligible for Tier 1 edits.
        covered = symbol(found.coverage, full)
        assert covered.executed
        assert covered.oracles == 0
        assert covered.tier is Tier.ZERO

    # The two shapes a discard can take, kept apart because they mean different
    # things: `flip` alternates and is visibly non-deterministic, while a clock
    # simply never reproduces and Polish cannot tell that from a bad recording.
    assert "varied across" in named(found.discarded, "sample.drift.flip").reason
    assert "did not reproduce" in named(found.discarded, "sample.drift.stamp").reason

    # The deterministic repository in the same run is unaffected: the gate
    # discards what varies, not everything.
    assert named(found.oracles, "sample.maths.add") is not None


def test_the_surviving_oracles_are_the_only_files_left_on_disk(characterized):
    """A discarded oracle that stayed on disk would be discarded in name only."""
    _, found = characterized(
        **{"sample/drift.py": DRIFT, "tests/test_drift.py": DRIFT_SUITE}
    )

    written = sorted(
        path.name for path in (found.workspace / "oracles").glob("test_*.py")
    )
    assert written == ["test_polish_sample_maths.py"]

    body = (found.workspace / "oracles" / "test_polish_sample_maths.py").read_text()
    assert "sample.drift" not in body
    assert body.count("def test_") == len(found.oracles)


def test_the_calls_the_recorder_refuses_are_never_recorded_at_all(characterized):
    """The two guards the replay gate does not back up.

    Both of these would *survive* replay if they were recorded — `push` because
    the arguments are captured at call time, so a replay gets exactly the inputs
    the recording had, and `explode` because a test asserting `repr(...) ==
    'None'` on a call that raises fails identically every time and would simply
    be discarded. So neither guard shows up as a discarded oracle, and neither
    is observable anywhere except here. An unreachable guard with no test is the
    kind that gets deleted by someone who did not know what it was for.
    """
    _, found = characterized(
        **{"sample/edges.py": EDGES, "tests/test_edges.py": EDGES_SUITE}
    )

    for name in ("push", "explode"):
        full = f"sample.edges.{name}"
        assert symbol(found.coverage, full).executed, f"{full} did not run"
        assert named(found.oracles, full) is None, f"{full} must not be recorded"
        assert named(found.discarded, full) is None, f"{full} must not be recorded"

    # And the narrowness of the exception rule, in the other direction: a call
    # that raised and handled it is ordinary. `try/except` is too common in real
    # Python for the guard to be a blanket one.
    assert named(found.oracles, "sample.edges.parse") is not None


# --- the quarantine ------------------------------------------------------------


def test_a_flaky_test_is_quarantined_and_named_in_the_run_log(characterized):
    """Criteria 3 and 4, and the ticket's insistence on *named*, not counted."""
    _, found = characterized(
        **{"tests/test_flaky.py": counting("flaky", "n != 0")},
    )

    quarantined = [item.nodeid for item in found.baseline.quarantined]
    assert quarantined == ["tests/test_flaky.py::test_flaky"]
    assert found.baseline.quarantined[0].outcomes == ("passed", "failed")

    assert "tests/test_flaky.py::test_flaky" not in found.baseline.trusted
    assert "tests/test_maths.py::test_add" in found.baseline.trusted

    assert any(
        "~ tests/test_flaky.py::test_flaky (passed / failed)" in line
        for line in found.summary()
    ), found.summary()


def test_a_quarantined_test_cannot_drop_a_change(characterized, sandbox_venv):
    """Criterion 5, which is the one that has to survive a hostile reading.

    The flaky test here passes on its first run and fails on every run after, so
    by the time verification asks, it is failing — and its baseline outcome is
    `passed`, so it *would* be read as a regression. `Baseline.trusts` is the
    only thing between it and dropping the change, which is exactly the property
    worth pinning.
    """
    root, found = characterized(
        **{"tests/test_flaky.py": counting("flaky", "n != 0")},
    )

    checked = verify(root, found, venv=sandbox_venv, policy=FAST)

    assert checked.ignored == ("tests/test_flaky.py::test_flaky",)
    assert checked.failed == ()
    assert checked.passed
    assert any("cannot drop a change" in line for line in checked.evidence())


def test_a_failure_that_passes_on_retry_is_not_believed(characterized, sandbox_venv):
    """The M=2 backstop, over whole-suite re-runs.

    The arithmetic is pinned on purpose: with `FAST`, the suite runs twice for
    the baseline and once for the trace, so run index 3 is verification's first
    attempt. Failing exactly there and passing after is the flake the 2x
    baseline missed — the case retry exists for.
    """
    root, found = characterized(
        **{"tests/test_late.py": counting("late", "n == 3")},
    )

    assert found.baseline.quarantined == ()

    checked = verify(root, found, venv=sandbox_venv, policy=FAST)

    assert checked.flaky == ("tests/test_late.py::test_late",)
    assert checked.failed == ()
    assert checked.attempts == 2
    assert checked.passed


# --- verification actually verifies -------------------------------------------


def test_an_unedited_tree_passes_its_own_oracles(characterized, sandbox_venv):
    root, found = characterized()

    checked = verify(root, found, venv=sandbox_venv, policy=FAST)

    assert checked.passed
    assert checked.oracle_failures == ()
    assert checked.attempts == 1


def test_a_broken_refactor_fails_the_oracles(characterized, sandbox_venv):
    """The claim the product sells, checked: recorded behaviour catches a change.

    `add` still passes the repository's own suite after this edit, because the
    suite only asserts `add(1, 2) == 3`... which is also what the oracle asserts.
    So the edit is made to `scale`, which the suite covers and the oracle covers
    differently — proving the oracle is doing work the suite is not.
    """
    root, found = characterized()
    source = root / "sample" / "maths.py"
    source.write_text(
        source.read_text().replace("value * factor", "value * factor + 0")
    )

    unchanged = verify(root, found, venv=sandbox_venv, policy=FAST)
    assert unchanged.passed, "an equivalent edit must not fail"

    source.write_text(source.read_text().replace("value * factor + 0", "value * 99"))
    broken = verify(root, found, venv=sandbox_venv, policy=FAST)

    assert not broken.passed
    assert broken.oracle_failures
    assert any("FAILED" in line for line in broken.evidence())


def test_a_suite_that_stops_being_collected_is_a_regression_not_a_pass(
    characterized, sandbox_venv
):
    """An absent test is a broken suite, not a quiet one.

    This is the shape a broken `conftest.py` takes: the tests do not fail, they
    stop existing. Read as "did not fail", every change would ship against a
    suite that no longer runs — and the oracles would still pass, because they
    are collected from Polish's own directory with Polish's own config.
    """
    root, found = characterized()
    (root / "tests" / "conftest.py").write_text("raise RuntimeError('broken')\n")

    checked = verify(root, found, venv=sandbox_venv, policy=FAST)

    assert not checked.passed
    assert "tests/test_maths.py::test_add" in checked.failed
    # Believed only after the retries, which is the backstop doing its job on a
    # failure that is real rather than flaky.
    assert checked.attempts == 3


# --- refusing, rather than guessing --------------------------------------------


def test_a_suite_that_collects_nothing_declines_rather_than_trusting_nothing(
    make_repo, sandbox_venv
):
    """An empty baseline is not a clean one.

    Without this, a repository whose suite stopped collecting would produce a
    baseline of zero tests, and every later verification would pass by having
    nothing to check — a green light with no evidence behind it.
    """
    root = make_repo(
        {
            "pyproject.toml": PROJECT,
            "sample/__init__.py": "",
            "sample/maths.py": MATHS,
            "tests/conftest.py": "raise RuntimeError('collection is broken')\n",
            "tests/test_maths.py": MATHS_SUITE,
        }
    )

    found = characterize(root, venv=sandbox_venv, policy=FAST)

    assert not found.baseline.usable
    assert "no tests" in found.baseline.declined
    assert found.oracles == ()
    assert found.coverage.tier_for(["sample.maths.add"]) is Tier.ZERO

    # And a characterization that declined cannot be verified against.
    checked = verify(root, found, venv=sandbox_venv, policy=FAST)
    assert not checked.passed
    assert checked.declined


def test_an_exhausted_budget_keeps_the_baseline_it_already_paid_for(characterized):
    """Downward resolution, without throwing away the quarantine list.

    A budget that expires during tracing has still learned which of the user's
    tests are flaky, and that is information the developer wants whether or not
    the rest of the night happened.
    """
    _, found = characterized(
        policy=Policy(baseline_runs=2, replays=2, total_seconds=0.0),
    )

    assert found.declined
    assert found.oracles == ()
    assert found.coverage.symbols == ()


# --- N and M are configurable ---------------------------------------------------


def test_n_and_m_come_out_of_polish_toml(characterized):
    """Criterion 10. Configurable, and read from the repository being run."""
    _, found = characterized(
        policy=None,
        **{"polish.toml": "[characterize]\nbaseline_runs = 2\nreplays = 2\n"},
    )

    assert found.baseline.runs == 2
    assert len(found.commands) == 2 + 1 + 2


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        ("[characterize]\nbaseline_runs = 1\n", "at least 2"),
        ("[characterize]\nreplays = 1\n", "at least 2"),
        ("[characterize]\nretries = -1\n", "negative"),
        ("[characterize]\nbaseline_run = 3\n", "does not have baseline_run"),
    ],
)
def test_a_policy_that_would_disable_the_gate_is_refused_loudly(
    make_repo, section, expected
):
    """A misconfiguration that silently defaulted would be a lie about what ran.

    `baseline_runs = 1` in particular is the dangerous one: it quarantines
    nothing, and a log saying "0 quarantined" would be indistinguishable from a
    suite with no flakes in it.
    """
    root = make_repo({"polish.toml": section})

    with pytest.raises(Misconfigured, match=expected):
        Policy.load(root)


# --- the run log ----------------------------------------------------------------


def test_a_night_shift_run_characterizes_a_tier_one_repository(
    make_git_repo, monkeypatch
):
    """The whole pipeline, end to end: `polish run` reaches the harness.

    Slow, and worth it — the seams between the probe, the sandbox and the
    harness are exactly where "each part works" and "the night works" diverge.
    """
    monkeypatch.setenv("POLISH_TEST", "1")
    root = make_git_repo(
        {
            "pyproject.toml": PROJECT,
            "sample/__init__.py": "",
            "sample/maths.py": MATHS,
            "tests/test_maths.py": MATHS_SUITE,
            "polish.toml": "[characterize]\nbaseline_runs = 2\nreplays = 2\n",
        }
    )

    record = envelope.run(root)

    assert record.tier is not None and record.tier.tier_one
    assert record.characterization is not None
    assert record.characterization.baseline.runs == 2
    assert record.characterization.oracles

    summary = "\n".join(record.summary())
    assert "baseline  2x suite" in summary
    assert "coverage  " in summary


def test_a_tier_zero_repository_is_not_characterized_at_all(make_git_repo):
    """ "Nothing to characterize" and "characterized nothing" are different facts."""
    record = envelope.run(
        make_git_repo({"pyproject.toml": PROJECT, "sample/__init__.py": ""})
    )

    assert record.tier is not None and not record.tier.tier_one
    assert record.characterization is None
