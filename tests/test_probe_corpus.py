"""The regression floor: what the probe says about repositories nobody wrote it for.

`test_probe.py` says what the gates do on cases someone thought of. This one
replays five real repositories — a healthy library, a second healthy library
with example apps in it, a repository needing API keys, one needing a GPU, and
one with no tests at all — and holds the line that matters: **no repository is
ever wrongly promoted to Tier 1.**
"""

from __future__ import annotations

import pytest

from polish.firstparty import scan
from polish.probe import Tier, probe
from probe_corpus_support import (
    FALSE_TIER_ONE_CEILING,
    ProbeManifest,
    TierMeasurement,
    format_tier_report,
    load_probe_manifests,
)

MANIFESTS = load_probe_manifests()

#: Three real `flask` modules that gate 3 demotes for a reason belonging to
#: Polish rather than to flask: `src/flask/sansio/` is a namespace subpackage,
#: `_discovery._address` stops walking outwards at the missing `__init__.py`,
#: and the resulting `import app` fails where `import flask.sansio.app` would
#: not. Tracked in [#34](https://github.com/yashc17/polish/issues/34).
#:
#: Written down here rather than waved through, because a per-path verdict is
#: what made it visible at all — under the per-repository rule flask was Tier 0
#: anyway and three misaddressed modules changed nothing. Pinning the exact
#: paths means fixing #34 fails this test, which is the correct way for a known
#: hole to be closed: loudly.
NAMESPACE_HOLE = "src/flask/sansio/"


@pytest.fixture(scope="session")
def measurements(tmp_path_factory) -> list[TierMeasurement]:
    results = []
    for manifest in MANIFESTS:
        root = manifest.materialise(tmp_path_factory.mktemp(manifest.name))
        first_party = scan(root).files
        verdict = probe(root, venv=root / ".absent", runner=manifest.replay())
        results.append(
            TierMeasurement(
                name=manifest.name,
                expected=manifest.expected,
                observed=verdict.ceiling,
                # Asked file by file rather than read off the ceiling, because
                # since #28 that is where the answer lives. `tier_of` is the
                # method #15 calls once per candidate change, so measuring
                # through it measures what the product will actually use.
                tier_one_paths=tuple(
                    path for path in first_party if verdict.tier_of(path) is Tier.ONE
                ),
                demoted_paths=tuple(item.path for item in verdict.demoted),
            )
        )
    return results


def test_no_repo_is_wrongly_promoted(measurements, capsys):
    """Zero false Tier 1. Issue #12's own criterion, and SPEC §4.3.

    The asymmetry is the product. A repository wrongly held at Tier 0 loses a
    night of deeper refactoring. A repository wrongly promoted gets
    characterization tests generated from execution that never happened, and
    every change it ships carries the claim "verified against recorded
    behaviour" with nothing behind it.
    """
    with capsys.disabled():
        print("\n" + format_tier_report(measurements))

    promoted = [row.name for row in measurements if row.false_tier_one]

    assert len(promoted) <= FALSE_TIER_ONE_CEILING, (
        "repositories labelled Tier 0 were classified Tier 1: "
        + ", ".join(promoted)
        + ". A false Tier 1 attaches a trust claim to nothing."
    )


@pytest.mark.parametrize("manifest", MANIFESTS, ids=lambda m: m.name)
def test_every_manifest_carries_a_stated_reason(manifest: ProbeManifest):
    """A label with no reasoning next to it cannot be checked by a reviewer.

    The detection corpus enforces the same discipline through `label_rule`:
    labels are stated by a human and justified in prose, never derived from the
    code under test, because a label derived from `probe()` would make the
    confusion matrix a tautology.
    """
    assert manifest.label_rule["reason"].strip()
    assert manifest.note.strip()
    assert manifest.expected in (Tier.ZERO, Tier.ONE)


def test_the_corpus_covers_the_four_cases_the_ticket_requires():
    """API keys, a GPU, no tests, a healthy suite — issue #12's criterion 4.

    Without this the corpus could drift into a set of tidy library repositories,
    and it would stop measuring the thing that motivated the tiering decision:
    SPEC §3.1 says the repositories that need Polish most are exactly the ones a
    characterization-only design hard-fails on.
    """
    notes = " ".join(manifest.note.lower() for manifest in MANIFESTS)

    for case in ("api key", "gpu", "no test", "healthy"):
        assert case in notes, (
            f"no corpus repository is documented as the {case!r} case; the "
            "corpus no longer covers what issue #12 asks it to."
        )


def test_a_repo_that_executes_nothing_is_never_tier_one(measurements):
    """The one rule with no exception, checked against the labels themselves.

    Every Tier 1 in this corpus must be a repository a human labelled Tier 1.
    Stated separately from the ceiling above so that deleting the ceiling does
    not delete the claim.

    Asked of every *path* as well as of the ceiling. Since #28 the verdict is
    per path, and a claim that only held at the coarser granularity would be a
    claim about a number nothing downstream reads: what #15 asks is
    `tier_of(path)`, one change at a time.
    """
    for row in measurements:
        if row.observed is Tier.ONE:
            assert row.expected is Tier.ONE, f"{row.name} was promoted unlabelled"
        if row.expected is Tier.ZERO:
            assert not row.tier_one_paths, (
                f"{row.name} is labelled Tier 0 and "
                f"{len(row.tier_one_paths)} of its files resolve to Tier 1: "
                + ", ".join(row.tier_one_paths[:5])
            )


def test_flask_is_no_longer_demoted_whole_by_its_example_apps(measurements):
    """#28's worked example, and the row the change exists to fix.

    485 tests pass and every `flask.*` module imports, but eight modules under
    `examples/` and `docs/` want celery, which flask does not declare. Under the
    per-repository verdict those eight demoted the library too, and this row was
    a false Tier 0 — labelled against the code deliberately, and left that way
    while question #4 was open.

    Both halves are asserted, because only together do they mean anything. The
    library reaching Tier 1 without the example apps still being held at Tier 0
    would not be this ticket delivered; it would be gate 3 stopping working.

    The `sansio` exception is a second thing this change surfaced rather than
    caused — see `NAMESPACE_HOLE` above and #34.
    """
    flask = next(row for row in measurements if row.name == "flask")

    assert flask.observed is Tier.ONE
    assert not flask.false_tier_zero
    assert "src/flask/app.py" in flask.tier_one_paths
    assert flask.demoted_paths, "gate 3 demoted nothing on a repository it should"
    assert all(
        path.startswith(("examples/", "docs/")) or path.startswith(NAMESPACE_HOLE)
        for path in flask.demoted_paths
    ), f"an unexpected module was demoted: {flask.demoted_paths}"
