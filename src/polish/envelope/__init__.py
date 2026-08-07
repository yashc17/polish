"""The safety envelope — snapshot, sandbox, branch, and the payload inside it.

`run()` is the whole interface. Everything behind it is private.

The envelope is the product (SPEC §1.1): a change Polish cannot verify is a
change Polish does not make, and the apparatus around the edit is what earns
the right to be left unattended. It takes the snapshot, builds the sandbox,
opens the branch, asks the tier probe which engine the repository has earned,
runs that engine inside it, commits what was proven one change at a time, and
leaves the working tree exactly as it found it. Five of the v1 acceptance
criteria are settled here — §4.2 (the snapshot byte-matches), §4.3 (the tier
probe classifies runnable repositories correctly), §4.4 (Tier 0 produces a
provably behaviour-preserving diff), §4.7 (one reviewable commit per change)
and §4.9 (the working tree is untouched).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import date
from itertools import count
from pathlib import Path

from polish import tier0
from polish.firstparty import FirstPartySet, scan
from polish.probe import Budget, TierVerdict, probe

from . import _git, _sandbox, _snapshot

#: Everything Polish writes into a repository lives here, and nothing else does.
WORKSPACE = ".polish"

BRANCH_PREFIX = "polish/"


class EnvelopeError(Exception):
    """Raised when Polish will not run against `root`.

    Carries an actionable message, on the same terms as `NotAPythonProject`:
    every refusal below has a next step, and naming it is the whole point of
    declining loudly instead of quietly doing something lesser.
    """


@dataclass(frozen=True)
class RunRecord:
    """What one night-shift run did.

    Every path is absolute; `summary()` renders them relative to `root`, which
    is how they are shown to a human.
    """

    root: Path
    run_id: str
    commit: str
    branch: str
    snapshot: Path
    worktree: Path
    venv: Path
    files: tuple[str, ...]
    mirrored: tuple[str, ...]
    sandbox_kept: bool
    #: The subject line of each change the run committed, in commit order.
    changes: tuple[str, ...] = ()
    #: One line per change that was made, failed verification and was reverted.
    #: Kept rather than swallowed: SPEC §3.10 wants the failure in the log.
    dropped: tuple[str, ...] = ()
    #: Why the run shipped nothing at all, when that is the answer. A tier
    #: whose verification could not run declines wholesale rather than
    #: proceeding unverified.
    declined: str = ""
    #: What the tier probe decided, and the evidence for it. Optional only so
    #: that a `RunRecord` can be built in tests without one; every real run has
    #: a verdict, because the tier chooses the engine (SPEC §2, step 3).
    tier: TierVerdict | None = None

    def summary(self) -> tuple[str, ...]:
        """The run, one fact per line — greppable from an overnight log."""
        sandbox = self.worktree.parent  # the worktree and its venv share it
        state = "kept" if self.sandbox_kept else "removed"
        lines = [
            _line("run", self.run_id),
            _line("commit", self.commit),
            _line(
                "snapshot",
                f"{self._relative(self.snapshot)} ({_count(self.files, 'file')})",
            ),
        ]
        if self.mirrored:
            lines.append(_line("mirrored", _count(self.mirrored, "uncommitted file")))
        lines += [
            _line("sandbox", f"{self._relative(sandbox)} ({state})"),
            _line("branch", self.branch),
        ]
        # The probe's own evidence, verbatim. Issue #12 requires the verdict to
        # reach the run log with what produced it, including every command that
        # was executed: a user whose repository landed Tier 0 is owed the
        # reason, and a claim about a command allowlist that a user cannot check
        # is a claim they have to take on faith.
        if self.tier is not None:
            lines += self.tier.summary()
        lines += [
            _line(
                "changes",
                f"{len(self.changes)} committed, {len(self.dropped)} dropped",
            ),
        ]
        lines += [f"  + {subject}" for subject in self.changes]
        lines += [f"  - {note}" for note in self.dropped]
        if self.declined:
            lines.append(f"  ! nothing shipped: {self.declined}")
        return tuple(lines)

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()


def run(
    root: Path,
    *,
    init_git: bool = False,
    keep_sandbox: bool = False,
    budget: Budget | None = None,
) -> RunRecord:
    """Perform a night-shift run against the repository at `root`.

    `budget` bounds the tier probe. Injectable for the same reason the observer's
    bounds are: a limit with no way to reach it in a test is a limit nobody has
    ever seen work.
    """
    root = Path(root)
    if not root.is_dir():
        raise EnvelopeError(f"{root} does not exist, or is not a directory.")
    root = root.resolve()

    # Everything that can refuse refuses before anything is written, including
    # `--init-git`: a run that was going to decline should leave no trace at all.
    first_party = scan(root)
    try:
        return _perform(
            root,
            first_party,
            init_git=init_git,
            keep_sandbox=keep_sandbox,
            budget=budget,
        )
    except _git.GitError as failure:
        # Git's own failures reach the user as refusals rather than tracebacks.
        # An unconfigured identity or a signing key wanting a passphrase is a
        # thing the user can fix, and unattended runs are read from a log.
        raise EnvelopeError(str(failure)) from failure


def _perform(
    root: Path,
    first_party: FirstPartySet,
    *,
    init_git: bool,
    keep_sandbox: bool,
    budget: Budget | None = None,
) -> RunRecord:
    _require_repository(root, init_git=init_git)

    workspace = _open_workspace(root)
    run_id = _allocate_run_id(root, workspace)

    snapshot = _snapshot.write(
        root, first_party.files, _snapshot_dir(workspace, run_id)
    )

    run_dir = workspace / "runs" / run_id
    sandbox = _sandbox.create(
        root,
        worktree=run_dir / "worktree",
        environment=run_dir / "venv",
        branch=f"{BRANCH_PREFIX}{run_id}",
    )

    # Step 3 of SPEC §2's pipeline, and the first thing that happens inside the
    # sandbox. The tier chooses the engine for everything after it, so it is
    # decided before any edit is planned rather than discovered during one.
    #
    # It runs against `sandbox.worktree`, never `root`: the probe installs
    # packages and executes the repository's own test suite, which is not
    # something to do to a working tree Polish has promised not to touch (§4.9).
    verdict = probe(
        sandbox.worktree,
        venv=sandbox.venv,
        first_party=first_party.files,
        budget=budget,
    )

    # The payload, inside the envelope that was just proven around it.
    committed, dropped, declined = _refactor(sandbox, first_party.files)

    if not keep_sandbox:
        _sandbox.destroy(root, sandbox)
        shutil.rmtree(run_dir, ignore_errors=True)

    return RunRecord(
        root=root,
        run_id=run_id,
        commit=_git.head(root),
        branch=sandbox.branch,
        snapshot=snapshot,
        worktree=sandbox.worktree,
        venv=sandbox.venv,
        files=first_party.files,
        mirrored=sandbox.mirrored,
        sandbox_kept=keep_sandbox,
        changes=committed,
        dropped=dropped,
        declined=declined,
        tier=verdict,
    )


def _refactor(
    sandbox: _sandbox.Sandbox, files: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    """Run Tier 0 in the sandbox, committing each change that verifies.

    The day's uncommitted work is committed first, on its own. Without that,
    every refactoring commit would arrive carrying whatever the developer had
    left unsaved in the same file, and the per-commit review surface SPEC §3.9
    is built on would show two authors' work as one diff.
    """
    worktree = sandbox.worktree
    if _git.pending_changes(worktree):
        _git.commit_all(worktree, _MIRRORED_COMMIT)

    committed: list[str] = []
    dropped: list[str] = []
    try:
        for change in tier0.refactor(worktree, files):
            if not change.kept:
                dropped.append(change.note())
                continue
            _git.commit_paths(worktree, (change.path,), change.message())
            committed.append(change.subject())
    except tier0.Unverifiable as failure:
        # Raised before any file is edited, so there is nothing to unwind —
        # only something to say. Verification that cannot run is not a verdict.
        return tuple(committed), tuple(dropped), str(failure)
    return tuple(committed), tuple(dropped), ""


def _require_repository(root: Path, *, init_git: bool) -> None:
    """Refuse anything that is not a git repository root, unless asked to fix it.

    There is one sandbox path and no degraded mode. Both fallbacks — a shadow
    repository elsewhere, and a non-git sandbox — were considered and rejected;
    see `docs/adr/0002-refuse-non-git-repositories.md`. `--init-git` removes
    the wall without building a second envelope: the wall becomes one flag.
    """
    enclosing = _git.repository_root(root)

    if enclosing is not None and enclosing != root:
        raise EnvelopeError(
            f"{root} is inside the git repository at {enclosing}, but is not "
            "its root.\n\n"
            "Polish snapshots, sandboxes and branches a whole repository, so "
            "aiming it at a\nsubdirectory would refactor one tree and commit "
            "to another.\n\n"
            f"  polish run {enclosing}\n"
        )

    if enclosing is None:
        if not init_git:
            raise EnvelopeError(_NOT_A_REPOSITORY.format(root=root))
        _git.initialise(root)

    if not _git.has_commits(root):
        if not init_git:
            raise EnvelopeError(_NO_COMMITS.format(root=root))
        _git.commit_all(root, "initial commit")


def _open_workspace(root: Path) -> Path:
    """Create `.polish/`, ignored by its own `.gitignore`.

    Polish never edits the user's `.gitignore`. A tool that writes to a tracked
    file to make room for itself has already broken the promise that the
    working tree is untouched (§4.9) — and would show up as a diff in the very
    review surface it is supposed to keep clean. A directory that ignores
    itself is the same trick `uv` uses for `.venv`, and it leaves `git status`
    reading exactly as it did before the run.
    """
    workspace = root / WORKSPACE
    workspace.mkdir(exist_ok=True)
    (workspace / ".gitignore").write_text("*\n", encoding="utf-8")
    return workspace


def _allocate_run_id(root: Path, workspace: Path) -> str:
    """The date, plus a suffix if today already has runs.

    A second run on the same night must not overwrite the first night's
    snapshot: the snapshot is the only record of a tree that no longer exists
    anywhere, so the one operation it must never support is being clobbered.
    Suffixing costs a directory; overwriting costs the recovery.
    """
    today = date.today().isoformat()
    for attempt in count(1):
        run_id = today if attempt == 1 else f"{today}.{attempt}"
        taken = _snapshot_dir(workspace, run_id).exists() or _git.branch_exists(
            root, f"{BRANCH_PREFIX}{run_id}"
        )
        if not taken:
            return run_id
    raise AssertionError("unreachable")  # pragma: no cover


def _snapshot_dir(workspace: Path, run_id: str) -> Path:
    return workspace / "snapshots" / "raw" / run_id


def _line(label: str, value: str) -> str:
    return f"{label:<10}{value}"


def _count(items: tuple[str, ...], noun: str) -> str:
    return f"{len(items)} {noun}" + ("" if len(items) == 1 else "s")


_MIRRORED_COMMIT = """\
chore(polish): the working tree as Polish found it

The uncommitted work replayed from the working tree into the sandbox, so that
every refactoring commit above this one is a diff of Polish's work alone.
Nothing in this commit was written by Polish.
"""

_NOT_A_REPOSITORY = """\
{root} is not a git repository.

Polish sandboxes itself in a git worktree, and the review output is a
branch with one commit per change. Neither is possible here.

  git init && git add -A && git commit -m "initial"

or let Polish do it for you:

  polish run --init-git
"""

_NO_COMMITS = """\
{root} is a git repository with no commits yet.

Polish sandboxes itself in a worktree checked out from HEAD, and there is
no HEAD to check out.

  git add -A && git commit -m "initial"

or let Polish do it for you:

  polish run --init-git
"""
