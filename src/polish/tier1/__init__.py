"""Tier 1 — the night shift, on a repository Polish can actually run.

One public entry point, and everything behind it is private:

    refactor(worktree, files, characterization=..., verdict=..., venv=...)

Tier 0 asks "can this edit be justified by reading alone?" and ships the small
set of transforms where the answer is yes. Tier 1 asks a different question —
"did this edit change what the code does?" — and can answer it, because
`polish.characterize` recorded what the code did before anything was edited. That
is the whole of the difference, and it is why the claim changes from *provably
behaviour-preserving* to **verified against recorded behaviour** (SPEC §3.1).

The loop is four steps per change, and the ticket's acceptance criteria are the
reason each one is where it is:

1. **Tier the change before touching it** (`_eligibility`). Question #4's rule
   over both tables that answer it — the probe's per-path ceiling and the
   trace's per-symbol coverage — plus #5's precondition: a change touching
   code with no surviving oracle is refused Tier 1 treatment, and the log says
   what refused it. Refused *before* the edit, so it costs and disturbs
   nothing.
2. **Let the agent edit, in the sandbox** (`_agent`). Never the working tree —
   `refactor()` takes a worktree and the envelope only ever hands it one.
3. **Verify** (`_verify`): the characterization oracles, the repository's own
   suite, and a type check. All three, every change, every attempt.
4. **Keep, repair once, or drop** (below). A failing change gets **exactly one**
   bounded repair attempt. If it still fails, that change alone is reverted, the
   failure output goes in the log, and the queue continues.

## The two properties this module exists to guarantee

**No change that fails verification lands** (criterion 5). There is one path to
a yielded `Change` with `kept=True`, and it runs through `Check.passed`. A
change that failed, declined, drifted, or raised is reverted from the snapshot
`_files` holds and yielded with `kept=False` — still a change, still in the log,
because SPEC §3.10 wants the failure visible rather than the night looking idle.

**One failure never aborts the queue** (criterion 7). Every per-change step is
inside the try below. An agent that raises, a verifier that cannot run, a
repository that moves under the run — each costs one change and nothing more.
"Aborting the whole run on one failure means a single flaky test wastes the
night" is the reasoning, and it applies to every failure, not just flaky ones.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from polish.characterize import Characterization, Coverage, Policy
from polish.probe import Runner, TierVerdict
from polish.tier0 import Unverifiable

from . import _eligibility
from ._agent import Agent, AgentError, Brief, Failure, Proposed, Recorded
from ._files import Snapshot
from ._verify import Check, Verifier

#: The tier this engine is. Quoted in every commit message it produces.
TIER = 1

#: **Exactly one** (SPEC §3.10, and issue #15 by name: "not zero, not
#: unbounded"). A module constant rather than a `Policy` field, deliberately.
#: Zero discards work an obvious fix would have saved; unbounded is an agent
#: arguing with a test suite until the budget runs out, unattended, at 3am.
#: Neither is a number a user should be able to reach for — the bound *is* the
#: policy, so it is not configuration.
_ONE_REPAIR_ATTEMPT = 1

__all__ = [
    "TIER",
    "Agent",
    "AgentError",
    "Brief",
    "Change",
    "Failure",
    "Proposed",
    "Recorded",
    # Tier 0's, re-exported rather than redefined. "The type check could not
    # run" means the same thing and resolves the same way in both tiers, and two
    # exception classes for one condition would make a caller choose which to
    # catch on evidence that does not exist.
    "Unverifiable",
    "refactor",
]


@dataclass(frozen=True)
class Change:
    """One proposed refactor, and what became of it.

    A dropped change is still a change. Tier 0 says the same thing for the same
    reason, and it matters more here: Tier 1's whole pitch is that it attempts
    work Tier 0 will not, so a reviewer who cannot see what was attempted and
    rejected has no way to tell a careful night from an idle one.
    """

    proposed: Proposed
    kept: bool
    #: Verification evidence when the change was kept; the refusal when not.
    evidence: tuple[str, ...]
    #: How many repair attempts were spent. 0 or 1, always — see
    #: `_ONE_REPAIR_ATTEMPT`. Quoted in the commit message either way, because
    #: "Repair attempts: 0" is information about how clean the edit was.
    repairs: int = 0
    #: The failing output, for a change that was dropped. In the run log rather
    #: than only in a debug stream: SPEC §3.10 asks for the failure by name.
    failure: str = ""

    @property
    def paths(self) -> tuple[str, ...]:
        return self.proposed.paths

    def subject(self) -> str:
        return f"refactor({self.proposed.category}): {self.proposed.summary}"

    def message(self) -> str:
        """The commit message, evidence and all (SPEC §3.9)."""
        body = [self.subject(), "", _wrap(self.proposed.rationale)]
        body += [
            "",
            f"Tier: {TIER} (characterization)",
            *self.evidence,
            f"Repair attempts: {self.repairs}",
        ]
        return "\n".join(body) + "\n"

    def note(self) -> str:
        """One line for the run log — what happened to this change, and why."""
        detail = self.evidence[0] if self.evidence else "dropped"
        return f"{self.subject()} — {detail}"

    def report(self) -> tuple[str, ...]:
        """The run log's entry for a dropped change: the line, then the output.

        Indented rather than summarised. The failure is the product here in the
        same way the "left alone" reason is Tier 0's: a developer deciding
        whether to trust tomorrow night needs to see what tonight could not do.
        """
        if self.kept or not self.failure:
            return (self.note(),)
        return (self.note(), *(f"    {line}" for line in self.failure.splitlines()))


def refactor(
    worktree: Path,
    files: Sequence[str],
    *,
    characterization: Characterization,
    verdict: TierVerdict,
    venv: Path,
    agent: Agent | None = None,
    verify_against: Sequence[str] | None = None,
    policy: Policy | None = None,
    runner: Runner | None = None,
) -> Iterator[Change]:
    """Run the Tier 1 queue inside `worktree`, yielding each change once settled.

    Every yielded change has already been decided: a kept one is on disk and
    verified, a dropped one has been reverted and the tree is byte-identical to
    what it was. The caller commits each kept change before taking the next,
    which is what makes the branch reviewable commit by commit (SPEC §3.9).

    `characterization` and `verdict` are the two tables an edit is tiered
    against, and both bind (`_eligibility`). Required rather than optional: a
    default of "no ceiling information" would silently weaken the rule to the
    coverage half, which is the shape of mistake this tier cannot afford.

    `files` is what may be edited — the run's scope (SPEC §3.6).
    `verify_against` is what the **type check** covers, defaulting to `files`,
    and the two are separate for the reason Tier 0 spells out: the scope may
    narrow what Polish edits, never what it checks. A cross-file dedup that
    stranded a caller just outside the scope is exactly the failure this tier is
    most able to cause and least able to notice any other way.

    `agent` is the edit engine. It defaults to the Claude Agent SDK
    (`_claude.ClaudeAgent`), imported lazily so that a Polish installed without
    it — or run without credentials — declines this tier cleanly instead of
    failing to import. Injectable on the same terms as `probe(runner=...)`: a
    seam only tests can reach is a seam nobody can inspect.

    Raises `Unverifiable` if the type check cannot establish a baseline. That
    happens before anything is edited, so the worktree is untouched.
    """
    worktree = Path(worktree)
    coverage = characterization.coverage
    checked = tuple(files if verify_against is None else verify_against)

    try:
        engine = agent if agent is not None else _default_agent(worktree, policy)
    except AgentError as refused:
        yield from ()
        raise Unverifiable(str(refused)) from refused

    verifier = Verifier(
        worktree,
        characterization=characterization,
        venv=venv,
        first_party=checked,
        checked=checked,
        policy=policy,
        runner=runner,
    )
    # Before the first edit, so a repository whose type check cannot run says so
    # with an untouched worktree rather than after the agent has been paid.
    verifier.establish()

    try:
        queue = tuple(engine.propose(_brief(worktree, files, characterization)))
    except AgentError as refused:
        # An agent that *could not* propose and one that proposed *nothing* are
        # different nights, and only the first is worth waking up to. This one
        # is reported through the tier's decline channel — the run ships what
        # Tier 0 already committed and says why Tier 1 added nothing.
        raise Unverifiable(f"the change queue could not be proposed — {refused}") from (
            refused
        )
    if not queue:
        # And this one is simply a quiet night. Nothing to report beyond the
        # zero in the run log's change count.
        return

    snapshot = Snapshot.take(worktree, checked)

    for proposed in queue:
        yield _settle(worktree, proposed, engine, verifier, snapshot, coverage, verdict)


def _settle(
    worktree: Path,
    proposed: Proposed,
    agent: Agent,
    verifier: Verifier,
    snapshot: Snapshot,
    coverage: Coverage,
    verdict: TierVerdict,
) -> Change:
    """Take one change from proposed to decided. Never raises.

    Criterion 7 lives in the `except` clauses: every way a single change can go
    wrong is caught here and costs that change alone. The queue continues.
    """
    eligible = _eligibility.resolve(worktree, proposed, coverage, verdict)
    if not eligible.eligible:
        # Refused before the edit, so there is nothing to revert. This is the
        # rule from #5's decision comment on #15, and the run log says why —
        # which is the half of that criterion a silent skip would have missed.
        return Change(
            proposed=proposed,
            kept=False,
            evidence=(eligible.refusal(),),
        )

    snapshot.cover(proposed.paths)
    try:
        agent.apply(proposed)
        return _verify_then(proposed, agent, verifier, snapshot, eligible, repairs=0)
    except AgentError as refused:
        return _dropped(snapshot, proposed, f"the agent could not edit — {refused}")
    except OSError as failure:
        return _dropped(
            snapshot, proposed, f"the edit could not be written — {failure}"
        )


def _verify_then(
    proposed: Proposed,
    agent: Agent,
    verifier: Verifier,
    snapshot: Snapshot,
    eligible: _eligibility.Eligibility,
    *,
    repairs: int,
) -> Change:
    """Verify what is on disk, then keep it, repair it once, or drop it."""
    drift = _drift(snapshot, proposed)
    if drift:
        # The agent edited outside what it declared. Not a verification failure
        # and not repairable: the change Polish tiered is not the change on the
        # disk, so there is nothing here whose eligibility was ever established.
        return _dropped(
            snapshot,
            proposed,
            f"the edit touched files the change did not declare — {', '.join(drift)}",
            paths=(*proposed.paths, *drift),
        )

    checked = verifier.check(proposed.paths)

    if checked.passed:
        snapshot.adopt(proposed.paths)
        return Change(
            proposed=proposed,
            kept=True,
            repairs=repairs,
            evidence=(
                f"Oracles: {eligible.covered} symbol"
                f"{'' if eligible.covered == 1 else 's'} with recorded behaviour, "
                "all still holding",
                *checked.evidence,
            ),
        )

    if checked.declined:
        # Verification that could not run is not a verdict, so it does not earn
        # the repair attempt either — there is nothing to repair towards.
        return _dropped(
            snapshot, proposed, f"verification could not run — {checked.declined}"
        )

    if repairs >= _ONE_REPAIR_ATTEMPT:
        # The bound, and the only place it is enforced. Second failure, so the
        # change is reverted alone and logged with what it failed on.
        return _dropped(
            snapshot,
            proposed,
            "still failing after one repair attempt",
            failure=checked.failure,
            repairs=repairs,
        )

    return _repair(proposed, agent, verifier, snapshot, eligible, checked)


def _repair(
    proposed: Proposed,
    agent: Agent,
    verifier: Verifier,
    snapshot: Snapshot,
    eligible: _eligibility.Eligibility,
    checked: Check,
) -> Change:
    """The one attempt. Same move a human makes (SPEC §3.10).

    The failing output goes back verbatim; the tree is left exactly as the
    failing edit left it, because a repair is a fix to *this* change rather than
    a second attempt at the change from scratch.
    """
    failure = Failure(
        change=proposed,
        detail=checked.failure,
        summary="verification failed, repairing",
    )
    try:
        agent.repair(failure)
    except AgentError as refused:
        return _dropped(
            snapshot,
            proposed,
            f"verification failed and the repair attempt could not run — {refused}",
            failure=checked.failure,
            repairs=_ONE_REPAIR_ATTEMPT,
        )
    except OSError as broken:
        return _dropped(
            snapshot,
            proposed,
            f"verification failed and the repair could not be written — {broken}",
            failure=checked.failure,
            repairs=_ONE_REPAIR_ATTEMPT,
        )

    return _verify_then(
        proposed,
        agent,
        verifier,
        snapshot,
        eligible,
        repairs=_ONE_REPAIR_ATTEMPT,
    )


def _drift(snapshot: Snapshot, proposed: Proposed) -> tuple[str, ...]:
    """Tracked paths that moved but were not declared by this change."""
    declared = set(proposed.paths)
    return tuple(name for name in snapshot.drifted() if name not in declared)


def _dropped(
    snapshot: Snapshot,
    proposed: Proposed,
    reason: str,
    *,
    failure: str = "",
    repairs: int = 0,
    paths: Sequence[str] | None = None,
) -> Change:
    """Revert this change alone and report it (SPEC §3.10).

    The revert is the whole declared path set plus anything that drifted, so
    that what is left behind is the tree as it was — including deleting a file
    the agent created, which no other revert in this codebase has had to do.
    """
    snapshot.restore(paths if paths is not None else proposed.paths)
    return Change(
        proposed=proposed,
        kept=False,
        repairs=repairs,
        evidence=(f"dropped, {reason}",),
        failure=failure,
    )


def _brief(
    worktree: Path, files: Sequence[str], characterization: Characterization
) -> Brief:
    coverage = characterization.coverage
    eligible = tuple(symbol.name for symbol in coverage.symbols if symbol.oracles)
    ineligible = tuple(symbol.name for symbol in coverage.symbols if not symbol.oracles)
    return Brief(
        root=str(worktree),
        files=tuple(files),
        eligible=eligible,
        ineligible=ineligible,
    )


def _default_agent(worktree: Path, policy: Policy | None) -> Agent:
    """The Claude Agent SDK, imported only when it is actually going to be used.

    Lazy because the SDK is the one dependency Polish cannot exercise offline:
    it needs Node, a `claude` binary and credentials. Importing it at module
    load would make `polish scan` on a machine without any of that fail on an
    import, for a tier it was never going to reach.
    """
    from ._claude import ClaudeAgent

    return ClaudeAgent(worktree)


def _wrap(text: str) -> str:
    return "\n".join(
        textwrap.fill(paragraph, width=72) for paragraph in text.split("\n\n")
    )
