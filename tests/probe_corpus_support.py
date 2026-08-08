"""Loading, replaying and scoring the hand-labelled tier-probe corpus.

The floor here is the mirror of the detection corpus's precision floor, and for
the same reason. **Zero false Tier 1**, always: a repository wrongly promoted
gets characterization tests generated from execution that never happened, and
then ships changes carrying the claim "verified against recorded behaviour"
(SPEC §3.1). A false Tier 0 costs a night of deeper refactoring and nothing
else. The errors are not symmetric, so the thresholds are not either.

**What a replay proves, and what it does not.** Each manifest holds a real
repository's shape plus what the probe's commands really did on a real machine,
recorded once by `scripts/capture_probe_corpus.py`. Replaying that exercises
every gate's logic, the batching and attribution in gate 3, and the whole
downward-resolution ladder — against repositories nobody wrote Polish for. It
cannot prove that the probe executes anything at all: a runner mutated into a
no-op would replay identically. `tests/test_probe.py` owns that claim, with
repositories that really install and really run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from corpus_support import CORPUS_DIR
from polish.probe import IMPORT_SMOKE, PIP_INSTALL, PYTEST, Command, Completed, Tier

PROBE_DIR = CORPUS_DIR / "probe"

#: A repository promoted to Tier 1 that should not have been. No tolerance:
#: this is issue #12's own acceptance criterion and SPEC §4.3.
FALSE_TIER_ONE_CEILING = 0


class MissingRecording(RuntimeError):
    """The probe issued a command the manifest has no answer for.

    Raised rather than defaulted. A default would be an invented outcome, and an
    invented outcome that happens to be a success is a fabricated Tier 1 — so
    corpus drift has to be loud, and the fix is to re-capture the manifest.
    """


@dataclass(frozen=True)
class ProbeManifest:
    name: str
    note: str
    label_rule: dict
    recorded: dict
    files: dict[str, str]

    @property
    def expected(self) -> Tier:
        return Tier(self.label_rule["expected_tier"])

    def materialise(self, root: Path) -> Path:
        for relative, contents in self.files.items():
            target = root / relative
            if relative.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents)
        return root

    def replay(self):
        """A `Runner` that answers from the recording instead of executing.

        Keyed by what a command *means* rather than by its argv. Raw argv keys
        would depend on `_gates.BATCH` and on module ordering, so changing
        either would turn the whole corpus into cache misses that quietly
        default to something.
        """
        recorded = self.recorded
        unimportable = frozenset(recorded.get("unimportable", ()))

        def runner(command: Command) -> Completed:
            if command.kind == PIP_INSTALL:
                if command.tail == ("pytest",):
                    return _pytest_availability(recorded)
                return _replay(recorded, "install", self.name)
            if command.kind == PYTEST:
                return _replay(recorded, "pytest", self.name)
            if command.kind == IMPORT_SMOKE:
                if command.modules == ("pytest",):
                    return _pytest_availability(recorded)
                return _replay_imports(command.modules, unimportable)
            return _replay(recorded, "entrypoint", self.name)

        return runner


def _pytest_availability(recorded: dict) -> Completed:
    if recorded.get("pytest_available"):
        return Completed(code=0, output="")
    return Completed(code=1, output="ModuleNotFoundError: No module named 'pytest'")


def _replay_imports(
    modules: tuple[str, ...], unimportable: frozenset[str]
) -> Completed:
    """Python stops at the first bad name, so the batch reports only that one."""
    failing = [name for name in modules if name in unimportable]
    if not failing:
        return Completed(code=0, output="")
    return Completed(code=1, output=f"ImportError while importing {failing[0]}")


def _replay(recorded: dict, key: str, name: str) -> Completed:
    entry = recorded.get(key)
    if entry is None:
        raise MissingRecording(
            f"{name}: the probe ran a {key} command that was not recorded when "
            f"this manifest was captured. Re-capture it with "
            f"scripts/capture_probe_corpus.py rather than guessing an outcome."
        )
    return Completed(
        code=entry["code"],
        output=entry.get("output", ""),
        timed_out=entry.get("timed_out", False),
    )


@dataclass(frozen=True)
class TierMeasurement:
    name: str
    expected: Tier
    observed: Tier
    #: The paths the verdict actually resolves to Tier 1, and the ones gate 3
    #: held back. Since #28 the verdict is per path, so the hand label — one
    #: tier for one repository, stated by a human — is measured against the
    #: *ceiling*, and these two carry the granularity the ceiling summarises.
    #: Without them, a demotion that quietly stopped happening would still score
    #: as a correct row.
    tier_one_paths: tuple[str, ...] = ()
    demoted_paths: tuple[str, ...] = ()

    @property
    def correct(self) -> bool:
        return self.expected is self.observed

    @property
    def false_tier_one(self) -> bool:
        """Labelled Tier 0, classified Tier 1. The error that must never happen.

        Asked of the ceiling *and* of every path beneath it: a per-path verdict
        makes false Tier 1 a per-path error, and a repository whose ceiling is
        Tier 0 while some file in it reads Tier 1 would be the same broken claim
        wearing finer granularity.
        """
        return self.expected is Tier.ZERO and (
            self.observed is Tier.ONE or bool(self.tier_one_paths)
        )

    @property
    def false_tier_zero(self) -> bool:
        return self.expected is Tier.ONE and self.observed is Tier.ZERO


def load_probe_manifests() -> list[ProbeManifest]:
    manifests = [_load(path) for path in sorted(PROBE_DIR.glob("*.json"))]
    if not manifests:
        raise RuntimeError(f"no probe manifests found in {PROBE_DIR}")
    return manifests


def _load(path: Path) -> ProbeManifest:
    raw = json.loads(path.read_text())
    # A manifest may carry its own tree, or name a detection manifest to reuse.
    # Reusing keeps one shape per repository: two copies of `requests` that had
    # drifted apart would make it unclear which one either corpus scored.
    files = raw.get("files")
    if files is None:
        shape = json.loads((CORPUS_DIR / f"{raw['shape']}.json").read_text())
        files = shape["files"]
    return ProbeManifest(
        name=raw["name"],
        note=raw["note"],
        label_rule=raw["label_rule"],
        recorded=raw["recorded"],
        files=files,
    )


def format_tier_report(measurements: list[TierMeasurement]) -> str:
    header = (
        f"{'repo':<14}{'labelled':>10}{'ceiling':>9}{'tier 1':>8}{'demoted':>9}"
        "   verdict"
    )
    lines = [header, "-" * len(header)]
    for row in sorted(measurements, key=lambda m: m.name):
        if row.false_tier_one:
            verdict = "FALSE TIER 1"
        elif row.false_tier_zero:
            verdict = "false Tier 0"
        else:
            verdict = "ok"
        # `probed` became two numbers when the verdict became per path (#28):
        # the ceiling the label is scored against, and how many paths actually
        # reached it. A repository can now be a correct row and still have most
        # of itself held at Tier 0, which is the whole point of the change.
        lines += [
            f"{row.name:<14}{row.expected!s:>10}{row.observed!s:>9}"
            f"{len(row.tier_one_paths):>8}{len(row.demoted_paths):>9}   {verdict}"
        ]

    lines += ["", "confusion matrix (rows labelled, columns the probed ceiling)", ""]
    lines += [f"{'':<14}{'probed 0':>10}{'probed 1':>10}"]
    for expected in (Tier.ZERO, Tier.ONE):
        counts = [
            sum(
                1
                for row in measurements
                if row.expected is expected and row.observed is observed
            )
            for observed in (Tier.ZERO, Tier.ONE)
        ]
        lines += [f"{f'labelled {expected}':<14}{counts[0]:>10}{counts[1]:>10}"]

    false_ones = sum(1 for row in measurements if row.false_tier_one)
    false_zeros = sum(1 for row in measurements if row.false_tier_zero)
    lines += [
        "",
        f"false Tier 1: {false_ones}   (ceiling: {FALSE_TIER_ONE_CEILING} — a "
        "false Tier 1 attaches a trust claim to nothing)",
        f"false Tier 0: {false_zeros}   (no ceiling — a missed night, not a "
        "broken repository)",
    ]
    return "\n".join(lines)
