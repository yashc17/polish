"""Loading, measuring and reporting the hand-labelled corpus.

The floors below are the product decision this module exists to enforce.
Precision is held at 1.0 because the two errors are not symmetric: missing a
source file costs a refactor Polish could have made, while including a vendored
one means Polish edits code the user does not own and cannot review — an
unbounded blast radius in a product whose entire pitch is that you can leave it
unattended. Recall is allowed to slip because a missed file is a missed
opportunity, not a broken repo.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

CORPUS_DIR = Path(__file__).parent / "corpus"

#: Any file wrongly called first-party fails the build. No tolerance.
PRECISION_FLOOR = 1.0

#: At most one file in twenty may be missed.
RECALL_FLOOR = 0.95


@dataclass(frozen=True)
class Manifest:
    name: str
    note: str
    label_rule: dict
    expect_error: bool
    first_party: frozenset[str]
    files: dict[str, str]

    def materialise(self, root: Path) -> Path:
        for relative, contents in self.files.items():
            target = root / relative
            if relative.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents)
        return root


@dataclass(frozen=True)
class Measurement:
    name: str
    labelled: frozenset[str]
    predicted: frozenset[str]

    @property
    def false_positives(self) -> frozenset[str]:
        """Predicted first-party, but hand-labelled as not. The dangerous error."""
        return self.predicted - self.labelled

    @property
    def false_negatives(self) -> frozenset[str]:
        return self.labelled - self.predicted

    @property
    def true_positives(self) -> frozenset[str]:
        return self.predicted & self.labelled

    @property
    def precision(self) -> float:
        # A prediction of nothing makes no false claims, so it is perfectly
        # precise. Recall is what catches an empty answer.
        if not self.predicted:
            return 1.0
        return len(self.true_positives) / len(self.predicted)

    @property
    def recall(self) -> float:
        if not self.labelled:
            return 1.0
        return len(self.true_positives) / len(self.labelled)


def load_manifests() -> list[Manifest]:
    manifests = [_load(path) for path in sorted(CORPUS_DIR.glob("*.json"))]
    if not manifests:
        raise RuntimeError(f"no corpus manifests found in {CORPUS_DIR}")
    return manifests


def _load(path: Path) -> Manifest:
    raw = json.loads(path.read_text())
    return Manifest(
        name=raw["name"],
        note=raw["note"],
        label_rule=raw["label_rule"],
        expect_error=raw.get("expect_error", False),
        first_party=frozenset(raw["first_party"]),
        files=raw["files"],
    )


def pooled(measurements: list[Measurement]) -> Measurement:
    """Micro-average: pool every file across every repo into one measurement.

    Pooling rather than averaging per-repo rates keeps a 3228-file repo from
    being outvoted by a 27-file one.
    """
    return Measurement(
        name="ALL",
        labelled=frozenset(
            f"{m.name}/{path}" for m in measurements for path in m.labelled
        ),
        predicted=frozenset(
            f"{m.name}/{path}" for m in measurements for path in m.predicted
        ),
    )


def format_report(measurements: list[Measurement]) -> str:
    rows = [*measurements, pooled(measurements)]
    header = (
        f"{'repo':<14}{'labelled':>9}{'found':>7}{'TP':>6}{'FP':>5}{'FN':>5}"
        f"{'precision':>11}{'recall':>9}"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        if row.name == "ALL":
            lines.append("-" * len(header))
        lines.append(
            f"{row.name:<14}{len(row.labelled):>9}{len(row.predicted):>7}"
            f"{len(row.true_positives):>6}{len(row.false_positives):>5}"
            f"{len(row.false_negatives):>5}"
            f"{row.precision:>11.4f}{row.recall:>9.4f}"
        )
    lines.append("")
    lines.append(f"floors: precision >= {PRECISION_FLOOR}  recall >= {RECALL_FLOOR}")
    return "\n".join(lines)
