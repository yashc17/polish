#!/usr/bin/env python3
"""Print precision and recall of `scan()` over the committed corpus.

The same numbers the regression test enforces, without needing to read a test
failure to see them. Run from the repo root:

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


def main() -> int:
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


if __name__ == "__main__":
    sys.exit(main())
