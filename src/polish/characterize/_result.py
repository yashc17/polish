"""What a characterization produces, and the policy that bounds producing it.

Kept apart from the machinery that fills these in, for the same reason
`probe._verdict` is: everything downstream of #13 — the Tier 1 transform loop in
#15, the per-symbol tier rule from question #4 — consumes these types and
nothing else. They are the interface; tracing and replaying are details that
could be replaced without a caller noticing.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, fields
from functools import cached_property
from pathlib import Path

from polish.probe import Tier

#: How many paths the run log itemises before summarising the rest. The full
#: table is always on `Coverage`; this bounds the log, not the data.
DETAIL = 12


class Misconfigured(Exception):
    """Raised when `[characterize]` in `polish.toml` cannot be honoured.

    Loud rather than silently defaulted: a user who wrote `baseline_runs = 1`
    meaning "be quick" has disabled the flake baseline entirely, and a tool that
    quietly substituted 3 would be lying about what it ran.
    """


@dataclass(frozen=True)
class Policy:
    """The numbers issue #5 settled, and the time they cost.

    `baseline_runs` is N and `retries` is M, and both are here rather than
    inline because issue #13 requires them configurable: they are the dial
    between a night that catches flakes and a night that finishes. `replays` is
    the stricter rule for generated oracles — the same 3, deliberately not
    shared with N, because the two would diverge the moment anyone tuned one.
    """

    #: Runs of the repository's own suite before any edit exists to bias them.
    baseline_runs: int = 3
    #: Extra whole-suite runs before a failure during the run is believed.
    retries: int = 2
    #: Replays of each generated oracle against unmodified code.
    replays: int = 3

    #: Recorded calls kept per symbol. More is not better: the fourth recording
    #: of the same function rarely characterises anything the first three did
    #: not, and every one of them is replayed three times.
    samples_per_symbol: int = 3
    #: Longest `repr` a recorded value may have. A 4kB argument list in a
    #: generated test is not a test anyone will read.
    value_chars: int = 200
    #: How deep a container may nest before it stops being reconstructible.
    value_depth: int = 4
    #: Total recorded calls, across every symbol.
    samples: int = 2000

    suite_seconds: float = 600.0
    #: Tracing costs several times what running costs, so this is not
    #: `suite_seconds`. A repository whose suite takes four minutes is a
    #: repository whose traced suite may take twenty.
    trace_seconds: float = 1800.0
    oracle_seconds: float = 300.0
    #: The ceiling on everything above. The 3x baseline alone triples suite
    #: runtime before a single edit is attempted, which on a large repository is
    #: the dominant cost of the night — this is where that gets bounded.
    total_seconds: float = 3600.0

    def __post_init__(self) -> None:
        if self.baseline_runs < 2:
            raise Misconfigured(
                "characterize.baseline_runs must be at least 2: a baseline of "
                "one run has nothing to compare against and would quarantine "
                "nothing, which reads in the log exactly like a suite with no "
                "flakes in it.\n\n"
                "  [characterize]\n  baseline_runs = 3\n"
            )
        if self.replays < 2:
            raise Misconfigured(
                "characterize.replays must be at least 2: a generated oracle "
                "replayed once has not been checked for variance, and issue #5 "
                "discards oracles rather than quarantining them precisely "
                "because a flaky oracle can pass a broken change.\n\n"
                "  [characterize]\n  replays = 3\n"
            )
        if self.retries < 0:
            raise Misconfigured("characterize.retries cannot be negative.")

    @classmethod
    def load(cls, root: Path) -> Policy:
        """Read `[characterize]` from `polish.toml`, or take the defaults."""
        section = _section(Path(root) / "polish.toml")
        known = {item.name for item in fields(cls)}
        unknown = sorted(set(section) - known)
        if unknown:
            raise Misconfigured(
                f"polish.toml [characterize] does not have {', '.join(unknown)}. "
                f"It has: {', '.join(sorted(known))}."
            )
        return cls(**{name: value for name, value in section.items() if name in known})


@dataclass(frozen=True)
class Quarantined:
    """One test excluded from tonight's verification, and what it did.

    The outcomes are carried rather than reduced to "flaky" because they are the
    evidence: `passed, failed, passed` and `passed, absent, absent` are both
    non-identical, but only the second one means the suite stopped collecting.
    """

    nodeid: str
    outcomes: tuple[str, ...]

    def note(self) -> str:
        return f"{self.nodeid} ({' / '.join(self.outcomes)})"


#: A test that is absent from a run did not merely not fail: collection stopped
#: reaching it, which is a way of breaking a suite and is treated as one.
ABSENT = "absent"

BROKEN = frozenset({"failed", ABSENT})

#: Worst-first, for reducing several runs of one test to the outcome the
#: baseline will hold it to.
SEVERITY = {"passed": 0, "skipped": 1, ABSENT: 2, "failed": 3}


@dataclass(frozen=True)
class Baseline:
    """The repository's suite, run N times before anything was edited.

    Authoritative by construction: it ran before an edit existed to bias it, so
    it can tell a pre-existing flake from one Polish just caused — which is the
    thing retrying alone can never do (issue #5).

    `observed` holds **every** test the baseline saw, quarantined ones included,
    each mapped to the best outcome it managed across the N runs. Recording the
    quarantined ones costs a little and buys the thing that makes criterion 5
    checkable: a quarantined test that passed twice and failed once has a
    baseline of `passed`, so it *would* be read as a regression when it fails
    during the run, and `trusts()` below is the only thing that stops it. A
    baseline that simply forgot those tests would give the same answer for the
    wrong reason, and no test could tell the two apart.
    """

    runs: int
    observed: tuple[tuple[str, str], ...] = ()
    quarantined: tuple[Quarantined, ...] = ()
    #: Why there is no baseline, when that is the answer. A baseline that could
    #: not run is not a baseline in which nothing was flaky.
    declined: str = ""

    @cached_property
    def quarantine(self) -> frozenset[str]:
        return frozenset(item.nodeid for item in self.quarantined)

    @cached_property
    def expected(self) -> dict[str, str]:
        return dict(self.observed)

    @property
    def trusted(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.observed if self.trusts(name))

    def trusts(self, nodeid: str) -> bool:
        """Whether a failure of `nodeid` is allowed to mean anything.

        The one rule that matters (issue #13, criterion 5): a quarantined test
        cannot drop a change. Every failure Polish acts on passes through here.
        """
        return nodeid not in self.quarantine

    def regressed(self, nodeid: str, outcome: str) -> bool:
        """Did this test get worse than the baseline says it was?

        Not "did it fail" — gate 2 promotes repositories whose suites already
        have failures in them ("128 passing" is the bar, not "all passing"), so
        holding every test to green would drop every change on a repository that
        was already red and blame Polish for it.
        """
        return (
            outcome in BROKEN
            and self.expected.get(nodeid) not in (None, "failed")
            and self.trusts(nodeid)
        )

    @property
    def usable(self) -> bool:
        return not self.declined


@dataclass(frozen=True)
class Oracle:
    """One generated characterization test that survived replay.

    `call` and `expected` are kept alongside the node id so that a reviewer
    reading the run log sees the claim itself — `add(1, 2)` was observed
    returning `3` — rather than only a test name.
    """

    symbol: str
    nodeid: str
    call: str
    expected: str


@dataclass(frozen=True)
class Discarded:
    """One generated test that did not survive, and why it did not.

    Discarded rather than quarantined, and the distinction is issue #5's:
    Polish owns these tests, so a non-deterministic one should not exist. A
    flaky test of the user's drops good work; a flaky oracle can *pass a broken
    change*, which is a false green on the exact claim the product sells.
    """

    symbol: str
    nodeid: str
    reason: str

    def note(self) -> str:
        return f"{self.nodeid} ({self.reason})"


@dataclass(frozen=True)
class SymbolCoverage:
    """What real execution and replay established about one symbol."""

    path: str
    module: str
    qualname: str
    executed: bool
    oracles: int

    @property
    def name(self) -> str:
        """The symbol's dotted name — the key everything else looks it up by."""
        return f"{self.module}.{self.qualname}"

    @property
    def tier(self) -> Tier:
        """Tier 1 only with a surviving oracle. Executed is not enough.

        Issue #5's downstream rule, stated as a property: code with no surviving
        oracle is not eligible for Tier 1 transforms and falls back to Tier 0.
        Without it, SPEC §3.9's commit message would claim "verified against
        recorded behaviour" for a change whose oracle was thrown away.
        """
        return Tier.ONE if self.oracles else Tier.ZERO


@dataclass(frozen=True)
class Coverage:
    """Per-symbol coverage — the input to the tiering rule, not a report of it.

    Question #4 settled tiering as **per symbol / per path**: an edit's tier is
    the *lowest* coverage tier among the symbols it touches. `tier_for` is that
    sentence, and the index below exists so it is a lookup rather than a
    reconstruction — #15 asks this question once per candidate edit.

    An unknown symbol is Tier 0, and so is an empty set of them. That is
    downward resolution (`probe`'s rule, unchanged here): a symbol Polish has no
    coverage record for is a symbol nothing characterised, which is exactly what
    Tier 0 means. It is also what makes the rule safe under the *observer's*
    stale-graph fallback — a caller that asks about symbols Polish has not seen
    gets caution, not silence.
    """

    symbols: tuple[SymbolCoverage, ...] = ()

    @cached_property
    def index(self) -> dict[str, SymbolCoverage]:
        return {symbol.name: symbol for symbol in self.symbols}

    @cached_property
    def paths(self) -> dict[str, tuple[SymbolCoverage, ...]]:
        grouped: dict[str, list[SymbolCoverage]] = {}
        for symbol in self.symbols:
            grouped.setdefault(symbol.path, []).append(symbol)
        return {path: tuple(found) for path, found in sorted(grouped.items())}

    def tier_of(self, name: str) -> Tier:
        found = self.index.get(name)
        return found.tier if found is not None else Tier.ZERO

    def tier_for(self, names: Iterable[str]) -> Tier:
        """The lowest tier among `names` — question #4's rule, verbatim."""
        tiers = [self.tier_of(name) for name in names]
        return min(tiers) if tiers else Tier.ZERO

    def tier_for_path(self, path: str) -> Tier:
        """The same rule for a whole file, which is what a `Change` is.

        A file with no symbols at all is Tier 0 rather than vacuously Tier 1:
        nothing in it was characterised, so nothing about it was verified.
        """
        return self.tier_for(symbol.name for symbol in self.paths.get(path, ()))

    @property
    def executed(self) -> int:
        return sum(1 for symbol in self.symbols if symbol.executed)

    @property
    def characterized(self) -> int:
        return sum(1 for symbol in self.symbols if symbol.tier is Tier.ONE)

    def lines(self) -> list[str]:
        """Per-path coverage, worst first, capped so a log stays a log."""
        rows = sorted(
            self.paths.items(),
            key=lambda item: (
                sum(1 for symbol in item[1] if symbol.tier is Tier.ONE),
                item[0],
            ),
        )
        shown = [
            f"  {path}: {len(found)} symbols, "
            f"{sum(1 for symbol in found if symbol.executed)} executed, "
            f"{sum(1 for symbol in found if symbol.tier is Tier.ONE)} tier 1"
            for path, found in rows[:DETAIL]
        ]
        if len(rows) > DETAIL:
            shown.append(f"  ... and {len(rows) - DETAIL} more paths")
        return shown


@dataclass(frozen=True)
class Verification:
    """What running the oracles and the suite said about one state of the tree.

    Only `failed` and `oracle_failures` can stop a change. `ignored` is the
    quarantine doing its job and is reported so that a reviewer can see it did:
    a suite whose flakes are invisible is a suite that looks like it agreed.
    """

    attempts: int
    failed: tuple[str, ...] = ()
    flaky: tuple[str, ...] = ()
    ignored: tuple[str, ...] = ()
    oracle_failures: tuple[str, ...] = ()
    declined: str = ""

    @property
    def passed(self) -> bool:
        return not self.declined and not self.failed and not self.oracle_failures

    def evidence(self) -> tuple[str, ...]:
        """The lines this belongs to a commit message as (SPEC §3.9)."""
        if self.declined:
            return (f"Verification: could not run — {self.declined}",)
        lines = [
            f"Suite: {'clean' if not self.failed else _count(self.failed, 'failure')}"
            f" over {_count_of(self.attempts, 'attempt')}",
            f"Oracles: {'passed' if not self.oracle_failures else 'FAILED'}",
        ]
        if self.flaky:
            lines.append(
                f"Retried and passed: {_count(self.flaky, 'test')} — "
                "not believed failed"
            )
        if self.ignored:
            lines.append(
                f"Quarantined and ignored: {_count(self.ignored, 'test')} — "
                "a flaky test cannot drop a change"
            )
        return tuple(lines)


@dataclass(frozen=True)
class Characterization:
    """Everything #13 establishes about a Tier 1 repository, before any edit."""

    baseline: Baseline
    coverage: Coverage = field(default_factory=Coverage)
    oracles: tuple[Oracle, ...] = ()
    discarded: tuple[Discarded, ...] = ()
    #: The scratch directory holding the harness and the surviving oracle
    #: files, so that verification can re-run them against an edited tree.
    workspace: Path | None = None
    #: Why there is nothing here, when that is the answer.
    declined: str = ""
    commands: tuple[str, ...] = ()

    def summary(self) -> tuple[str, ...]:
        """One fact per line, in the same register as the tier verdict."""
        lines = [
            _line(
                "baseline",
                f"{self.baseline.runs}x suite, "
                f"{len(self.baseline.trusted)} trusted, "
                f"{len(self.baseline.quarantined)} quarantined"
                + (f" — {self.baseline.declined}" if self.baseline.declined else ""),
            )
        ]
        # Named, not counted (issue #13, criterion 4). A flaky suite is
        # information the developer wants, and it explains why some code got
        # less attention tonight than they expected.
        lines += [f"  ~ {item.note()}" for item in self.baseline.quarantined]
        lines.append(
            _line(
                "oracles",
                f"{len(self.oracles)} kept, {len(self.discarded)} discarded"
                + (f" — {self.declined}" if self.declined else ""),
            )
        )
        lines += [f"  - {item.note()}" for item in self.discarded]
        lines.append(
            _line(
                "coverage",
                f"{self.coverage.characterized} of {len(self.coverage.symbols)} "
                f"symbols at tier 1, {self.coverage.executed} executed",
            )
        )
        lines += self.coverage.lines()
        lines += [_line("ran", command) for command in self.commands]
        return tuple(lines)


def _section(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    section = data.get("characterize", {})
    return section if isinstance(section, dict) else {}


def _line(label: str, value: str) -> str:
    return f"{label:<10}{value}"


def _count(items: Sequence[str], noun: str) -> str:
    return _count_of(len(items), noun)


def _count_of(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")
