"""The characterization harness — what the code really does, and what may be believed.

Two public entry points, one for each half of issue #13:

    characterize(worktree, venv=...)     trace, generate, replay, measure
    verify(worktree, characterization)   check an edited tree against all of it

SPEC §3.1 defines Tier 1 as "characterization tests from real execution traces",
and SPEC §4.5 makes it an acceptance criterion that every shipped change passes
them. This package is where that claim is either earned or refused, and most of
the code here is about refusing.

`characterize()` does four things, in an order that is not arbitrary:

1. **Baseline the repository's own suite, N times, before anything is edited.**
   A test whose result is not identical every time is quarantined and named in
   the run log. It runs first because it is the only measurement that can
   distinguish a pre-existing flake from one Polish caused.
2. **Trace the suite** with `sys.settrace`, recording real calls and returns —
   real execution, not code Polish invented to exercise it.
3. **Generate oracles from those recordings and replay each one R times against
   unmodified code.** Any variance and the test is *discarded, never emitted*.
4. **Report coverage per symbol**, which question #4 made the input to tiering:
   an edit's tier is the lowest coverage tier among the symbols it touches.

The asymmetry between steps 1 and 3 is issue #5's decision and worth restating,
because it looks like an inconsistency until you see which way each error runs.
A flaky test of the user's **drops good work**; it is quarantined, because you
cannot delete someone else's test. A flaky oracle **can pass a broken change**;
it is deleted, because Polish wrote it and a non-deterministic oracle should not
exist. The second error is the one the product cannot survive.

What this package does *not* do is pin anything. Freezing clocks, seeding RNGs
and stubbing the network would salvage more oracles, and every pin would assert
something about behaviour that Polish has not observed — a wrong pin produces a
confident oracle for behaviour that never happens, which is strictly worse than
no oracle because it looks like verification.

**Both execution sources, not just the suite (#32).** The probe promotes on two
things — gate 2's suite and gate 4's opted-in entry point — and this package
traces whichever of them the repository has, in that order. The entry point is
not a second harness: same recorder, same sample format, same generated oracles,
same replay gate. What differs is only what got run, and one consequence of it.
An entry point runs **once**, so there is no flake baseline to take and no
quarantine step at all, and the R replays of the generated oracles become the
whole determinism gate — issue #5's stricter rule, standing on its own. The run
log therefore reports `baseline none`, never the far more comfortable
"0 quarantined", which would read exactly like a suite measured and found clean.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from polish.firstparty import scan
from polish.probe import ENTRYPOINT, TRACE, NotAllowed, Runner, test_files

from . import _baseline, _coverage, _oracles, _session
from ._result import (
    FROM_ENTRYPOINT,
    FROM_SUITE,
    Baseline,
    Characterization,
    Coverage,
    Discarded,
    Misconfigured,
    Oracle,
    Policy,
    Quarantined,
    SymbolCoverage,
    Verification,
)
from ._session import Exhausted, Session

#: Where the harness, the generated oracles and the JSON going each way live:
#: beside the worktree, never inside it — the same rule, and the same reason, as
#: Tier 0's type-check cache.
SCRATCH_DIR = "characterization"

__all__ = [
    "FROM_ENTRYPOINT",
    "FROM_SUITE",
    "SCRATCH_DIR",
    "Baseline",
    "Characterization",
    "Coverage",
    "Discarded",
    "Exhausted",
    "Misconfigured",
    "Oracle",
    "Policy",
    "Quarantined",
    "SymbolCoverage",
    "Verification",
    "characterize",
    "verify",
]


def characterize(
    root: Path,
    *,
    venv: Path,
    first_party: Iterable[str] | None = None,
    scratch: Path | None = None,
    policy: Policy | None = None,
    runner: Runner | None = None,
) -> Characterization:
    """Characterize the repository at `root`, using the virtualenv at `venv`.

    `root` is the sandbox worktree, not the user's tree: this runs the
    repository's suite four or more times over and writes generated tests, and
    neither is a thing to do to a working tree Polish promised not to touch.

    Nothing raises out of here except a `polish.toml` that cannot be honoured.
    An unattended run reads this out of a log at 3am, so a repository that
    surprises the harness has to become a declined characterization rather than
    a traceback — and a declined characterization leaves every symbol at Tier 0,
    which is the same downward resolution the probe applies.

    `runner` replaces the subprocess layer, on the same terms as
    `polish.probe.probe(runner=...)`: public because a seam only tests can reach
    is a seam nobody can inspect, and harmless because the allowlist is enforced
    when a command is constructed rather than when it is dispatched.
    """
    root = Path(root)
    files = tuple(first_party) if first_party is not None else tuple(scan(root).files)
    policy = policy or Policy.load(root)

    session = _session.open_session(
        root,
        venv=venv,
        first_party=files,
        scratch=Path(scratch) if scratch else root.parent / SCRATCH_DIR,
        policy=policy,
        runner=runner,
    )
    try:
        return _perform(root, files, session)
    except Exhausted as spent:
        return _declined(session, Baseline(runs=0), str(spent))
    except NotAllowed as refused:
        return _declined(
            session, Baseline(runs=0), f"a command outside the allowlist: {refused}"
        )
    except OSError as failure:
        return _declined(
            session, Baseline(runs=0), f"the harness could not run: {failure}"
        )


def verify(
    root: Path,
    characterization: Characterization,
    *,
    venv: Path,
    first_party: Iterable[str] | None = None,
    policy: Policy | None = None,
    runner: Runner | None = None,
) -> Verification:
    """Check the tree at `root` against everything `characterize()` established.

    Runs the surviving oracles and the trusted part of the repository's suite,
    retrying a regression over M more whole-suite runs before believing it. The
    two rules issue #13 asks to be checkable are both enforced in one place:
    **a quarantined test cannot appear in `failed`**, and **a failure that
    passes on retry is not believed**.

    Declines rather than passing when it cannot run. `Verification.passed` is
    false for a declined check, so a change whose verification could not happen
    is a change that does not ship — SPEC §1.1, unchanged.

    What gets checked follows the execution source (#32). A suite repository is
    checked against both halves: the surviving oracles, and the trusted suite
    with M retries behind it. An entry-point repository has no suite, so it is
    checked against the oracles alone — and, because that is then the only check
    there is, a characterization that produced no oracle is refused here rather
    than passed for want of anything to disagree with.
    """
    source = characterization.source
    refused = _cannot_verify(characterization)
    if refused:
        return Verification(attempts=0, source=source, declined=refused)

    root = Path(root)
    files = tuple(first_party) if first_party is not None else tuple(scan(root).files)
    session = _session.open_session(
        root,
        venv=venv,
        first_party=files,
        scratch=characterization.workspace,
        policy=policy or Policy.load(root),
        runner=runner,
    )
    try:
        if source == FROM_ENTRYPOINT:
            return _baseline.verify_oracles(session)
        return _baseline.verify(
            session,
            characterization.baseline,
            oracles=bool(characterization.oracles),
        )
    except Exhausted as spent:
        return Verification(attempts=0, source=source, declined=str(spent))
    except NotAllowed as refused:
        return Verification(attempts=0, source=source, declined=f"refused: {refused}")
    except OSError as failure:
        return Verification(attempts=0, source=source, declined=str(failure))


def _cannot_verify(characterization: Characterization) -> str:
    """Why this characterization cannot be verified against, or `""`.

    Asked before a session is opened, because every answer here is a refusal and
    refusing is cheaper than preparing to. The two sources fail differently and
    for the same underlying reason — there has to be *something* trustworthy to
    check against, and for a suite repository that is the baseline while for an
    entry-point one it is the oracles.
    """
    if characterization.workspace is None:
        return characterization.declined or "nothing was characterized"
    if characterization.source == FROM_ENTRYPOINT:
        if not characterization.oracles:
            return (
                characterization.declined
                or "the entry point produced no surviving oracle, and it is the "
                "only execution source this repository has"
            )
        return ""
    if not characterization.baseline.usable:
        return (
            characterization.declined
            or characterization.baseline.declined
            or "nothing was characterized"
        )
    return ""


def _perform(root: Path, files: Sequence[str], session: Session) -> Characterization:
    """Characterize from whichever execution source the repository actually has.

    Gate 2's suite first, gate 4's entry point only when the suite did not
    supply one — the probe's own order, and the probe's own rule: an entry point
    is "not run when gate 2 already supplied a source", because running a user's
    entry point is the riskiest thing Polish does and a second execution source
    buys nothing that the first one did not already give.

    The suite is skipped outright when there are no test files, rather than run
    N times to discover that pytest collects nothing. That is not only a saving:
    it is what makes `Baseline.runs` zero for an entry-point repository, and a
    baseline of zero runs is the one that must report *none* rather than "0
    quarantined" (#32).
    """
    baseline = (
        _baseline.establish(session)
        if test_files(files)
        else Baseline(runs=0, declined=_WHY_NO_SUITE)
    )
    try:
        if baseline.usable:
            return _characterized(root, files, session, baseline)
        if session.entrypoint:
            # Either there was no suite at all, or there was one and it cannot
            # be believed — which is gate 2 supplying no execution source, in
            # both spellings. Gate 4's is then the one that earned Tier 1, so it
            # is the one to trace. The unusable baseline is carried along rather
            # than discarded: "the suite could not be trusted" is a fact about
            # the repository the developer wants either way.
            #
            # Before the entry point runs, not after: if the oracles cannot be
            # replayed there is nothing to be gained by executing a user's entry
            # point, and executing one is the riskiest thing Polish does.
            missing = session.ensure_pytest()
            if missing:
                return _declined(session, baseline, missing, FROM_ENTRYPOINT)
            return _characterized(
                root,
                files,
                session,
                baseline,
                task=ENTRYPOINT,
                seconds=session.policy.entrypoint_seconds,
                source=FROM_ENTRYPOINT,
            )
    except Exhausted as spent:
        # The baseline survives the budget running out. Its quarantine list is
        # information the developer wants whether or not the rest happened.
        return _declined(session, baseline, str(spent))

    # No trustworthy suite and no entry point, so no trustworthy anything.
    # Tracing a suite whose results Polish cannot compare would produce
    # recordings it has no grounds to believe — the vacuous test SPEC §3.1
    # refuses — and there is no other source to fall back to.
    return _declined(session, baseline, baseline.declined)


def _characterized(
    root: Path,
    files: Sequence[str],
    session: Session,
    baseline: Baseline,
    *,
    task: str = TRACE,
    seconds: float | None = None,
    source: str = FROM_SUITE,
) -> Characterization:
    """Trace one execution source, then generate and replay from what it saw.

    One function for both sources on purpose. Everything after the traced run —
    the sample format, `_oracles.emit`, the replay gate, `_coverage.measure` —
    is identical, and #32 is an execution source rather than a second harness.
    A parallel copy for the entry point would be the first place the two could
    quietly diverge on which recordings are believed.
    """
    traced = session.run(task, seconds or session.policy.trace_seconds)
    if not traced.ok:
        return _declined(
            session, baseline, f"the traced {task} failed — {traced.error}", source
        )

    emitted = _oracles.emit(session, traced.payload.get("samples", []))
    return Characterization(
        baseline=baseline,
        coverage=_coverage.measure(
            root,
            files,
            executed=traced.payload.get("executed", []),
            oracles=emitted.oracles,
        ),
        oracles=emitted.oracles,
        discarded=emitted.discarded,
        workspace=session.scratch,
        declined=emitted.declined,
        commands=tuple(session.commands),
        source=source,
    )


def _declined(
    session: Session, baseline: Baseline, reason: str, source: str = FROM_SUITE
) -> Characterization:
    """Nothing was generated — but the workspace is still handed back.

    A run whose trace failed after a clean baseline has no oracles and every
    symbol at Tier 0, and it *still* knows which of the user's tests are flaky.
    Dropping the workspace here would throw that away and leave verification
    with nothing at all, when what it actually has is the weaker of the two
    checks. Half a verification is worth reporting; claiming a whole one is not.
    """
    return Characterization(
        baseline=baseline,
        workspace=session.scratch,
        declined=reason,
        commands=tuple(session.commands),
        source=source,
    )


_WHY_NO_SUITE = (
    "no test_*.py or *_test.py among the first-party files, so there is no "
    "suite to run N times and nothing to quarantine"
)
