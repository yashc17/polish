"""The sandbox — a git worktree with its own virtualenv (SPEC §3.3).

Two things want isolating, and only one of them can be. The working tree is
protected absolutely, by Polish working somewhere else entirely and only ever
reading the original. The machine is not protected at all: the worktree runs
the repo's real code against the real filesystem. SPEC §3.3 calls that the
spec's weakest guarantee and requires it be stated plainly in the README rather
than buried, which is where it now is.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import venv
from dataclasses import dataclass
from pathlib import Path

from . import _git


@dataclass(frozen=True)
class Sandbox:
    """Where a run's work happens, and what had to be replayed into it."""

    worktree: Path
    venv: Path
    branch: str
    mirrored: tuple[str, ...]


def create(root: Path, *, worktree: Path, environment: Path, branch: str) -> Sandbox:
    """Build the sandbox, or leave nothing behind trying.

    A half-built sandbox is worse than none: the worktree stays registered in
    the user's repository and its branch stays on `git branch`, so an
    unattended tool that fails at 3am leaves debris every night and the next
    run has to work around a name git thinks is live. The rollback is
    deliberately untested — reaching it means forcing `venv.create` to fail,
    and the suite only goes through public interfaces.
    """
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git.add_worktree(root, worktree, branch)
    try:
        mirrored = _mirror_pending_work(root, worktree)
        _build_venv(environment)
    except BaseException:
        # Suppressed, and in this order, so that a failure while cleaning up
        # never replaces the failure worth reporting.
        with contextlib.suppress(_git.GitError):
            _git.remove_worktree(root, worktree)
        with contextlib.suppress(_git.GitError):
            _git.delete_branch(root, branch)
        shutil.rmtree(environment, ignore_errors=True)
        raise
    return Sandbox(
        worktree=worktree, venv=environment, branch=branch, mirrored=mirrored
    )


def destroy(root: Path, sandbox: Sandbox) -> None:
    """Tear the sandbox down.

    A run's durable outputs are the snapshot and the branch; the worktree and
    its virtualenv are scaffolding. Leaving a full checkout plus a virtualenv
    behind every night is how a tool that sells itself on trust quietly fills
    someone's disk. `polish run --keep-sandbox` keeps it for inspection.
    """
    _git.remove_worktree(root, sandbox.worktree)
    shutil.rmtree(sandbox.venv, ignore_errors=True)


def _mirror_pending_work(root: Path, worktree: Path) -> tuple[str, ...]:
    """Replay the user's uncommitted work into the sandbox.

    A worktree is checked out from HEAD, but the tree Polish was pointed at is
    the working tree — and at the end of a day of AI-assisted coding most of
    that day's work is usually still uncommitted. A sandbox built from HEAD
    alone would refactor yesterday's code, which is both useless and unsafe:
    the snapshot and the sandbox would describe different trees.

    Tracked changes replay as a patch, new files as copies. The user's
    repository is only ever read.
    """
    patch = _git.pending_patch(root)
    if patch.strip():
        _git.apply_patch(worktree, patch)
    for relative in _git.untracked_files(root):
        target = worktree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # A symlink is copied as a symlink, not as whatever it points at: that
        # is the more faithful mirror, and it means a dangling one — which git
        # is perfectly happy to list — cannot abort the night.
        shutil.copy2(root / relative, target, follow_symlinks=False)
    return _git.pending_changes(root)


def _build_venv(path: Path) -> None:
    """Create the run's virtualenv, with pip.

    With pip because the tier probe (#12) has to install the repo's own
    dependencies before it can decide whether the repo runs, and a virtualenv
    without pip cannot do that. Built from the interpreter Polish is itself
    running under; matching a repo's required Python version is the tier
    probe's problem, not the envelope's.

    It lives beside the worktree rather than inside it, so that it never shows
    up in the sandbox's own `git status` and can never be swept into a
    refactoring commit.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    builder = venv.EnvBuilder(with_pip=True, symlinks=os.name != "nt", clear=True)
    builder.create(path)
