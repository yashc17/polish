"""Layer 2 — the gitignore-aware walk.

What a repo ignores is the closest thing to a declaration of what it does not
consider its own, and it is available even when packaging metadata is not. Git
is not required: the ignore files are read directly, so a directory that was
never `git init`-ed still resolves (SPEC §5 Q1 leaves non-git repos open).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from pathspec import PathSpec

IGNORE_FILENAME = ".gitignore"
REPO_LOCAL_EXCLUDE = Path(".git") / "info" / "exclude"


@dataclass(frozen=True)
class _Frame:
    """One ignore file's rules, and the directory they are anchored to."""

    base: str  # posix path of the anchoring directory, relative to the repo root
    spec: PathSpec


@dataclass(frozen=True)
class IgnoreRules:
    """The ignore rules in effect at some depth in the tree.

    Immutable and cheap to extend, so each directory in the walk holds the
    rules it inherited plus its own without mutating its parent's view.
    """

    frames: tuple[_Frame, ...] = ()

    def extend(self, directory: Path, base: str) -> IgnoreRules:
        """Return rules with `directory`'s own `.gitignore` layered on top."""
        lines = _read_ignore_file(directory / IGNORE_FILENAME)
        if not lines:
            return self
        frame = _Frame(base=base, spec=PathSpec.from_lines("gitignore", lines))
        return replace(self, frames=(*self.frames, frame))

    def ignores(self, relative_path: str) -> bool:
        """True if `relative_path` (posix, repo-root-relative) is ignored.

        Git's rule is last-match-wins, with rules nearer the file taking
        precedence over rules further up the tree — so frames are consulted
        outermost first and the deepest opinion survives.
        """
        verdict = False
        for frame in self.frames:
            local = _relative_to_base(relative_path, frame.base)
            if local is None:
                continue
            matched = frame.spec.check_file(local).include
            if matched is not None:
                verdict = matched
        return verdict


def root_rules(root: Path) -> IgnoreRules:
    """The rules in effect at the repo root, including `.git/info/exclude`."""
    rules = IgnoreRules()
    lines = _read_ignore_file(root / REPO_LOCAL_EXCLUDE)
    if lines:
        spec = PathSpec.from_lines("gitignore", lines)
        rules = replace(rules, frames=(_Frame(base="", spec=spec),))
    return rules.extend(root, base="")


def _relative_to_base(relative_path: str, base: str) -> str | None:
    """Re-anchor a repo-root-relative path onto `base`, or None if outside it."""
    if not base:
        return relative_path
    prefix = f"{base}/"
    if not relative_path.startswith(prefix):
        return None
    return relative_path[len(prefix) :]


def _read_ignore_file(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
