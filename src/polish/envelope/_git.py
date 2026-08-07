"""Every git subprocess Polish runs.

Git is not an optional accelerator for the safety envelope — it *is* the
envelope. The sandbox is a worktree and the review output is a branch (SPEC
§3.3, §3.9), so a directory without a repository has no envelope at all. That
is why Polish refuses one rather than degrading; see
`docs/adr/0002-refuse-non-git-repositories.md`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(Exception):
    """A git command failed.

    Carries git's own stderr verbatim. For the failures Polish cannot
    anticipate — an unconfigured identity, a signing key wanting a passphrase —
    git's message is already the actionable one, and paraphrasing it would only
    lose detail.
    """


def repository_root(directory: Path) -> Path | None:
    """The root of the git repository containing `directory`, or None."""
    try:
        toplevel = _text(directory, "rev-parse", "--show-toplevel")
    except GitError:
        return None
    return Path(toplevel).resolve()


def has_commits(root: Path) -> bool:
    """False for a freshly `git init`-ed repository, whose HEAD is unborn."""
    return _succeeds(root, "rev-parse", "--verify", "--quiet", "HEAD")


def head(root: Path) -> str:
    """The abbreviated commit the run is sandboxed from."""
    return _text(root, "rev-parse", "--short", "HEAD")


def initialise(root: Path) -> None:
    _run(root, "init", "--quiet")


def commit_all(root: Path, message: str) -> None:
    _run(root, "add", "-A")
    _run(root, "commit", "--quiet", "-m", message)


def branch_exists(root: Path, name: str) -> bool:
    return _succeeds(root, "show-ref", "--verify", "--quiet", f"refs/heads/{name}")


def add_worktree(root: Path, path: Path, branch: str) -> None:
    """Check `branch`, created at HEAD, out into its own worktree at `path`."""
    _run(root, "worktree", "add", "--quiet", "-b", branch, str(path), "HEAD")


def delete_branch(root: Path, name: str) -> None:
    _run(root, "branch", "--delete", "--force", name)


def remove_worktree(root: Path, path: Path) -> None:
    _run(root, "worktree", "remove", "--force", str(path))
    # A worktree directory that git still has registered is worse than no
    # worktree: the next run's `worktree add` fails on a name git thinks is live.
    _run(root, "worktree", "prune")


def pending_patch(root: Path) -> bytes:
    """A patch of every uncommitted change to a tracked file.

    `--binary` rather than a plain diff so that images, fixtures and anything
    else git calls binary survive the round trip instead of applying as
    "Binary files differ".
    """
    return _run(root, "diff", "HEAD", "--binary")


def apply_patch(worktree: Path, patch: bytes) -> None:
    _run(worktree, "apply", "--whitespace=nowarn", stdin=patch)


def untracked_files(root: Path) -> tuple[str, ...]:
    """Untracked, non-ignored paths — the day's new files, before they are added.

    `--exclude-standard` is what keeps `.polish/` and the user's virtualenv out
    of this list, since both are ignored.
    """
    raw = _run(root, "ls-files", "--others", "--exclude-standard", "-z")
    return tuple(sorted(_split_nul(raw)))


def pending_changes(root: Path) -> tuple[str, ...]:
    """Every path with uncommitted work: modified, staged, deleted or new."""
    entries = _split_nul(_run(root, "status", "--porcelain", "-z", "-uall"))
    names: list[str] = []
    pending = iter(entries)
    for entry in pending:
        status, path = entry[:2], entry[3:]
        if status[0] in "RC":
            # Renames and copies spend a second NUL-separated field on the
            # path the change came from. Consume it, or it reads as a filename.
            next(pending, None)
        names.append(path)
    return tuple(sorted(names))


def _run(cwd: Path, *args: str, stdin: bytes | None = None) -> bytes:
    completed = subprocess.run(
        ("git", *args), cwd=cwd, input=stdin, capture_output=True, check=False
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)} failed in {cwd}:\n{detail}")
    return completed.stdout


def _text(cwd: Path, *args: str) -> str:
    return _run(cwd, *args).decode("utf-8", "replace").strip()


def _succeeds(cwd: Path, *args: str) -> bool:
    try:
        _run(cwd, *args)
    except GitError:
        return False
    return True


def _split_nul(raw: bytes) -> list[str]:
    return [field for field in raw.decode("utf-8", "replace").split("\0") if field]
