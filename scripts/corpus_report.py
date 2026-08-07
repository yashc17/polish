#!/usr/bin/env python3
"""Print both corpus metrics: detection precision/recall, and tier accuracy.

The same numbers the regression tests enforce, without needing to read a test
failure to see them. The two floors are the same product decision applied to
two components — a file wrongly called first-party and a repository wrongly
promoted to Tier 1 both mean Polish acting confidently on something it cannot
account for. Run from the repo root:

    python scripts/corpus_report.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))

from corpus_support import (  # noqa: E402
    PRECISION_FLOOR,
    RECALL_FLOOR,
    Measurement,
    format_report,
    load_manifests,
    pooled,
)
from polish.firstparty import NotAPythonProject, scan  # noqa: E402
from polish.probe import probe  # noqa: E402
from probe_corpus_support import (  # noqa: E402
    FALSE_TIER_ONE_CEILING,
    TierMeasurement,
    format_tier_report,
    load_probe_manifests,
)


def detection() -> int:
    measurements = []
    declined = []
    for manifest in load_manifests():
        with TemporaryDirectory() as directory:
            root = manifest.materialise(Path(directory))
            try:
                predicted = frozenset(scan(root).files)
            except NotAPythonProject as error:
                declined.append((manifest.name, str(error).splitlines()[0]))
                continue
        measurements.append(
            Measurement(
                name=manifest.name,
                labelled=manifest.first_party,
                predicted=predicted,
            )
        )

    print(format_report(measurements))
    if declined:
        print("\ndeclined (expected):")
        for name, reason in declined:
            print(f"  {name}: {reason}")

    overall = pooled(measurements)
    if overall.precision < PRECISION_FLOOR or overall.recall < RECALL_FLOOR:
        print("\nBELOW FLOOR", file=sys.stderr)
        return 1
    return 0


def tiering() -> int:
    """Replay the probe corpus. Offline: no venv is built and nothing executes."""
    measurements = []
    for manifest in load_probe_manifests():
        with TemporaryDirectory() as directory:
            root = manifest.materialise(Path(directory))
            verdict = probe(root, venv=root / ".absent", runner=manifest.replay())
        measurements.append(
            TierMeasurement(
                name=manifest.name,
                expected=manifest.expected,
                observed=verdict.tier,
            )
        )

    print(format_tier_report(measurements))
    promoted = [row.name for row in measurements if row.false_tier_one]
    if len(promoted) > FALSE_TIER_ONE_CEILING:
        print(f"\nFALSE TIER 1: {', '.join(promoted)}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    print("first-party detection")
    print("=" * 66)
    failed = detection()
    print("\n\ntier probe")
    print("=" * 66)
    return failed or tiering()


if __name__ == "__main__":
    sys.exit(main())
