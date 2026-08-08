"""The Tier 1 edit loop, against repositories that really execute.

Every repository below is built on disk, every suite run is a real subprocess in
a real virtualenv, and every "edit" is a real write. Only the *agent* is
substituted — `Scripted` below plays the part the Claude Agent SDK plays in
production, and it plays it by editing files, which is the whole of what the
protocol in `polish.tier1._agent` lets an agent do.

That substitution is the point rather than a shortcut. Issue #15's product is a
**policy** — verify, repair exactly once, then drop that change alone and carry
on — and a test that called a model would be measuring the model. What has to be
pinned here is that a broken change cannot ship, that it gets one repair attempt
and not two, and that its neighbours in the queue survive it. Every one of those
is a fact about the harness, and each is provable only if the edit really lands
on a disk and the verification really runs.

`BROKEN_SCALE` is the deliberately broken refactor the ticket asks for by name.
It is built to pass the repository's own suite and fail the recorded oracle,
because that is the case the product exists for: if the suite caught it, Tier 1
would not have needed to.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from polish import envelope, tier1
from polish.characterize import Policy, characterize
from polish.firstparty import scan
from polish.probe import Tier
from polish.tier1 import AgentError, Proposed

#: Smaller N and R. The subject here is the edit loop, not the flake policy.
FAST = Policy(baseline_runs=2, replays=2, retries=2)

PROJECT = '[project]\nname = "sample"\nversion = "0.1.0"\n'

#: Annotated, so the type check has something to catch. `scale` is the important
#: one: the suite asserts only its *length*, while the oracle records the whole
#: value — so an edit can be suite-clean and oracle-red, which is the only shape
#: of bug Tier 1 catches that Tier 0 could not.
MATHS = '''\
"""Deterministic by construction — every call here replays identically."""


def greet(name: str) -> str:
    return "hello " + name


def scale(values: list[int], factor: int = 2) -> list[int]:
    return [value * factor for value in values]


def total(values: list[int]) -> int:
    return sum(values)
'''

MATHS_SUITE = """\
from sample.maths import greet, scale, total


def test_greet():
    assert greet("polish") == "hello polish"


def test_scale():
    assert len(scale([1, 2])) == 2


def test_total():
    assert total([1, 2, 3]) == 6
"""

#: The deliberately broken refactor (issue #15, criterion 6). `scale` still
#: returns two elements, so `test_scale` passes exactly as before; the recorded
#: oracle says `scale([1, 2], 2)` was observed returning `[2, 4]`, and it no
#: longer does. Nothing but the characterization can see this.
BROKEN_SCALE = "return [value * factor + 1 for value in values]"
GOOD_SCALE = "return [value * factor for value in values]"

#: A change mypy rejects, to prove the type check is in the loop on its own.
MISTYPED_GREET = 'return "hello " + len(name)'

#: Non-deterministic, so its oracle is discarded and its symbols stay Tier 0 —
#: the fixture for the eligibility refusal (#5's rule, applied by #15).
DRIFT = '''\
"""Never reproduces, so no oracle survives replay."""

import time


def stamp() -> int:
    return time.time_ns()
'''

DRIFT_SUITE = """\
from sample.drift import stamp


def test_stamp():
    assert stamp() > 0
