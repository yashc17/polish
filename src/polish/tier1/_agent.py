"""The seam between Polish and whatever writes the edit.

SPEC §3.2 settles the edit engine as an agent rather than a codemod, and settles
what that costs: **every edit is a black box, so the verification layer must be
genuinely airtight.** This module is the black box's outline — three calls, and
nothing else Polish is allowed to assume about how an edit gets made.

    propose(brief)              the change queue, before anything is edited
    apply(change)               make the edit, in the sandbox
    repair(change, failure)     one bounded attempt, given what went wrong

Keeping the protocol this narrow is the point rather than a convenience. An
`Agent` that could also run the tests, read the oracles, or commit would be an
agent whose own account of success Polish might be tempted to believe — and the
one claim this product sells is that the *harness* decided, not the editor. The
protocol is shaped so there is nothing to believe: the agent changes bytes on a
disk, and everything downstream is measured.

`polish.tier1._claude` is the implementation that ships. This module imports
nothing from it, so the loop can be exercised end to end — including the
repair-then-drop path #15 asks for by name — without an API key, a network, or a
model whose output would make the test a measurement of Claude rather than of
Polish.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

#: The transform categories Tier 1 may propose, which become the commit-message
#: category in SPEC §3.9. A closed set, because the category is a review surface:
#: "a developer who distrusts one category can drop it without losing the night"
#: only works if the categories are stable enough to distrust.
CATEGORIES = ("dedup", "dead-code", "extract", "docstring", "simplify")


class AgentError(Exception):
    """The agent could not do what it was asked.

    Never a verdict about a change, and never fatal to a run. A queue that
    could not be proposed is an empty queue; an edit that could not be made is a
    change that never existed. Both are reported and the night continues, on the
    same terms as every other downward resolution in this codebase.
    """


@dataclass(frozen=True)
class Brief:
    """What the agent is told before it proposes anything.

    `eligible` is the part that does real work. Question #4's tiering rule is
    enforced by the harness whatever the agent proposes (`_eligibility`), but
    telling the agent up front which symbols are actually verifiable turns a
    refusal it would have earned into work it never wasted. The check stays
    where it is; this is the hint, not the gate.
    """

    #: The sandbox worktree. Every path below is relative to it.
    root: str
    #: What may be edited — the run's scope (SPEC §3.6), not the whole repo.
    files: tuple[str, ...]
    #: Dotted names of symbols with a surviving oracle, so Tier 1 work aimed at
    #: them can actually ship.
    eligible: tuple[str, ...]
    #: Dotted names of symbols that were executed but have no surviving oracle,
    #: plus those nothing executed. Editing these is not forbidden — it is
    #: refused Tier 1 treatment, which is a different and less useful outcome.
    ineligible: tuple[str, ...]
    categories: tuple[str, ...] = CATEGORIES


@dataclass(frozen=True)
class Proposed:
    """One change the agent intends to make, before it has made it.

    Proposed rather than applied, and the gap between the two is where the
    eligibility check lives: a change is tiered, and possibly refused, on what
    it *says* it will touch, before a single byte moves. An agent that then
    touches something else is caught by the drift check in `_files` and dropped.

    `paths` may name a file that does not exist yet — SPEC §3.9's worked example
    is "extract shared prompt-loading into prompts.py", which is a new file by
    construction.
    """

    category: str
    #: One imperative line. Becomes the commit subject (SPEC §3.9).
    summary: str
    #: The body of the commit message: what moved, and why it is the same.
    rationale: str
    paths: tuple[str, ...]
    #: Dotted names the change touches. Advisory — `_eligibility` also resolves
    #: every symbol in every path, because a change rewrites a whole file and a
    #: promise about which symbols it disturbs is not one Polish can check.
    symbols: tuple[str, ...] = ()
    #: What the agent intends to do, in its own words, replayed to it at edit
    #: time. The queue is proposed in one pass and applied one change at a time,
    #: so this is what survives between the two.
    instructions: str = ""

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise AgentError(
                f"{self.category!r} is not a Tier 1 category ({', '.join(CATEGORIES)})."
            )
        if not self.paths:
            raise AgentError("a change that touches no files is not a change.")


@dataclass(frozen=True)
class Failure:
    """Why a change failed verification, in the form the agent gets it back.

    Verbatim, and that is deliberate. SPEC §3.10 justifies the single repair
    attempt as "the same move a human makes", and the move only works if what
    comes back is the real output — a paraphrased failure is a failure the agent
    has to guess at, and the one attempt is spent guessing.
    """

    change: Proposed
    #: The failing output: type-checker diagnostics, test ids, oracle claims.
    detail: str
    #: One line for the run log, when this change is finally dropped.
    summary: str = ""

    def note(self) -> str:
        return self.summary or self.detail.splitlines()[0]


@runtime_checkable
class Agent(Protocol):
    """Whatever writes the edits. Claude Agent SDK in production, a fake in tests."""

    def propose(self, brief: Brief) -> Sequence[Proposed]:
        """The change queue. Must not edit anything — planning is read-only."""

    def apply(self, change: Proposed) -> None:
        """Make the edit on disk. Raises `AgentError` if it could not."""

    def repair(self, failure: Failure) -> None:
        """One bounded attempt at fixing what verification rejected.

        Called at most once per change, ever (SPEC §3.10). The bound is the
        caller's and is not negotiable from in here — an agent cannot ask for a
        second attempt, because the number that makes the policy a policy is
        `_ONE_REPAIR_ATTEMPT` in `polish.tier1`.
        """


@dataclass
class Recorded:
    """An `Agent` that does nothing and remembers being asked.

    Not a test double — it ships, because `polish run --dry-run` has to be able
    to show a night's plan without an edit, and because a queue that was
    proposed and never applied is the cheapest way to see what Tier 1 would do
    to a repository you do not yet trust it with.
    """

    queue: tuple[Proposed, ...] = ()
    applied: list[Proposed] = field(default_factory=list)
    repaired: list[Failure] = field(default_factory=list)

    def propose(self, brief: Brief) -> Sequence[Proposed]:
        return self.queue

    def apply(self, change: Proposed) -> None:
        self.applied.append(change)

    def repair(self, failure: Failure) -> None:
        self.repaired.append(failure)
