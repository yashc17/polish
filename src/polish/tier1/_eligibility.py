"""Which proposed changes have earned Tier 1 treatment, and which have not.

Two rules meet here, decided separately and both binding (see the note on
[#14](https://github.com/yashc17/polish/issues/14) that keeps them apart):

**Question #4 — tiering granularity.** An edit's tier is the *lowest* tier among
the code it touches, resolving downward. `Coverage.tier_for` already is that
sentence; this module decides what "the code it touches" means for a change that
names files rather than symbols.

**Question #5 — oracle reliability.** Code with no surviving characterization
test is not eligible for Tier 1 transforms and falls back to Tier 0 for the run.
`SymbolCoverage.tier` already is that sentence, so it composes rather than
needing to be reconciled: a symbol whose oracle was discarded is Tier 0, and one
Tier 0 symbol holds the whole change down.

The composition is what the run log has to be able to explain, because without
it SPEC §3.9's commit message would say "verified against recorded behaviour"
about a change whose oracle was thrown away. The claim has to be true of that
specific change, not of the run.

**Issue #28 — the probe's per-path ceiling.** A module gate 3 could not import
is Tier 0 whatever the coverage table says about it, and
[ADR 0010](../../../docs/adr/0010-a-tier-ceiling-that-gate-three-demotes-per-module.md)
makes that answerable per path. Both tables bind, and neither subsumes the other:
the probe supplies a **ceiling** ("this file could not even be imported"),
coverage supplies the **floor by evidence** ("no oracle survived for this
symbol"). A change clears only if it clears both, which is a `min` — computed
here because here is the only place that holds a whole change.

Composing them as a `min` over independent evidence, rather than as a chain of
conditions, is what keeps the rule honest as tables get added. Each one only ever
takes a tier away.

## The one place the rule is not simply "every symbol in every path"

A change may declare a path that **does not exist yet** — extraction creates
one. Resolving that path through `tier_for_path` would return Tier 0 (a file
with no symbols is not vacuously verified), which would refuse every extraction
Tier 1 exists to perform.

It would also be wrong, and the distinction is worth being precise about. Tier 0
for an uncharacterised *existing* file means "this code has behaviour Polish
never observed". A file that does not exist has no behaviour to preserve: there
is nothing about it to get wrong, and the thing that must not break — every
caller of the code being moved *out* of somewhere — lives in the existing paths,
which are resolved exactly as strictly as before. So a not-yet-existing path
contributes nothing to the tier.

**But a change made only of such paths is refused**, and for a different reason
than the tiering rule. If every declared path is new, the change moves no code
out of anywhere: it is generation, and SPEC §1.2 says Polish "only touches code
that already exists". A genuine extraction always declares the file it takes the
code *from* — that is what makes it an extraction rather than a new module — and
that file is what tiers it. So the two rules do not fight: new paths are exempt
from tiering, and a change with nothing *but* new paths is not a refactor.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from polish.characterize import Coverage
from polish.probe import Tier, TierVerdict

from ._agent import Proposed


@dataclass(frozen=True)
class Eligibility:
    """Whether a proposed change may be made at Tier 1, and why not when not."""

    tier: Tier
    #: The symbols that held the tier down, worst first. Named rather than
    #: counted, on the same principle as the quarantine list: a developer whose
    #: dedup was refused is owed the symbol that refused it.
    blocking: tuple[str, ...] = ()
    #: Paths the probe demoted, which hold a change down whatever the coverage
    #: table says about the symbols in them.
    demoted: tuple[str, ...] = ()
    #: How many symbols the change touches that do have a surviving oracle.
    #: Carried into the commit message as the evidence behind the tier claim.
    covered: int = 0
    #: True when the change touches no file that exists — see `resolve`.
    creates_only: bool = False

    @property
    def eligible(self) -> bool:
        return self.tier is Tier.ONE

    def refusal(self) -> str:
        """One line for the run log, saying exactly what was not verifiable.

        The two tables get different sentences on purpose. "Polish could not
        import this file" and "Polish recorded nothing about this function" are
        different problems with different fixes, and a refusal that blurred them
        would send a developer looking in the wrong place.
        """
        if self.eligible:
            return ""
        if self.creates_only:
            return (
                "refused Tier 1: it only creates files and changes none that "
                "exist, which is code generation rather than refactoring "
                "(SPEC §1.2)"
            )
        if self.demoted:
            return (
                f"refused Tier 1: {_listed(self.demoted)} "
                f"{'was' if len(self.demoted) == 1 else 'were'} demoted by the "
                "tier probe and could not be imported, so nothing here was "
                "characterized to verify a change against"
            )
        if not self.blocking:
            return (
                "refused Tier 1: nothing it touches was characterized, so no "
                "recorded behaviour exists to verify the change against"
            )
        return (
            f"refused Tier 1: {_listed(self.blocking)} "
            f"{'has' if len(self.blocking) == 1 else 'have'} no surviving "
            "characterization oracle, and a change is tiered by the lowest "
            "coverage among the symbols it touches"
        )


def resolve(
    root: Path, change: Proposed, coverage: Coverage, verdict: TierVerdict
) -> Eligibility:
    """Tier `change` against both tables, over everything it could disturb.

    Deliberately wider than the change's own `symbols`. A change rewrites whole
    files, and the agent's account of which symbols it will disturb is a promise
    Polish cannot check — so every symbol in every path it touches is resolved,
    and the declared symbols are added on top in case the agent named something
    in a file it did not list.

    The probe's ceiling is applied to the same paths, and it is checked *first*
    because it is the stronger statement: a file that could not be imported has
    no trustworthy coverage record either, so reporting the missing oracles
    would be reporting a symptom.
    """
    existing = [
        relative for relative in change.paths if (Path(root) / relative).is_file()
    ]
    if not existing:
        # Every declared path is new, so the change moves no code out of
        # anywhere — it only adds. That is not a refactor, it is generation, and
        # SPEC §1.2 is explicit that Polish "only touches code that already
        # exists". A real extraction always declares the file it takes the code
        # *from*, which is what tiers it; a queue entry that declares only its
        # destination has not described an extraction at all.
        return Eligibility(tier=Tier.ZERO, creates_only=True)

    # Only the paths that exist. A path the probe never saw takes the ceiling
    # (gate 3 is demote-only, so absence of evidence cannot demote), which is
    # what keeps a not-yet-created file from holding an extraction down here as
    # well as in the coverage half below.
    demoted = tuple(
        sorted(path for path in existing if verdict.tier_of(path) is Tier.ZERO)
    )

    touched = set(change.symbols)
    for relative in existing:
        touched.update(symbol.name for symbol in coverage.paths.get(relative, ()))

    blocking = tuple(
        sorted(name for name in touched if coverage.tier_of(name) is Tier.ZERO)
    )
    # An empty set of symbols resolves to Tier 0 through `Coverage.tier_for`, so
    # "existing files Polish has no record of" needs no branch of its own:
    # downward resolution is already the rule, in the one place that states it.
    return Eligibility(
        tier=min(verdict.tier_for(existing), coverage.tier_for(touched)),
        blocking=blocking,
        demoted=demoted,
        covered=len(touched) - len(blocking),
    )


def _listed(names: tuple[str, ...], limit: int = 3) -> str:
    shown = ", ".join(names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")
