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

## The seam left for #28

The probe's verdict is still per *repository* — #3 chose that deliberately while
question #4 was open — so the only per-change evidence that exists today is the
coverage table, and that is what `resolve` consumes.
[#28](https://github.com/yashc17/polish/issues/28) makes the verdict per path,
which adds a second and independent reason a change can be refused: a module
gate 3 could not even import is Tier 0 whatever the coverage table says about it.

The two answer different halves and neither subsumes the other. The probe would
supply a **ceiling** ("this file could not be imported"); coverage supplies the
**floor by evidence** ("no oracle survived for this symbol"). So #28 lands here
as one more term rather than a rewrite: `resolve` takes the verdict, and `tier`
becomes `min(verdict.tier_for(change.paths), <what is computed below>)`. The
refusal line, the blocking list and the loop that consumes them are all
unchanged — which is the reason for writing the rule as a `min` over independent
evidence instead of a chain of conditions.

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
from polish.probe import Tier

from ._agent import Proposed


@dataclass(frozen=True)
class Eligibility:
    """Whether a proposed change may be made at Tier 1, and why not when not."""

    tier: Tier
    #: The symbols that held the tier down, worst first. Named rather than
    #: counted, on the same principle as the quarantine list: a developer whose
    #: dedup was refused is owed the symbol that refused it.
    blocking: tuple[str, ...] = ()
    #: How many symbols the change touches that do have a surviving oracle.
    #: Carried into the commit message as the evidence behind the tier claim.
    covered: int = 0
    #: True when the change touches no file that exists — see `resolve`.
    creates_only: bool = False

    @property
    def eligible(self) -> bool:
        return self.tier is Tier.ONE

    def refusal(self) -> str:
        """One line for the run log, saying exactly what was not verifiable."""
        if self.eligible:
            return ""
        if self.creates_only:
            return (
                "refused Tier 1: it only creates files and changes none that "
                "exist, which is code generation rather than refactoring "
                "(SPEC §1.2)"
            )
        if not self.blocking:
            return (
                "refused Tier 1: nothing it touches was characterized, so no "
                "recorded behaviour exists to verify the change against"
            )
        shown = ", ".join(self.blocking[:3])
        more = f" and {len(self.blocking) - 3} more" if len(self.blocking) > 3 else ""
        return (
            f"refused Tier 1: {shown}{more} "
            f"{'has' if len(self.blocking) == 1 else 'have'} no surviving "
            "characterization oracle, and a change is tiered by the lowest "
            "coverage among the symbols it touches"
        )


def resolve(root: Path, change: Proposed, coverage: Coverage) -> Eligibility:
    """Tier `change` against `coverage`, over everything it could disturb.

    Deliberately wider than the change's own `symbols`. A change rewrites whole
    files, and the agent's account of which symbols it will disturb is a promise
    Polish cannot check — so every symbol in every path it touches is resolved,
    and the declared symbols are added on top in case the agent named something
    in a file it did not list.
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

    touched = set(change.symbols)
    for relative in existing:
        touched.update(symbol.name for symbol in coverage.paths.get(relative, ()))

    if not touched:
        # Existing files, but nothing in them Polish has a coverage record for.
        # Downward resolution, unchanged: no record is not the same as no
        # problem, so an empty set of symbols is Tier 0 rather than vacuously
        # Tier 1.
        return Eligibility(tier=Tier.ZERO)

    blocking = tuple(
        sorted(name for name in touched if coverage.tier_of(name) is Tier.ZERO)
    )
    return Eligibility(
        tier=coverage.tier_for(touched),
        blocking=blocking,
        covered=len(touched) - len(blocking),
    )
