"""The flake policy for the repository's own suite (issue #5, decided).

Two asymmetric halves, and the asymmetry is the decision:

    before the run   N runs of the suite; any test whose result is not
                     identical every time is quarantined and named in the log
    during the run   a regression is retried over M more whole-suite runs
                     before it is believed

The baseline is authoritative because it happens before any edit exists to bias
it: it is the only thing that can tell a pre-existing flake from one Polish just
caused. Retry is the backstop for the rarer flake that cleared N runs — cheap
insurance against silently dropping good work, and useless on its own, because
"passed once, keep the change" eventually keeps a change that intermittently
breaks something.

Quarantine is used here and *not* for Polish's generated oracles, which are
discarded instead. The difference is ownership: you cannot delete a user's test,
and you should not create a non-deterministic one of your own.
"""

from __future__ import annotations

from collections.abc import Sequence

from polish.probe import ORACLES, SUITE

from ._result import ABSENT, SEVERITY, Baseline, Quarantined, Verification
from ._session import Outcome, Session


def establish(session: Session) -> Baseline:
    """Run the suite N times and decide which tests may be believed."""
    policy = session.policy
    runs = session.repeat(SUITE, policy.suite_seconds, policy.baseline_runs)

    refused = [run.error for run in runs if not run.ok]
    if refused:
        # One unusable run out of N makes the whole comparison unusable: what
        # the remaining runs agreed about is not evidence that the suite is
        # stable, and reporting it as though it were is the failure mode this
        # entire module exists to prevent.
        return Baseline(runs=len(runs), declined=refused[0])

    if not any(run.tests for run in runs):
        # A suite that collected nothing agrees with itself perfectly, which is
        # the most dangerous shape an empty baseline can take: every later
        # verification would pass by having nothing to check. Gate 2 should have
        # caught this — it demands more than zero passing tests — but a
        # `conftest` that raises after the probe ran would arrive here, and
        # "green because there was nothing to be red" must not ship a change.
        return Baseline(
            runs=len(runs),
            declined=(
                f"the suite reported no tests across {len(runs)} runs; "
                f"{_collection(runs)}"
            ),
        )

    return _compare(len(runs), [run.tests for run in runs])


def _collection(runs: Sequence[Outcome]) -> str:
    errors = sorted({error for run in runs for error in run.payload.get("errors", [])})
    if not errors:
        return "nothing was collected"
    return f"collection failed in {', '.join(errors[:3])}"


def verify(session: Session, baseline: Baseline, *, oracles: bool) -> Verification:
    """Check an edited tree against the oracles and the trusted suite.

    The oracles run once and the suite runs up to `1 + M` times, and those are
    different rules on purpose. An oracle already survived N replays against
    unmodified code, so a failing oracle is the signal — retrying it would be
    retrying the one measurement that was built to be trustworthy. The user's
    suite got no such treatment beyond its baseline, which is what M is for.
    """
    if not baseline.usable:
        return Verification(attempts=0, declined=baseline.declined)

    failures = _oracle_failures(session) if oracles else ()
    if isinstance(failures, str):
        return Verification(attempts=0, declined=failures)

    attempts: list[dict[str, str]] = []
    for _ in range(1 + session.policy.retries):
        run = session.run(SUITE, session.policy.suite_seconds)
        if not run.ok:
            return Verification(attempts=len(attempts), declined=run.error)
        attempts.append(run.tests)
        if not _regressions(baseline, attempts[-1]):
            # Nothing to retry. The backstop is for failures, and spending two
            # more full suite runs on a green one is how a night ends unfinished.
            break

    believed = set.intersection(*(_regressions(baseline, run) for run in attempts))
    seen = set().union(*(_regressions(baseline, run) for run in attempts))

    return Verification(
        attempts=len(attempts),
        failed=tuple(sorted(believed)),
        flaky=tuple(sorted(seen - believed)),
        ignored=tuple(sorted(_ignored(baseline, attempts[0]))),
        oracle_failures=failures,
    )


def _oracle_failures(session: Session) -> tuple[str, ...] | str:
    if not any(session.oracle_dir.glob("test_*.py")):
        return ()
    run = session.run(ORACLES, session.policy.oracle_seconds)
    if not run.ok:
        return run.error
    return tuple(
        sorted(nodeid for nodeid, outcome in run.tests.items() if outcome != "passed")
    )


def _regressions(baseline: Baseline, tests: dict[str, str]) -> set[str]:
    """Trusted tests that got worse than the baseline said they were.

    Every id the baseline knows is asked about, not only the ones this run
    reported: a test that stopped being collected is absent rather than passing,
    and reading an absence as a pass is how a broken `conftest` ships.
    """
    return {
        nodeid
        for nodeid in baseline.expected
        if baseline.regressed(nodeid, tests.get(nodeid, ABSENT))
    }


def _ignored(baseline: Baseline, tests: dict[str, str]) -> set[str]:
    """Quarantined tests that failed — reported, and acted on by nobody."""
    return {
        nodeid
        for nodeid in baseline.quarantine
        if tests.get(nodeid, ABSENT) in ("failed", ABSENT)
    }


def _compare(runs: int, results: Sequence[dict[str, str]]) -> Baseline:
    """Reduce N runs to what may be trusted, and what may not.

    "Non-identical" is over the union of the test ids, not the intersection: a
    test that appeared in two runs out of three is exactly as untrustworthy as
    one that changed outcome, and taking the intersection would have made it
    invisible instead.
    """
    every = sorted({nodeid for result in results for nodeid in result})
    observed: list[tuple[str, str]] = []
    quarantined: list[Quarantined] = []

    for nodeid in every:
        outcomes = tuple(result.get(nodeid, ABSENT) for result in results)
        best = min(outcomes, key=lambda item: SEVERITY.get(item, SEVERITY[ABSENT]))
        observed.append((nodeid, best))
        if len(set(outcomes)) > 1:
            quarantined.append(Quarantined(nodeid=nodeid, outcomes=outcomes))

    return Baseline(runs=runs, observed=tuple(observed), quarantined=tuple(quarantined))