"""


# --- the agent, played by a script ---------------------------------------------


@dataclass
class Scripted:
    """An `Agent` whose edits are real writes to real files.

    Holds no opinion about whether an edit is good — it cannot, because the
    protocol gives it nowhere to express one. That is the property under test as
    much as it is a convenience: everything that decides whether a change ships
    is on the other side of this seam.
    """

    root: Path
    queue: tuple[Proposed, ...] = ()
    edits: dict[str, Callable[[Path], None]] = field(default_factory=dict)
    repairs: dict[str, Callable[[Path], None]] = field(default_factory=dict)
    #: Summaries whose `apply` raises, standing in for an agent that gave up.
    refuses: frozenset[str] = frozenset()
    applied: list[str] = field(default_factory=list)
    attempted: list[str] = field(default_factory=list)

    def propose(self, brief) -> tuple[Proposed, ...]:
        self.brief = brief
        return self.queue

    def apply(self, change: Proposed) -> None:
        self.applied.append(change.summary)
        if change.summary in self.refuses:
            raise AgentError("no idea how to do that")
        edit = self.edits.get(change.summary)
        if edit is not None:
            edit(self.root)

    def repair(self, failure) -> None:
        self.attempted.append(failure.change.summary)
        repair = self.repairs.get(failure.change.summary)
        if repair is not None:
            repair(self.root)


def rewrite(relative: str, before: str, after: str) -> Callable[[Path], None]:
    """An edit that swaps one line of one file — a real write, on real bytes."""

    def _edit(root: Path) -> None:
        target = root / relative
        source = target.read_text()
        assert before in source, f"{before!r} is not in {relative}"
        target.write_text(source.replace(before, after))

    return _edit


def create(relative: str, body: str) -> Callable[[Path], None]:
    def _edit(root: Path) -> None:
        (root / relative).write_text(body)

    return _edit


def extract_greeting(root: Path) -> None:
    """A real extraction: code leaves one file and arrives in a new one.

    Written out rather than composed from `rewrite`, because the shape is the
    point — an extraction that only *created* a file would not be an extraction,
    and `_eligibility` refuses exactly that.
    """
    (root / "sample" / "greeting.py").write_text('GREETING = "hello"\n')
    maths = root / "sample" / "maths.py"
    maths.write_text(
        "from sample.greeting import GREETING\n\n"
        + maths.read_text().replace(
            'return "hello " + name', 'return GREETING + " " + name'
        )
    )


def touch_other(relative: str) -> Callable[[Path], None]:
    def _edit(root: Path) -> None:
        target = root / relative
        target.write_text(target.read_text() + "\n# stray\n")

    return _edit


def change(summary: str, *paths: str, category: str = "simplify") -> Proposed:
    return Proposed(
        category=category,
        summary=summary,
        rationale=f"{summary}, preserving behaviour.",
        paths=paths,
    )


# --- fixtures -------------------------------------------------------------------


@pytest.fixture
def tier_one(make_repo, sandbox_venv):
    """Build a repository, characterize it for real, and run the Tier 1 loop."""

    def _run(agent_for, **extra: str):
        root = make_repo(
            {
                "pyproject.toml": PROJECT,
                "sample/__init__.py": "",
                "sample/maths.py": MATHS,
                "tests/test_maths.py": MATHS_SUITE,
                **extra,
            }
        )
        found = characterize(root, venv=sandbox_venv, policy=FAST)
        assert found.oracles, "the fixture repository must produce oracles"

        every = scan(root).files
        agent = agent_for(root)
        changes = list(
            tier1.refactor(
                root,
                [name for name in every if name.startswith("sample/")],
                characterization=found,
                venv=sandbox_venv,
                agent=agent,
                verify_against=every,
                policy=FAST,
            )
        )
        return root, agent, changes, found

    return _run


def kept(changes):
    return [item.proposed.summary for item in changes if item.kept]


def dropped(changes):
    return [item.proposed.summary for item in changes if not item.kept]


def find(changes, summary):
    return next(item for item in changes if item.proposed.summary == summary)


# --- a good change ships, and carries its evidence ------------------------------


def test_a_verified_change_is_kept_with_the_evidence_behind_it(tier_one):
    """Criterion 2, from the passing side, plus SPEC §3.9's commit message.

    The edit is genuinely behaviour-preserving — `sum` and a fold agree — so all
    three checks pass and the change ships with what verified it attached.
    """
    root, _, changes, _ = tier_one(
        lambda root: Scripted(
            root,
            queue=(change("fold total by hand", "sample/maths.py"),),
            edits={
                "fold total by hand": rewrite(
                    "sample/maths.py",
                    "return sum(values)",
                    "result = 0\n    for value in values:\n"
                    "        result += value\n    return result",
                )
            },
        )
    )

    assert kept(changes) == ["fold total by hand"]
    message = changes[0].message()
    assert "refactor(simplify): fold total by hand" in message
    assert "Tier: 1 (characterization)" in message
    assert "Repair attempts: 0" in message
    assert "Type check: clean" in message
    assert "Oracles:" in message
    # And the edit really is on the disk, which is what "kept" has to mean.
    assert "result += value" in (root / "sample" / "maths.py").read_text()


# --- all three checks are in the loop -------------------------------------------


def test_a_change_the_suite_cannot_see_is_still_caught_by_the_oracles(tier_one):
    """The claim the product sells, inside the loop that ships changes.

    `test_scale` asserts only the length, so it passes after this edit. The
    oracle recorded the value and does not. If the oracles were not in the loop,
    this change would ship.
    """
    root, _, changes, _ = tier_one(
        lambda root: Scripted(
            root,
            queue=(change("tighten scale", "sample/maths.py"),),
            edits={
                "tighten scale": rewrite("sample/maths.py", GOOD_SCALE, BROKEN_SCALE)
            },
        )
    )

    assert kept(changes) == []
    failure = changes[0].failure
    assert "sample.maths.scale([1, 2], 2) was observed returning" in failure
    assert "no longer does" in failure
    # Reverted: the file is byte-identical to the fixture.
    assert (root / "sample" / "maths.py").read_text() == MATHS


def test_a_change_that_breaks_the_existing_suite_is_dropped(tier_one):
    root, _, changes, _ = tier_one(
        lambda root: Scripted(
            root,
            queue=(change("rename the greeting", "sample/maths.py"),),
            edits={
                "rename the greeting": rewrite(
                    "sample/maths.py", 'return "hello " + name', 'return "hi " + name'
                )
            },
        )
    )

    assert kept(changes) == []
    assert "tests/test_maths.py::test_greet" in changes[0].failure


def test_a_change_that_only_the_type_check_can_see_is_dropped(tier_one):
    """The third check, on its own — and the cheap one runs first.

    mypy rejects this before a single test runs, which is the ordering
    `_verify` argues for: the expensive checks are spent on changes that might
    still ship.
    """
    root, _, changes, _ = tier_one(
        lambda root: Scripted(
            root,
            queue=(change("simplify the greeting", "sample/maths.py"),),
            edits={
                "simplify the greeting": rewrite(
                    "sample/maths.py", 'return "hello " + name', MISTYPED_GREET
                )
            },
        )
    )

    assert kept(changes) == []
    assert "The type check reports" in changes[0].failure
    assert (root / "sample" / "maths.py").read_text() == MATHS


# --- exactly one repair attempt --------------------------------------------------


def test_a_repairable_change_is_repaired_once_and_kept(tier_one):
    """Criterion 3, from the side where the repair pays for itself.

    "Dropping without repair discards work an obvious fix would have saved"
    (SPEC §3.10) — this is that fix, and the commit says it happened.
    """
    root, agent, changes, _ = tier_one(
        lambda root: Scripted(
            root,
            queue=(change("tidy scale", "sample/maths.py"),),
            edits={"tidy scale": rewrite("sample/maths.py", GOOD_SCALE, BROKEN_SCALE)},
            repairs={
                "tidy scale": rewrite("sample/maths.py", BROKEN_SCALE, GOOD_SCALE)
            },
        )
    )

    assert kept(changes) == ["tidy scale"]
    assert agent.attempted == ["tidy scale"]
    assert changes[0].repairs == 1
    assert "Repair attempts: 1" in changes[0].message()


def test_the_repair_attempt_is_exactly_one_never_two(tier_one):
    """Criterion 3, and the half a bound of "a few" would still satisfy.

    The repair here makes the change *differently* broken, so a loop with any
    bound above one would keep going. The agent is asked once, and once only.
    """
    root, agent, changes, _ = tier_one(
        lambda root: Scripted(
            root,
            queue=(change("tidy scale", "sample/maths.py"),),
            edits={"tidy scale": rewrite("sample/maths.py", GOOD_SCALE, BROKEN_SCALE)},
            repairs={
                "tidy scale": rewrite(
                    "sample/maths.py",
                    BROKEN_SCALE,
                    "return [value * factor + 2 for value in values]",
                )
            },
        )
    )

    assert agent.attempted == ["tidy scale"], "one attempt, not zero and not two"
    assert kept(changes) == []
    assert changes[0].repairs == 1
    assert "still failing after one repair attempt" in changes[0].evidence[0]
    assert (root / "sample" / "maths.py").read_text() == MATHS


# --- the headline: repair, fail, drop that change alone, keep going --------------


def test_a_broken_refactor_is_repaired_then_dropped_and_the_queue_continues(tier_one):
    """Criteria 4, 5, 6 and 7 in one run — the shape of a real night.

    Three changes: a good one, the deliberately broken one, and another good
    one. The broken one is repaired once, still fails, and is reverted **alone**
    — the changes either side of it are on disk and its own file is byte-
    identical to where it started. Its failure is in the log rather than
    summarised away, because a reviewer deciding whether to trust tomorrow night
    needs to see what tonight could not do.
    """
    root, agent, changes, _ = tier_one(
        lambda root: Scripted(
            root,
            queue=(
                change("fold total by hand", "sample/maths.py"),
                change("tidy scale", "sample/maths.py"),
                change(
                    "extract the greeting",
                    "sample/maths.py",
                    "sample/greeting.py",
                    category="extract",
                ),
            ),
            edits={
                "fold total by hand": rewrite(
                    "sample/maths.py", "return sum(values)", "return sum(list(values))"
                ),
                "tidy scale": rewrite("sample/maths.py", GOOD_SCALE, BROKEN_SCALE),
                "extract the greeting": extract_greeting,
            },
            repairs={
                "tidy scale": rewrite(
                    "sample/maths.py",
                    BROKEN_SCALE,
                    "return [value * factor + 3 for value in values]",
                )
            },
        )
    )

    assert kept(changes) == ["fold total by hand", "extract the greeting"]
    assert dropped(changes) == ["tidy scale"]

    # The queue continued: the change *after* the failure was attempted at all.
    assert agent.applied == ["fold total by hand", "tidy scale", "extract the greeting"]

    # Reverted alone. `scale` is back; the kept change in the same file is not.
    source = (root / "sample" / "maths.py").read_text()
    assert GOOD_SCALE in source
    assert BROKEN_SCALE not in source
    assert "return sum(list(values))" in source
    assert (root / "sample" / "greeting.py").exists()

    # Logged with its failure output, not merely counted.
    report = "\n".join(find(changes, "tidy scale").report())
    assert "still failing after one repair attempt" in report
    assert "was observed returning" in report


def test_one_failing_change_never_aborts_the_rest_of_the_queue(tier_one):
    """Criterion 7, over the failure the loop does not otherwise see.

    An agent that raises is not a change that failed verification — it is a
    change that never existed. It costs its own slot and nothing else.
    """
    _, agent, changes, _ = tier_one(
        lambda root: Scripted(
            root,
            queue=(
                change("give up here", "sample/maths.py"),
                change("fold total by hand", "sample/maths.py"),
            ),
            refuses=frozenset({"give up here"}),
            edits={
                "fold total by hand": rewrite(
                    "sample/maths.py", "return sum(values)", "return sum(list(values))"
                )
            },
        )
    )

    assert dropped(changes) == ["give up here"]
    assert kept(changes) == ["fold total by hand"]
    assert "the agent could not edit" in find(changes, "give up here").evidence[0]


# --- the eligibility refusal ------------------------------------------------------


def test_a_change_touching_code_with_no_oracle_is_refused_and_the_log_says_why(
    tier_one,
):
    """#5's precondition, applied by #15 — and refused *before* the edit.

    `sample.drift.stamp` is executed by the suite, so it is covered; its oracle
    varies across replays and is discarded, so it is Tier 0. A change touching
    it would otherwise ship a commit claiming "verified against recorded
    behaviour" about behaviour nothing recorded.
    """
    root, agent, changes, found = tier_one(
        lambda root: Scripted(
            root,
            queue=(change("tidy the timestamp", "sample/drift.py"),),
            edits={
                "tidy the timestamp": rewrite(
                    "sample/drift.py", "time.time_ns()", "int(time.time() * 1e9)"
                )
            },
        ),
        **{"sample/drift.py": DRIFT, "tests/test_drift.py": DRIFT_SUITE},
    )

    assert found.coverage.tier_of("sample.drift.stamp") is Tier.ZERO
    assert kept(changes) == []
    # Refused rather than attempted: the agent was never asked to edit it, so
    # there was never anything to revert.
    assert agent.applied == []

    refusal = changes[0].evidence[0]
    assert "refused Tier 1" in refusal
    assert "sample.drift.stamp" in refusal
    assert "no surviving characterization oracle" in refusal


def test_a_not_yet_existing_path_does_not_hold_an_extraction_down(tier_one):
    """The rule's one exception, which extraction depends on.

    `sample/greeting.py` does not exist when the change is tiered, and a file
    with no symbols is Tier 0. If that counted, the lowest-tier rule would
    refuse every extraction Tier 1 exists to perform — caution aimed at the one
    file in the change that cannot be broken. The file it takes the code *from*
    is tiered exactly as strictly as ever.
    """
    root, _, changes, _ = tier_one(
        lambda root: Scripted(
            root,
            queue=(
                change(
                    "extract the greeting",
                    "sample/maths.py",
                    "sample/greeting.py",
                    category="extract",
                ),
            ),
            edits={"extract the greeting": extract_greeting},
        )
    )

    assert kept(changes) == ["extract the greeting"]
    assert (root / "sample" / "greeting.py").read_text() == 'GREETING = "hello"\n'


def test_a_change_that_only_creates_files_is_refused_as_code_generation(tier_one):
    """SPEC §1.2: "not a code generator. It only touches code that already exists."

    A queue entry whose every path is new moves nothing out of anywhere, so
    there is no behaviour it could be preserving — and nothing recorded that
    could verify it either way. Refused before the edit, and the log says which
    of the two rules refused it, because "no oracle" and "not a refactor" are
    different problems with different fixes.
    """
    root, agent, changes, _ = tier_one(
        lambda root: Scripted(
            root,
            queue=(
                change(
                    "add a constants module", "sample/constants.py", category="extract"
                ),
            ),
            edits={
                "add a constants module": create("sample/constants.py", "LIMIT = 10\n")
            },
        )
    )

    assert kept(changes) == []
    assert agent.applied == []
    assert "code generation rather than refactoring" in changes[0].evidence[0]
    assert not (root / "sample" / "constants.py").exists()


# --- the agent is not trusted to stay inside its declaration ----------------------


def test_an_edit_outside_the_declared_files_is_reverted_in_full(tier_one):
    """The black box's edge (SPEC §3.2), and what makes "alone" true.

    An undeclared edit that survived would sit in the worktree unverified as
    part of this change and unreverted with it, waiting to be swept into
    whichever later commit happened to name that path. It is not repairable
    either: the change Polish tiered is not the change on the disk.
    """
    root, agent, changes, _ = tier_one(
        lambda root: Scripted(
            root,
            queue=(change("tidy total", "sample/maths.py"),),
            edits={"tidy total": touch_other("sample/__init__.py")},
        )
    )

    assert kept(changes) == []
    assert agent.attempted == [], "drift is not a verification failure to repair"
    assert "did not declare" in changes[0].evidence[0]
    assert (root / "sample" / "__init__.py").read_text() == ""


# --- the whole night ---------------------------------------------------------------


def test_a_night_shift_run_ships_tier_one_changes_on_a_branch(
    make_git_repo, monkeypatch, sandbox_venv
):
    """Criterion 1, end to end: the edits happen in the sandbox and land on a
    branch, and the working tree is byte-identical afterwards (SPEC §4.9).

    The agent is injected through `polish.tier1.refactor`'s default, which is
    the one seam a real run resolves lazily — so this exercises the envelope's
    wiring rather than a path only the test can reach.
    """
    monkeypatch.setenv("POLISH_TEST", "1")
    root = make_git_repo(
        {
            "pyproject.toml": PROJECT,
            "sample/__init__.py": "",
            "sample/maths.py": MATHS,
            "tests/test_maths.py": MATHS_SUITE,
            "polish.toml": "[characterize]\nbaseline_runs = 2\nreplays = 2\n",
        }
    )
    before = (root / "sample" / "maths.py").read_text()

    def _agent(worktree, policy):
        return Scripted(
            Path(worktree),
            queue=(
                change("fold total by hand", "sample/maths.py"),
                change("tidy scale", "sample/maths.py"),
            ),
            edits={
                "fold total by hand": rewrite(
                    "sample/maths.py", "return sum(values)", "return sum(list(values))"
                ),
                "tidy scale": rewrite("sample/maths.py", GOOD_SCALE, BROKEN_SCALE),
            },
        )

    monkeypatch.setattr(tier1, "_default_agent", _agent)

    record = envelope.run(root)

    assert any("fold total by hand" in subject for subject in record.changes)
    assert any("tidy scale" in note for note in record.dropped)

    summary = "\n".join(record.summary())
    # Criterion 4: the failure output reaches the run log, not just a count.
    assert "was observed returning" in summary

    # §4.9, which every one of these tests is downstream of.
    assert (root / "sample" / "maths.py").read_text() == before


# --- reading a queue out of what a model actually says ---------------------------


class TestReadingTheQueue:
    """`_claude._parse`, which is the one part of the SDK adapter testable here.

    Everything else in that module needs Node, a `claude` binary and
    credentials. This does not — and it is where the adapter is most likely to
    be wrong, because it is the only place Polish parses free-form model output.
    """

    def parse(self, reply: str):
        from polish.tier1._claude import _parse

        return _parse(reply)

    def test_a_bare_json_array_is_read(self):
        queue = self.parse(
            '[{"category": "dedup", "summary": "merge the loaders",'
            ' "rationale": "same code twice", "paths": ["a.py", "b.py"]}]'
        )
        assert len(queue) == 1
        assert queue[0].category == "dedup"
        assert queue[0].paths == ("a.py", "b.py")

    def test_a_fenced_block_surrounded_by_prose_is_read(self):
        """Models explain themselves. The queue still has to come out."""
        queue = self.parse(
            "Here is what I would do:\n\n"
            '```json\n[{"category": "dead-code", "summary": "drop it",'
            ' "rationale": "unreachable", "paths": ["a.py"]}]\n```\n\n'
            "Let me know if you want me to proceed."
        )
        assert [item.summary for item in queue] == ["drop it"]

    def test_an_empty_queue_is_a_real_answer(self):
        assert self.parse("[]") == []

    def test_one_malformed_entry_costs_only_that_entry(self):
        """The loop's own rule, one layer up.

        A category nobody recognises and a change touching no files are both
        unusable. Neither is a reason to throw away the entries either side.
        """
        queue = self.parse(
            "["
            '{"category": "dedup", "summary": "good", "rationale": "r",'
            ' "paths": ["a.py"]},'
            '{"category": "invented", "summary": "bad category",'
            ' "rationale": "r", "paths": ["a.py"]},'
            '{"category": "dedup", "summary": "no paths", "rationale": "r",'
            ' "paths": []},'
            '{"category": "extract", "summary": "also good", "rationale": "r",'
            ' "paths": ["b.py"]}'
            "]"
        )
        assert [item.summary for item in queue] == ["good", "also good"]

    @pytest.mark.parametrize(
        "reply",
        ["I could not find anything to do.", '{"category": "dedup"}', ""],
    )
    def test_a_reply_that_is_not_a_queue_is_refused_loudly(self, reply):
        """Declined, not silently empty.

        "The agent proposed nothing" and "the agent did not answer the question"
        are different nights, and only the second is worth waking up to.
        """
        with pytest.raises(AgentError):
            self.parse(reply)


def test_a_tier_one_repository_still_gets_tier_zero_transforms(
    make_git_repo, monkeypatch
):
    """SPEC §3.1: Tier 1 permits "everything in Tier 0, plus".

    Promotion must not take anything away. Without both tiers running, a
    repository that earned Tier 1 would silently stop getting unused-import
    removal — worse off for having been promoted.
    """
    monkeypatch.setenv("POLISH_TEST", "1")
    root = make_git_repo(
        {
            "pyproject.toml": PROJECT,
            "sample/__init__.py": "",
            "sample/maths.py": "import json\n\n" + MATHS,
            "tests/test_maths.py": MATHS_SUITE,
            "polish.toml": "[characterize]\nbaseline_runs = 2\nreplays = 2\n",
        }
    )
    monkeypatch.setattr(
        tier1, "_default_agent", lambda worktree, policy: Scripted(Path(worktree))
    )

    record = envelope.run(root)

    assert record.tier is not None and record.tier.tier_one
    assert any("unused import" in subject for subject in record.changes), record.changes
