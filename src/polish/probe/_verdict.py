"""What a probe produces: a tier, and the evidence that produced it.

Kept apart from the gates that fill it in, because everything downstream of #12
— the whole Tier 1 chain — consumes this and nothing else. The verdict is the
interface; the four gates are an implementation detail that could be replaced
without any caller noticing.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

PASS = "pass"
FAIL = "fail"
SKIP = "skip"


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
class TierVerdict:
    """The probe's answer for one repository.

    `commands` is every command that was actually issued, in order. It is in the
    run log rather than in a debug flag because "the probe respects the command
    allowlist" (issue #12) is a claim, and a claim a user cannot check is a
    claim they have to take on faith — which is the thing this product does not
    ask for.
    """

    tier: Tier
    reason: str
    gates: tuple[GateOutcome, ...]
    commands: tuple[str, ...] = ()

    @property
    def tier_one(self) -> bool:
        return self.tier is Tier.ONE

    def summary(self) -> tuple[str, ...]:
        """The verdict, one fact per line — the same register as `RunRecord`."""
        claim = "characterization" if self.tier_one else "provable"
        lines = [_line("tier", f"{self.tier} ({claim})"), _line("reason", self.reason)]
        lines += [
            _line(f"gate {gate.number}", f"{gate.status:<5}{gate.name}: {gate.detail}")
            for gate in self.gates
        ]
        lines += [_line("ran", command) for command in self.commands]
        return tuple(lines)


def _line(label: str, value: str) -> str:
    return f"{label:<10}{value}"
