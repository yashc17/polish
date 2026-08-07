"""The night run that actually changes code: acceptance criteria 4 and 7.

`test_envelope.py` proves the envelope holds around nothing. This proves what
happens when something is inside it — on a repository with **no test suite**,
which is the case Tier 0 exists for and the case criterion 4 names.

The two claims being tested are the product:

* §4.4 — the diff is *provably* behaviour-preserving. Not "the tests passed":
  there are no tests. Every commit is re-proven here from its own blobs.
* §4.7 — one reviewable commit per change, each carrying its evidence, and
  dropping any single one is safe. That last part is checked by reverting each
  commit on its own and watching it apply cleanly.
"""

from __future__ import annotations

import ast
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from conftest import commit_repo, git, materialise
from polish import envelope, tier0

#: A repository with no tests, no CI and the shapes an unused-import pass has
#: to tell apart: removable, load-bearing, and used.
REPO = {
    "pyproject.toml": '[project]\nname = "demo"\n',
    ".gitignore": "*.log\n",
    "README.md": "# demo\n",
    "demo/__init__.py": "",
    "demo/core.py": (
        '"""Core."""\n\nimport json\nimport os\n\n\ndef where():\n    return os.sep\n'
    ),
    "demo/util.py": (
        "import string\nfrom typing import Optional\n\n\n"
        "def pad(text: str) -> str:\n    return text.strip()\n"
    ),
    # `hashlib` is unused here but imported back out of this module by
    # `consumer`, so removing it breaks a file that never mentions it.
    "demo/shared.py": "import hashlib\n\n\ndef digest(text):\n    return text\n",
    "demo/consumer.py": (
        "from demo.shared import hashlib\n\n\n"
        "def sha(text):\n    return hashlib.sha256(text).hexdigest()\n"
    ),
}

#: Left uncommitted, the way a day of AI-assisted coding actually ends.
PENDING = {
    "demo/core.py": (
        '"""Core."""\n\nimport json\nimport os\n\n\n'
        'def where():\n    return os.sep + "!"\n'
    ),
    "demo/written_today.py": 'import base64\nimport re\n\nPATTERN = re.compile(r"x")\n',
}


@dataclass(frozen=True)
class Night:
    root: Path
    record: envelope.RunRecord
    before: dict[str, bytes]

    def commits(self) -> tuple[str, ...]:
        """The commits the run added to the branch, oldest first."""
        listed = git(self.root, "rev-list", "--reverse", f"main..{self.record.branch}")
        return tuple(listed.split())

    def message(self, commit: str) -> str:
        return git(self.root, "log", "-1", "--format=%B", commit)

    def touched(self, commit: str) -> tuple[str, ...]:
        listed = git(
            self.root, "diff-tree", "--no-commit-id", "--name-only", "-r", commit
        )
        return tuple(listed.split())

    def blob(self, commit: str, path: str) -> str:
        return git(self.root, "show", f"{commit}:{path}")


@pytest.fixture(scope="module")
def night(tmp_path_factory) -> Night:
    root = commit_repo(materialise(tmp_path_factory.mktemp("night"), REPO))
    materialise(root, PENDING)
    before = tree_state(root)

    record = envelope.run(root)

    return Night(root=root, record=record, before=before)


def tree_state(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and not {".git", ".polish"} & set(path.relative_to(root).parts)
    }


def module_bindings(source: str) -> set[str]:
    """Every name the module's top-level imports bind."""
    return {
        alias.asname
        or (alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name)
        for node in ast.parse(source).body
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }


def test_it_removed_the_unused_imports_it_could_prove_safe(night):
    assert night.record.changes == (
        "refactor(imports): drop 1 unused import from demo/core.py",
        "refactor(imports): drop 2 unused imports from demo/util.py",
        "refactor(imports): drop 1 unused import from demo/written_today.py",
    )


def test_a_change_that_would_break_another_file_never_landed(night):
    assert len(night.record.dropped) == 1
    assert night.record.dropped[0].startswith("demo/shared.py: dropped")
    assert "the type check reported 1 new diagnostic" in night.record.dropped[0]
    # And the branch carries the file exactly as the developer wrote it.
    assert night.blob(night.record.branch, "demo/shared.py") == REPO["demo/shared.py"]


def test_the_branch_is_one_commit_per_change_on_top_of_the_days_work(night):
    commits = night.commits()

    assert len(commits) == 1 + len(night.record.changes)
    assert night.message(commits[0]).startswith(
        "chore(polish): the working tree as Polish found it"
    )
    # The day's uncommitted work arrives on its own, so that no refactoring
    # commit below it is carrying somebody else's diff.
    assert set(night.touched(commits[0])) == set(PENDING)
    for commit, subject in zip(commits[1:], night.record.changes, strict=True):
        assert night.message(commit).startswith(subject)


def test_each_commit_touches_exactly_one_file(night):
    for commit in night.commits()[1:]:
        assert len(night.touched(commit)) == 1


def test_each_commit_carries_the_tier_and_its_verification_evidence(night):
    for commit in night.commits()[1:]:
        message = night.message(commit)
        assert "Tier: 0 (deterministic codemod, LibCST)" in message
        assert "AST-equivalence: proven —" in message
        assert "Type check: clean —" in message


def test_every_committed_diff_is_provably_behaviour_preserving(night):
    """Acceptance criterion 4, re-proven from what the branch actually holds."""
    for commit in night.commits()[1:]:
        path = night.touched(commit)[0]
        before = night.blob(f"{commit}~1", path)
        after = night.blob(commit, path)

        removed = module_bindings(before) - module_bindings(after)

        assert removed, f"{commit} removed no import binding from {path}"
        assert tier0.prove(before, after, removed=sorted(removed)).holds


def test_dropping_any_single_commit_is_safe(night, tmp_path_factory):
    """Acceptance criterion 7: no commit depends on any other."""
    clone = tmp_path_factory.mktemp("clone") / "repo"
    git(night.root.parent, "clone", "--quiet", str(night.root), str(clone))
    git(clone, "checkout", "--quiet", night.record.branch)

    for commit in night.commits()[1:]:
        reverted = subprocess.run(
            ("git", "revert", "--no-edit", commit),
            cwd=clone,
            capture_output=True,
            text=True,
            check=False,
        )
        assert reverted.returncode == 0, (
            f"{commit} does not revert alone:\n{reverted.stderr}"
        )
        git(clone, "reset", "--hard", "--quiet", night.record.branch)


def test_the_working_tree_is_untouched_even_though_the_run_changed_code(night):
    assert tree_state(night.root) == night.before
    assert git(night.root, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert (night.root / "demo" / "core.py").read_text() == PENDING["demo/core.py"]


def test_the_summary_reports_what_it_committed_and_what_it_dropped(night):
    summary = night.record.summary()

    assert "changes   3 committed, 1 dropped" in summary
    assert sum(line.startswith("  + ") for line in summary) == 3
    assert sum(line.startswith("  - ") for line in summary) == 1
    assert not any(line.startswith("  ! ") for line in summary)
