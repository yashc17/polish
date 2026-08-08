"""The three checks every Tier 1 change passes, composed into one answer.

Issue #15's second acceptance criterion names them: **characterization tests,
existing tests, and a type check**. The first two are `polish.characterize`'s
`verify()`, which already owns the quarantine rule and the retry backstop. The
third is Tier 0's `mypy` checker, reused rather than reimplemented — the gate is
the same gate ("no diagnostic that was not already there", ADR 0004), and a
second implementation of it would be a second thing to keep true.

**The type check runs first, and the ordering is not incidental.** It costs a
few seconds against an incremental cache; the oracles and the suite cost the
repository's whole test runtime, up to three times over. A change that broke a
name is going to fail either way, and failing it on the cheap check leaves the
expensive one for changes that might actually ship. On a large repository that
is most of the difference between a night that finishes and one that does not.

## What "failed" is allowed to mean

`Check.failure` is what the single repair attempt is given (SPEC §3.10), so it
is assembled to be *actionable rather than complete*: the diagnostics with their
file and message, the failing test ids, and — for a failing oracle — the
recorded call and what it was observed returning. The last of those is the one a
paraphrase would ruin. "An oracle failed" tells the agent nothing; `scale([1, 2],
2) was observed returning [2, 4]` tells it exactly which behaviour it changed,
which is the difference between a repair attempt and a guess.

A check that could not *run* is not a failure and does not get a repair attempt.
It is `declined`, the change is reverted, and the run says so — verification
that could not happen is not a verdict (SPEC §1.1).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from polish.characterize import Characterization, Oracle, Policy
from polish.characterize import verify as characterize_verify
from polish.probe import Runner
from polish.tier0 import CACHE_DIR, Unverifiable
from polish.tier0._typecheck import Checker, Diagnostics

#: How many failing items of each kind reach the agent. Enough to fix a change;
#: not so many that the repair attempt is spent reading. A change that broke
#: forty tests is not one an agent is going to talk its way out of.
DETAIL = 8


@dataclass(frozen=True)
class Check:
    """What the three checks said about one state of the tree."""

    passed: bool
    #: The lines this contributes to the commit message (SPEC §3.9).
    evidence: tuple[str, ...] = ()
    #: The failing output, verbatim enough to act on. Empty when it passed.
    failure: str = ""
    #: Why the check could not run. A declined check never passes and never
    #: earns a repair attempt — there is nothing to repair towards.
    declined: str = ""

    @property
    def repairable(self) -> bool:
        return not self.passed and not self.declined


class Verifier:
    """Runs the three checks against the sandbox worktree, once per attempt.

    Holds the type checker's baseline, which is established before any edit
    exists to bias it — the same reason the flake baseline is established first
    (issue #5). A baseline computed after the queue started would fold the first
    change's diagnostics into what every later change is allowed to introduce.
    """

    def __init__(
        self,
        worktree: Path,
        *,
        characterization: Characterization,
        venv: Path,
        first_party: Sequence[str],
        checked: Sequence[str],
        policy: Policy | None = None,
        runner: Runner | None = None,
    ) -> None:
        self._worktree = Path(worktree)
        self._characterization = characterization
        self._venv = Path(venv)
        self._first_party = tuple(first_party)
        self._checked = tuple(checked)
        self._policy = policy
        self._runner = runner
        self._oracles = {oracle.nodeid: oracle for oracle in characterization.oracles}
        self._baseline: Diagnostics | None = None

    def establish(self) -> None:
        """Take the type check's baseline. Raises `Unverifiable` if it cannot.

        Called before the queue is applied, so a repository whose type check
        cannot run declines the whole tier with an untouched worktree rather
        than discovering it after the first edit.
        """
        self._baseline = self._checker(self._checked).run()

    def check(self, paths: Sequence[str] = ()) -> Check:
        """Verify the tree as it stands. `paths` widens the type-check surface.

        `paths` is the change's own files, which matters only for a file the
        change created: the baseline's file set was fixed before it existed, and
        mypy will not check a file nobody named. Note the surface only ever
        *widens* — the scope may narrow what Polish edits, never what it checks
        (the rule Tier 0's `verify_against` exists to enforce).
        """
        typed = self._typecheck(paths)
        if not typed.passed:
            return typed

        checked = characterize_verify(
            self._worktree,
            self._characterization,
            venv=self._venv,
            first_party=self._first_party,
            policy=self._policy,
            runner=self._runner,
        )
        if checked.declined:
            return Check(passed=False, declined=checked.declined)
        if checked.passed:
            return Check(passed=True, evidence=(*typed.evidence, *checked.evidence()))
        return Check(
            passed=False,
            failure=_behaviour_failure(checked, self._oracles),
            evidence=checked.evidence(),
        )

    def _typecheck(self, paths: Sequence[str]) -> Check:
        if self._baseline is None:  # pragma: no cover - establish() comes first
            raise AssertionError("establish() must run before check()")

        surface = tuple(dict.fromkeys((*self._checked, *paths)))
        try:
            found = self._checker(surface).run()
        except Unverifiable as failure:
            return Check(
                passed=False, declined=f"the type check could not run — {failure}"
            )

        introduced = found.new_since(self._baseline)
        if not introduced:
            return Check(
                passed=True,
                evidence=(
                    "Type check: clean — mypy reports no diagnostic that was "
                    "not there before",
                ),
            )
        shown = "\n".join(f"  {line}" for line in introduced[:DETAIL])
        more = (
            f"\n  ... and {len(introduced) - DETAIL} more"
            if len(introduced) > DETAIL
            else ""
        )
        return Check(
            passed=False,
            failure=(
                f"The type check reports {_count(len(introduced), 'new diagnostic')} "
                f"that were not there before this change:\n{shown}{more}"
            ),
        )

    def _checker(self, files: Sequence[str]) -> Checker:
        # Rebuilt per run rather than held, because the file set moves when a
        # change creates a file. The cache directory is shared, so every check
        # after the first is incremental — the cost is writing a text file.
        return Checker(
            self._worktree,
            [name for name in files if (self._worktree / name).is_file()],
            cache=self._worktree.parent / CACHE_DIR,
        )


def _behaviour_failure(checked, oracles: dict[str, Oracle]) -> str:
    """The suite and oracle failures, in the form the repair attempt needs."""
    parts: list[str] = []

    if checked.oracle_failures:
        lines = [
            "This change contradicts behaviour that was recorded from a real "
            "run of the repository's own suite, before any edit:"
        ]
        for nodeid in checked.oracle_failures[:DETAIL]:
            oracle = oracles.get(nodeid)
            if oracle is None:  # pragma: no cover - an oracle file out of step
                lines.append(f"  {nodeid} failed")
                continue
            lines.append(
                f"  {oracle.call} was observed returning {oracle.expected}, "
                f"and no longer does ({nodeid})"
            )
        if len(checked.oracle_failures) > DETAIL:
            lines.append(f"  ... and {len(checked.oracle_failures) - DETAIL} more")
        parts.append("\n".join(lines))

    if checked.failed:
        lines = [
            f"The repository's own suite reports "
            f"{_count(len(checked.failed), 'test')} that "
            f"{'was' if len(checked.failed) == 1 else 'were'} not failing before "
            f"this change, over {_count(checked.attempts, 'attempt')}:"
        ]
        lines += [f"  {nodeid}" for nodeid in checked.failed[:DETAIL]]
        if len(checked.failed) > DETAIL:
            lines.append(f"  ... and {len(checked.failed) - DETAIL} more")
        parts.append("\n".join(lines))

    return "\n\n".join(parts)


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")
