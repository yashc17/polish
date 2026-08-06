"""The regression floor: precision and recall of `scan()` over real repos.

Every other test in this suite says what the algorithm does on a case someone
thought of. This one says how well it does on repositories nobody wrote it for.
"""

from __future__ import annotations

import pytest

from corpus_support import (
    PRECISION_FLOOR,
    RECALL_FLOOR,
    Manifest,
    Measurement,
    format_report,
    load_manifests,
    pooled,
)
from polish.firstparty import NotAPythonProject, scan

MANIFESTS = load_manifests()
SCANNABLE = [m for m in MANIFESTS if not m.expect_error]


@pytest.fixture(scope="session")
def measurements(tmp_path_factory) -> list[Measurement]:
    results = []
    for manifest in SCANNABLE:
        root = manifest.materialise(tmp_path_factory.mktemp(manifest.name))
        results.append(
            Measurement(
                name=manifest.name,
                labelled=manifest.first_party,
                predicted=frozenset(scan(root).files),
            )
        )
    return results


@pytest.mark.parametrize("manifest", SCANNABLE, ids=lambda m: m.name)
def test_no_repo_falls_below_the_floor(manifest: Manifest, measurements):
    measured = next(m for m in measurements if m.name == manifest.name)

    assert measured.precision >= PRECISION_FLOOR, (
        f"{manifest.name}: precision {measured.precision:.4f} below "
        f"{PRECISION_FLOOR}. Polish would refactor code the corpus says is not "
        f"first-party:\n  " + "\n  ".join(sorted(measured.false_positives)[:20])
    )
    assert measured.recall >= RECALL_FLOOR, (
        f"{manifest.name}: recall {measured.recall:.4f} below {RECALL_FLOOR}. "
        f"Polish would skip real source:\n  "
        + "\n  ".join(sorted(measured.false_negatives)[:20])
    )


def test_the_corpus_as_a_whole_holds_the_floor(measurements, capsys):
    overall = pooled(measurements)

    with capsys.disabled():
        print("\n" + format_report(measurements))

    assert overall.precision >= PRECISION_FLOOR
    assert overall.recall >= RECALL_FLOOR


@pytest.mark.parametrize(
    "manifest", [m for m in MANIFESTS if m.expect_error], ids=lambda m: m.name
)
def test_repos_with_no_python_are_declined(manifest: Manifest, tmp_path):
    root = manifest.materialise(tmp_path)

    with pytest.raises(NotAPythonProject):
        scan(root)


def test_the_corpus_covers_the_case_the_spec_was_written_around():
    """SPEC §5 Q2 cites a repo that is 99.6% virtualenv. Keep one in the corpus.

    Without this the corpus could drift into a set of tidy library repos, and
    the metric would stop measuring the thing that motivated the ticket.
    """
    venv_heavy = [
        m
        for m in SCANNABLE
        if sum(1 for path in m.files if path.endswith(".py")) > 100
        and len(m.first_party) / sum(1 for p in m.files if p.endswith(".py")) < 0.05
    ]

    assert venv_heavy, (
        "no corpus repo has an overwhelming majority of non-first-party .py "
        "files; the virtualenv case is no longer covered"
    )
