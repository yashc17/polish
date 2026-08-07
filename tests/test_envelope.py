"""The safety envelope's contract: snapshot, sandbox, branch, untouched tree.

Two of the v1 acceptance criteria are proved here rather than asserted —
§4.2 (the snapshot byte-matches the pre-run tree) and §4.9 (the working tree is
untouched throughout).

Most of these are properties of one run, so one representative run is performed
per module and interrogated from several angles. Building a virtualenv costs a
couple of seconds, and a run per assertion would spend most of the suite's time
proving the same `venv.create` works repeatedly.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

from conftest import commit_repo, git, materialise
from polish import envelope
from polish.firstparty import scan

#: A repository with something of every kind in it: source, a unicode source
#: file, an ignored artifact, a non-Python file, and a virtualenv — 30 000 of
#: whose files would be the common real-world case (SPEC §5 Q2).
REPO = {
    "pyproject.toml": '[project]\nname = "demo"\n',
    ".gitignore": "*.log\n",
    "demo/__init__.py": "",
    "demo/core.py": "def go():\n    return 1\n",
    "demo/unicode.py": 'SIGN = "café — ✅"\n',
    "README.md": "# demo\n",
    "run.log": "ignored artifact\n",
    ".venv/pyvenv.cfg": "home = /usr/bin\n",
    ".venv/lib/python3.12/site-packages/torch/__init__.py": "VERSION = 1\n",
}

#: Left uncommitted, the way a day of AI-assisted coding actually ends.
PENDING = {
    "demo/core.py": "def go():\n    return 2\n",
    "demo/written_today.py": "def fresh():\n    return None\n",
}


@dataclass(frozen=True)
class Run:
    root: Path
    record: envelope.RunRecord
    before: dict[str, bytes]
    status_before: str


@pytest.fixture(scope="module")
def completed_run(tmp_path_factory) -> Run:
    root = commit_repo(materialise(tmp_path_factory.mktemp("repo"), REPO))
    materialise(root, PENDING)
    before, status_before = tree_state(root), git(root, "status", "--porcelain")

    record = envelope.run(root, keep_sandbox=True)

    return Run(root=root, record=record, before=before, status_before=status_before)


def tree_state(root: Path) -> dict[str, bytes]:
    """Every working-tree file and its bytes.

    `.git` is excluded because it is not the working tree — the run is
    *supposed* to add a branch ref there — and `.polish` because it is Polish's
    own workspace. That `.polish` is the only thing the run adds is asserted
    separately, so excluding it here does not hide anything.
    """
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and not {".git", ".polish"} & set(path.relative_to(root).parts)
    }


def test_the_snapshot_byte_matches_the_pre_run_tree(completed_run):
    snapshot = completed_run.record.snapshot

    for relative in completed_run.record.files:
        assert (snapshot / relative).read_bytes() == (
            completed_run.root / relative
        ).read_bytes(), relative


def test_the_snapshot_holds_the_first_party_set_and_nothing_else(completed_run):
    snapshot = completed_run.record.snapshot
    copied = {
        path.relative_to(snapshot).as_posix()
        for path in snapshot.rglob("*")
        if path.is_file()
    }

    assert copied == set(scan(completed_run.root).files)
    assert "demo/written_today.py" in copied
    # The 30 000-file case: a snapshot that copies the virtualenv is not a
    # snapshot anybody can afford to take nightly.
    assert not any("site-packages" in relative for relative in copied)


def test_the_working_tree_is_byte_identical_after_the_run(completed_run):
    assert tree_state(completed_run.root) == completed_run.before


def test_the_run_adds_nothing_to_the_tree_but_its_own_workspace(completed_run):
    entries = {path.name for path in completed_run.root.iterdir()}
    original = {relative.split("/")[0] for relative in REPO}

    assert entries == original | {".git", ".polish"}


def test_git_reports_the_repository_exactly_as_it_was(completed_run):
    assert git(completed_run.root, "status", "--porcelain") == (
        completed_run.status_before
    )
    assert (
        git(completed_run.root, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    )


def test_the_output_branch_is_created_at_head(completed_run):
    root, record = completed_run.root, completed_run.record

    assert record.branch == f"polish/{date.today().isoformat()}"
    assert git(root, "rev-parse", record.branch) == git(root, "rev-parse", "HEAD")


def test_the_sandbox_is_a_worktree_carrying_the_uncommitted_work(completed_run):
    worktree = completed_run.record.worktree

    # A linked worktree keeps a `.git` file pointing at the real repository,
    # where a clone would have a directory. This is the former.
    assert (worktree / ".git").is_file()
    assert git(worktree, "rev-parse", "--abbrev-ref", "HEAD").strip() == (
        completed_run.record.branch
    )
    for relative, contents in PENDING.items():
        assert (worktree / relative).read_text() == contents
    assert set(completed_run.record.mirrored) == set(PENDING)
    # `.polish` is ignored, so the sandbox cannot contain the workspace that
    # contains the sandbox.
    assert not (worktree / ".polish").exists()


def test_the_sandbox_virtualenv_is_real_and_sits_outside_the_worktree(completed_run):
    record = completed_run.record
    interpreter = venv_python(record.venv)

    assert record.venv.parent == record.worktree.parent
    assert interpreter.exists()
    prefix = subprocess.run(
        [str(interpreter), "-c", "import pip, sys; print(sys.prefix)"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert Path(prefix) == record.venv


def test_the_workspace_ignores_itself_without_touching_the_users_gitignore(
    completed_run,
):
    root = completed_run.root

    assert (root / ".polish" / ".gitignore").read_text() == "*\n"
    assert (root / ".gitignore").read_text() == REPO[".gitignore"]


def test_the_sandbox_is_removed_when_the_run_ends(make_git_repo):
    root = make_git_repo(REPO)

    record = envelope.run(root)

    assert not record.worktree.exists()
    assert not record.venv.exists()
    assert record.worktree.parent.parent.exists()  # .polish/runs survives, empty
    assert str(record.worktree) not in git(root, "worktree", "list")
    # The durable outputs are untouched by the teardown.
    assert record.snapshot.is_dir()
    assert git(root, "rev-parse", record.branch)


def test_a_second_run_the_same_day_does_not_clobber_the_first(make_git_repo):
    root = make_git_repo(REPO)
    today = date.today().isoformat()

    first = envelope.run(root)
    (root / "demo" / "core.py").write_text("def go():\n    return 99\n")
    second = envelope.run(root)

    assert (first.run_id, second.run_id) == (today, f"{today}.2")
    assert first.branch != second.branch
    assert (first.snapshot / "demo/core.py").read_text() == REPO["demo/core.py"]
    assert (
        second.snapshot / "demo/core.py"
    ).read_text() == "def go():\n    return 99\n"


def test_it_refuses_a_directory_that_is_not_a_git_repository(make_repo):
    root = make_repo(REPO)

    with pytest.raises(envelope.EnvelopeError) as refusal:
        envelope.run(root)

    assert "not a git repository" in str(refusal.value)
    assert "git init && git add -A" in str(refusal.value)
    assert "--init-git" in str(refusal.value)
    # A refusal leaves no trace: no repository, and no workspace either.
    assert not (root / ".git").exists()
    assert not (root / ".polish").exists()


def test_init_git_creates_the_repository_and_runs(make_repo):
    root = make_repo(REPO)

    record = envelope.run(root, init_git=True)

    assert (root / ".git").is_dir()
    assert git(root, "rev-parse", record.branch)
    assert record.snapshot.is_dir()
    # `--init-git` adds a `.git/`; §4.9 is about the tree being refactored, and
    # every source file is still exactly what it was.
    assert (root / "demo" / "core.py").read_text() == REPO["demo/core.py"]


def test_it_refuses_a_repository_with_no_commits(make_repo):
    root = make_repo(REPO)
    git(root, "init", "--quiet", "-b", "main")

    with pytest.raises(envelope.EnvelopeError) as refusal:
        envelope.run(root)

    assert "no commits yet" in str(refusal.value)
    assert "--init-git" in str(refusal.value)


def test_init_git_commits_a_repository_that_has_no_commits(make_repo):
    root = make_repo(REPO)
    git(root, "init", "--quiet", "-b", "main")

    record = envelope.run(root, init_git=True)

    assert git(root, "rev-parse", record.branch)


def test_it_refuses_a_subdirectory_of_a_repository(make_git_repo):
    root = make_git_repo(REPO)

    with pytest.raises(envelope.EnvelopeError) as refusal:
        envelope.run(root / "demo")

    assert "is not its root" in str(refusal.value)
    assert f"polish run {root}" in str(refusal.value)


def test_it_declines_a_non_python_directory_before_writing_anything(make_repo):
    root = make_repo({"index.js": "", "package.json": "{}\n"})

    with pytest.raises(Exception) as refusal:
        envelope.run(root, init_git=True)

    assert "not a Python project" in str(refusal.value)
    assert not (root / ".git").exists()


def venv_python(environment: Path) -> Path:
    if os.name == "nt":  # pragma: no cover - the suite runs on POSIX in CI
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"
