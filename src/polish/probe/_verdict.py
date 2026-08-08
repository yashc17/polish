"""What a probe produces: a tier per path, and the evidence that produced it.

Kept apart from the gates that fill it in, because everything downstream of #12
— the whole Tier 1 chain — consumes this and nothing else. The verdict is the
interface; the four gates are an implementation detail that could be replaced
without any caller noticing.

**The verdict is not one tier.** It was, until #28: issue #3 chose a
per-repository answer deliberately while question #4 was open, and #4 closed in
favour of per-symbol / per-path tiering — an edit's tier is the *lowest* tier
among the code it touches. So what a probe returns now is a **ceiling** plus the
paths gate 3 took it away from, and `tier_of` is the question a caller actually
has: not "is this repository Tier 1" but "may I use Tier 1 on *this file*".
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cached_property

PASS = "pass"
FAIL = "fail"
SKIP = "skip"

#: How many demoted paths the run log itemises before summarising the rest. The
#: full list is always on the verdict; this bounds the log, not the data.
DETAIL = 12


class Tier(enum.IntEnum):
    """How aggressively Polish has earned the right to refactor (SPEC §3.1).

    Ordered, and the ordering is load-bearing: every downward resolution in the
    probe is expressible as `min(...)`, and `Tier.ZERO` is falsy, so "no
    evidence" and "Tier 0" are the same value. A tier that is not `ONE` is
    always `ZERO` — there is no third state for "unsure", because unsure *is*
    Tier 0.
    """

    ZERO = 0
    ONE = 1

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True)
class Budget:
    """Time bounds. Injectable so the timeout paths can have tests.

    A probe that hangs is worse than a probe that gives up: SPEC §4.1 requires a
    run to complete unattended, and a repository whose suite waits on a database
    that is not there will otherwise wait until morning. Every bound resolves
    downward on expiry, never upward.
    """

    install_seconds: float = 300.0
    suite_seconds: float = 600.0
    import_seconds: float = 60.0
    entrypoint_seconds: float = 120.0
    total_seconds: float = 900.0


@dataclass(frozen=True)
class GateOutcome:
    """One gate's answer, and why.

    `status` is not a tier. Gate 3 passing means "did not demote", and gate 4
    being skipped means "was never configured, and is never run without being" —
    neither is a promotion. Keeping the two vocabularies separate is what stops
    a reader from summing four passes into a Tier 1.
    """

    number: int
    name: str
    status: str
    detail: str

    @property
    def passed(self) -> bool:
        return self.status == PASS

    @property
    def failed(self) -> bool:
        return self.status == FAIL


@dataclass(frozen=True)
class Demotion:
    """One module held below the ceiling, and the evidence that held it there.

    Gate 3's output, one row per module that did not import. Carrying the path
    as well as the dotted name is what makes the verdict answerable: a caller
    holds a `Change`, and a change is one file (SPEC §3.9).
    """

    module: str
    path: str
    detail: str

    def note(self) -> str:
        return f"{self.path} — {self.module} {self.detail}"


@dataclass(frozen=True)
class TierVerdict:
    """The probe's answer for one repository, at the granularity of a path.

    `ceiling` is the highest tier *any* code here earned, and it is a genuinely
    repository-wide fact: an execution source (SPEC §3.1's suite or opted-in
    entry point) either exists or does not, and without one there is nothing to
    characterize anywhere. `demoted` is what gate 3 subtracts from it, per
    module. Together they answer `tier_of(path)`, which is the only question
    downstream of here — #15 asks it once per candidate change.

    `commands` is every command that was actually issued, in order. It is in the
    run log rather than in a debug flag because "the probe respects the command
    allowlist" (issue #12) is a claim, and a claim a user cannot check is a
    claim they have to take on faith — which is the thing this product does not
    ask for.
    """

    ceiling: Tier
    reason: str
    gates: tuple[GateOutcome, ...]
    demoted: tuple[Demotion, ...] = ()
    commands: tuple[str, ...] = ()

    @property
    def reaches_tier_one(self) -> bool:
        """Is *any* code here eligible for Tier 1? Not "is the repository".

        What the envelope asks before characterizing at all: a repository with
        no execution source has nothing to trace, and one with an execution
        source has something to trace even if half its modules were demoted.
        """
        return self.ceiling is Tier.ONE

    @cached_property
    def demoted_paths(self) -> frozenset[str]:
        return frozenset(item.path for item in self.demoted)

    def tier_of(self, path: str) -> Tier:
        """The tier of one first-party path — question #4's granularity.

        A path gate 3 never smoked takes the ceiling, and that is deliberate
        rather than lax. **Gate 3 is demote-only** (SPEC §3.1, and the loudest
        rule in `_gates`): a clean import promotes nothing, so the *absence* of
        an import result cannot demote either, or the gate would have quietly
        become a promotion gate — "only what gate 3 saw may be Tier 1". What is
        unsmoked is the suite's own files and the modules that run rather than
        define themselves; every addressable first-party module has an answer,
        because the envelope hands the probe the whole first-party set.

        The upward-resolution worry this raises is answered one layer down, not
        here: an edit is Tier 1 only if `characterize.Coverage` also has a
        surviving oracle for the symbols it touches, and *that* table resolves
        an unknown name to Tier 0. The probe supplies a ceiling; the coverage
        table supplies the floor-by-evidence.
        """
        return Tier.ZERO if path in self.demoted_paths else self.ceiling

    def tier_for(self, paths: Iterable[str]) -> Tier:
        """The lowest tier among `paths` — question #4's resolution rule.

        Empty resolves to Tier 0, matching `Coverage.tier_for`: a change that
        touches nothing has had nothing verified about it, and the two rules are
        composed by `min()` at the call site, so they must not disagree about
        the degenerate case.
        """
        tiers = [self.tier_of(path) for path in paths]
        return min(tiers) if tiers else Tier.ZERO

    def summary(self) -> tuple[str, ...]:
        """The verdict, one fact per line — the same register as `RunRecord`."""
        claim = "characterization" if self.reaches_tier_one else "provable"
        lines = [
            _line("tier", f"{self.ceiling} ({claim}) — the ceiling for every path"),
            _line("reason", self.reason),
        ]
        lines += [
            _line(f"gate {gate.number}", f"{gate.status:<5}{gate.name}: {gate.detail}")
            for gate in self.gates
        ]
        # Named, not counted. Issue #28's last criterion asks the run log to say
        # which code landed in which tier and why, and a count would answer only
        # the first half — a developer who sees eight demoted paths wants to
        # know whether they are their own package or somebody's example app.
        if self.demoted:
            lines.append(
                _line(
                    "demoted",
                    f"{len(self.demoted)} paths held at tier 0; the rest are "
                    f"tier {self.ceiling}",
                )
            )
            lines += [f"  ! {item.note()}" for item in self.demoted[:DETAIL]]
            if len(self.demoted) > DETAIL:
                lines.append(f"  ! ... and {len(self.demoted) - DETAIL} more paths")
        lines += [_line("ran", command) for command in self.commands]
        return tuple(lines)


def _line(label: str, value: str) -> str:
    return f"{label:<10}{value}"
