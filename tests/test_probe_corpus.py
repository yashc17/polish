"""The regression floor: what the probe says about repositories nobody wrote it for.

`test_probe.py` says what the gates do on cases someone thought of. This one
replays five real repositories — a healthy library, a second healthy library
with example apps in it, a repository needing API keys, one needing a GPU, and
one with no tests at all — and holds the line that matters: **no repository is
ever wrongly promoted to Tier 1.**
"""

from __future__ import annotations

import pytest

from polish.probe import Tier, probe
from probe_corpus_support import (
    FALSE_TIER_ONE_CEILING,
    ProbeManifest,
    TierMeasurement,
    format_tier_report,
    load_probe_manifests,
)

MANIFESTS = load_probe_manifests()


@pytest.fixture(scope="session")
def measurements(tmp_path_factory) -> list[TierMeasurement]:
    results = []
    for manifest in MANIFESTS:
        root = manifest.materialise(tmp_path_factory.mktemp(manifest.name))
        verdict = probe(root, venv=root / ".absent", runner=manifest.replay())
        results.append(
            TierMeasurement(
                name=manifest.name,
                expected=manifest.expected,
                observed=verdict.tier,
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
    """
    for row in measurements:
        if row.observed is Tier.ONE:
            assert row.expected is Tier.ONE, f"{row.name} was promoted unlabelled"
