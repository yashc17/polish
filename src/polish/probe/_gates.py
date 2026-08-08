"""The four gates, evaluated in order (issue #3's decision).

Hard filters, no scores, no thresholds. A scored combination was considered and
rejected: `venv builds (30) + imports clean (20) + entry point found (10) = 60`
clears a 60 bar with no test suite at all, and a repository that averages its
way into Tier 1 has an unearned trust claim attached to it. Hard gates make that
arithmetic impossible.

    1  isolated venv builds from declared deps     fail -> Tier 0, repository-wide
    2  test suite discovered and runs, >0 passing  pass -> Tier 1 candidate
    3  import smoke over first-party modules       fail -> Tier 0 per module
    4  opted-in entry point                        pass -> Tier 1 candidate

Gates 1, 2 and 4 answer about the repository, because what they establish is
repository-wide: a sandbox either builds or does not, and an execution source
(SPEC §3.1) either exists or does not. What they produce is therefore a
**ceiling** — the best tier anything here can reach. **Gate 3 subtracts from
that ceiling one module at a time** (issue #28, question #4): a module that does
not import is held at Tier 0 and everything that does import keeps the ceiling.
It used to demote the whole repository, which is how `flask` — 485 passing
tests, every `flask.*` module importing — was pinned to Tier 0 by eight example
apps under `examples/` that want celery.

**Gate 3 is demote-only, and that is the load-bearing part.** SPEC §3.1 defines
Tier 1 as characterization tests from real execution traces. Importing a module
traces only import-time code — constants, definitions, decorators. It never
enters a function body or a branch. A repository promoted on import smoke alone
would arrive at Tier 1 with nothing to characterize, producing exactly the
vacuous test §3.1 calls "actively corrosive to a trust-first product": it passes
before and after because the code it covers was never executed either time.

So Tier 1 needs an *execution source* — gate 2's suite, or gate 4's opt-in — and
gate 3 exists only to take one away again. Anyone tempted to let a clean import
smoke promote a repository is about to reintroduce the bug the whole design is
shaped around.

Gate 3 sits between the two promotion sources rather than after them because it
guards both: a repository whose entry point runs but whose modules do not import
is no more characterizable than one whose suite passes and whose modules do not.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic

from . import _commands, _discovery
from ._commands import Command, Completed, NotAllowed
from ._verdict import (
    FAIL,
    PASS,
    SKIP,
    Budget,
    Demotion,
    GateOutcome,
    Tier,
    TierVerdict,
)

Runner = Callable[[Command], Completed]

#: Modules per `python -c` in gate 3. Small enough that attributing a failure
#: costs little, large enough that a big repository is not all process startup.
BATCH = 25

#: `1 failed, 128 passed in 4.21s` — pytest's own summary, which is the only
#: place a pass *count* appears. The exit code cannot distinguish "ran and
#: everything passed" from "collected nothing", and that difference is the gate.
PASSED = re.compile(r"(\d+) passed")

GATE_NAMES = {1: "venv", 2: "suite", 3: "imports", 4: "entrypoint"}


class BudgetExhausted(Exception):
    """Raised when the probe has spent its time. Always resolves to Tier 0."""


@dataclass
class Session:
    """The runner, the clock, and the log of what was actually executed."""

    python: Path
    budget: Budget
    runner: Runner
    started: float = field(default_factory=monotonic)
    commands: list[str] = field(default_factory=list)

    def run(self, command: Command) -> Completed:
        remaining = self.budget.total_seconds - (monotonic() - self.started)
        if remaining <= 0:
            raise BudgetExhausted(
                f"the probe's {self.budget.total_seconds:.0f}s budget was spent "
                f"before `{command.display()}` could run"
            )
        # Clamped, so no single command can overrun the total on its own.
        bounded = Command(
            kind=command.kind,
            python=command.python,
            arguments=command.arguments,
            timeout=min(command.timeout, remaining),
        )
        self.commands.append(bounded.display())
        return self.runner(bounded)


@dataclass(frozen=True)
class Smoke:
    """Gate 3's answer: an outcome for the log, and a tier for each module.

    The outcome alone was enough while a failure demoted the repository. Now
    that it demotes per module (#28), the gate has to hand back *which* modules
    — and `attempted`, so that "eight of a hundred failed" can be told from
    "all eight of eight failed". They are different repositories and only the
    second one has nothing left to characterize.
    """

    outcome: GateOutcome
    demoted: tuple[Demotion, ...] = ()
    attempted: int = 0

    @property
    def nothing_survived(self) -> bool:
        return self.attempted > 0 and len(self.demoted) == self.attempted


def resolve(root: Path, first_party: Sequence[str], session: Session) -> TierVerdict:
    """Walk the gates and return the verdict, with every gate's evidence.

    Nothing raises out of here. An unattended run reads this out of a log at
    3am, so an unexpected repository has to become a verdict rather than a
    traceback — and the only verdict an unexpected repository can have is the
    conservative one. Every `except` below therefore ends at Tier 0, including
    the ones that mean Polish itself is broken.
    """
    reached: list[GateOutcome] = []
    try:
        return _walk(root, first_party, session, reached)
    except BudgetExhausted as spent:
        reason = f"{spent}; ambiguity resolves downward"
    except NotAllowed as refused:
        # Not a repository being unusual — a bug or an attack. Either way the
        # night continues at Tier 0 rather than ending.
        reason = f"a command outside the allowlist was refused: {refused}"
    except OSError as failure:
        reason = f"the probe could not run in the sandbox: {failure}"
    return _verdict(Tier.ZERO, reason, _padded(reached, reason), session)


def _walk(
    root: Path,
    first_party: Sequence[str],
    session: Session,
    reached: list[GateOutcome],
) -> TierVerdict:
    target = _discovery.opted_in(root)

    reached.append(_gate_one(root, session))
    if reached[-1].failed:
        return _verdict(
            Tier.ZERO, _WHY_GATE_ONE, _padded(reached, _WHY_PINNED), session
        )

    suite = _append(reached, _gate_two(root, first_party, session))
    smoke = _gate_three(
        root, first_party, session, needed=suite.passed or target is not None
    )
    reached.append(smoke.outcome)

    # Modules that failed the smoke are held at Tier 0 individually from here
    # on; they no longer take the repository with them. The one case that still
    # pins is a repository where *nothing* imported: there is no code left for
    # the ceiling to apply to, so raising it would be a claim about an empty
    # set, and gate 4 — the riskiest command the probe has — is not worth
    # running to decide the tier of nothing.
    if smoke.nothing_survived:
        return _verdict(
            Tier.ZERO, _WHY_GATE_THREE, _padded(reached, _WHY_PINNED), session, smoke
        )

    # Gate 4 runs only when it could change the answer. Gate 2 having already
    # supplied an execution source is not a reason to also run a user's entry
    # point: it is the riskiest thing the probe can do, for no extra verdict.
    entry = _append(reached, _gate_four(session, None if suite.passed else target))

    if suite.passed:
        return _verdict(Tier.ONE, _WHY_SUITE, reached, session, smoke)
    if entry.passed:
        return _verdict(Tier.ONE, _WHY_ENTRYPOINT, reached, session, smoke)
    return _verdict(Tier.ZERO, _WHY_NO_SOURCE, reached, session, smoke)


def _gate_one(root: Path, session: Session) -> GateOutcome:
    """Can an isolated virtualenv be built from what the repository declares?

    This gate executes no repository code, which is what makes it safe to run
    first and unattended. `--only-binary :all:` is why: no sdist is built, so no
    third-party `setup.py` runs either, and the project itself is never
    installed. A repository that genuinely needs a source build fails here and
    lands Tier 0 — the acceptable error, not the dangerous one.
    """
    required = _discovery.requirements(root)
    if not required:
        return _outcome(1, PASS, "nothing declared; the sandbox virtualenv is enough")
    result = session.run(
        _commands.pip_install(session.python, required, session.budget.install_seconds)
    )
    if result.ok:
        return _outcome(1, PASS, f"{len(required)} declared requirements installed")
    return _outcome(
        1,
        FAIL,
        f"installing {len(required)} declared requirements failed: {result.tail()}",
    )


def _gate_two(root: Path, first_party: Sequence[str], session: Session) -> GateOutcome:
    """Is there a suite, does it run, and does anything in it pass?

    ">0 passing" rather than "all passing": a suite with three failures and a
    hundred and twenty-eight passes is still a hundred and twenty-eight real
    execution traces, and demanding a green suite would push exactly the
    half-tidy repositories Polish exists for down to Tier 0.
    """
    discovered = _discovery.test_files(first_party)
    if not discovered:
        return _outcome(
            2, FAIL, "no test_*.py or *_test.py among the first-party files"
        )

    missing = _ensure_pytest(session)
    if missing is not None:
        return _outcome(2, FAIL, missing)

    result = session.run(_commands.pytest(session.python, session.budget.suite_seconds))
    if result.timed_out:
        return _outcome(
            2,
            FAIL,
            f"the suite did not finish within {session.budget.suite_seconds:.0f}s",
        )
    match = PASSED.search(result.output)
    if match is None:
        return _outcome(2, FAIL, f"pytest reported no pass count: {result.tail()}")
    passing = int(match.group(1))
    if passing == 0:
        return _outcome(2, FAIL, f"{len(discovered)} test files, 0 passing tests")
    return _outcome(
        2, PASS, f"{passing} passing tests across {len(discovered)} test files"
    )


def _gate_three(
    root: Path, first_party: Sequence[str], session: Session, *, needed: bool
) -> Smoke:
    """Which of the repository's own modules import? Demote-only, per module.

    The set this produces is closed under the import graph without having to
    walk it: a module that imports a broken one is itself broken, so it fails
    the smoke too and arrives here on its own. Nothing that keeps the ceiling
    depends at import time on anything that lost it.
    """
    if not needed:
        return Smoke(_outcome(3, SKIP, _WHY_SKIP_THREE))

    found = _discovery.modules(root, first_party)
    if not found:
        return Smoke(_outcome(3, PASS, "no addressable first-party modules"))

    failures = _failed_imports(session, [module.name for module in found])
    if not failures:
        return Smoke(
            _outcome(
                3,
                PASS,
                f"{len(found)} first-party modules import cleanly — which "
                "promotes nothing (SPEC §3.1)",
            ),
            attempted=len(found),
        )
    return Smoke(
        _outcome(
            3,
            FAIL,
            f"{len(failures)} of {len(found)} first-party modules do not import "
            "and are held at tier 0: " + ", ".join(sorted(failures)[:5]),
        ),
        demoted=tuple(
            Demotion(module=module.name, path=module.path, detail=failures[module.name])
            for module in found
            if module.name in failures
        ),
        attempted=len(found),
    )


def _gate_four(session: Session, target: str | None) -> GateOutcome:
    """The opt-in execution source. Never inferred, never automatic.

    SPEC §3.3 makes this the largest risk surface the probe has, so a
    `__main__.py` or a `[project.scripts]` table is not consent — only
    `[probe] entrypoint` in the user's own `polish.toml` is.
    """
    if target is None:
        return _outcome(4, SKIP, "not configured; never run without being asked")
    try:
        command = _commands.entrypoint(
            session.python, target, session.budget.entrypoint_seconds
        )
    except NotAllowed as refused:
        # Answered here rather than at `resolve()`, because the user wrote this
        # into their own config and the actionable message is about that line.
        return _outcome(4, FAIL, f"refused: {refused}")

    result = session.run(command)
    if result.ok:
        return _outcome(4, PASS, f"`{command.display()}` exited 0")
    return _outcome(4, FAIL, f"`{command.display()}` {result.tail()}")


def _ensure_pytest(session: Session) -> str | None:
    """Make pytest importable in the sandbox, or say why gate 2 cannot proceed.

    Failure here is still a gate 2 failure and still resolves downward. The two
    commands live in `_commands` because characterization needs the same pair
    for the same reason (#32), and one implementation is one thing to keep true.
    """
    return (
        _commands.ensure_pytest(
            session.python,
            session.run,
            import_seconds=session.budget.import_seconds,
            install_seconds=session.budget.install_seconds,
        )
        or None
    )


def _failed_imports(session: Session, names: Sequence[str]) -> dict[str, str]:
    """Import every name, in batches, and attribute the failures to a module.

    A failing batch is re-run one module at a time. Python stops at the first
    bad name in an `import a, b, c`, so the batch alone can only say that
    something in it broke — and "something broke" is not the evidence issue #12
    asks the verdict to carry. Since #28 it is also not enough to *act* on: a
    demotion names one module, so the failure has to be attributed to one.

    The value is that module's own error, which is what makes the demotion
    reviewable — `ModuleNotFoundError: celery` reads as somebody's example app,
    and a `SyntaxError` reads as a repository Polish should be nowhere near.
    """
    failures: dict[str, str] = {}
    timeout = session.budget.import_seconds
    for start in range(0, len(names), BATCH):
        batch = names[start : start + BATCH]
        if session.run(_commands.import_smoke(session.python, batch, timeout)).ok:
            continue
        for name in batch:
            alone = _commands.import_smoke(session.python, [name], timeout)
            result = session.run(alone)
            if not result.ok:
                failures[name] = f"does not import: {result.tail(1)}"
    return failures


def _outcome(number: int, status: str, detail: str) -> GateOutcome:
    return GateOutcome(
        number=number, name=GATE_NAMES[number], status=status, detail=detail
    )


def _append(reached: list[GateOutcome], outcome: GateOutcome) -> GateOutcome:
    reached.append(outcome)
    return outcome


def _padded(reached: Sequence[GateOutcome], reason: str) -> list[GateOutcome]:
    """Fill in the gates that never ran, so the log has all four either way.

    A verdict that silently omits a gate reads as though that gate passed. Every
    gate says what happened to it, including "nothing".
    """
    seen = {outcome.number for outcome in reached}
    missing = [
        _outcome(number, SKIP, reason) for number in GATE_NAMES if number not in seen
    ]
    return sorted([*reached, *missing], key=lambda outcome: outcome.number)


def _verdict(
    tier: Tier,
    reason: str,
    gates: Sequence[GateOutcome],
    session: Session,
    smoke: Smoke | None = None,
) -> TierVerdict:
    return TierVerdict(
        ceiling=tier,
        reason=reason,
        gates=tuple(gates),
        demoted=smoke.demoted if smoke is not None else (),
        commands=tuple(session.commands),
    )


_WHY_GATE_ONE = "the sandbox could not be built from the declared dependencies"
_WHY_GATE_THREE = "no first-party module imports in the sandbox"
_WHY_SUITE = "a test suite ran, so the code that imports can be characterized"
_WHY_ENTRYPOINT = (
    "an opted-in entry point ran, so the code that imports can be characterized"
)
_WHY_NO_SOURCE = (
    "no execution source: nothing was found to trace, so there would be nothing "
    "to characterize (SPEC §3.1)"
)
_WHY_PINNED = "not evaluated: an earlier gate already pinned Tier 0"
_WHY_SKIP_THREE = (
    "not evaluated: no execution source, and an import smoke cannot supply one "
    "(SPEC §3.1)"
)
