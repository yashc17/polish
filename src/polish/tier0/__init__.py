"""Tier 0 — the deterministic tier, for repositories that cannot be executed.

Two public entry points, one for doing the work and one for the claim behind it:

    refactor(worktree, files)       edit, prove, keep or drop — one file at a time
    prove(before, after, removed)   the Tier 0 claim, checkable on its own terms

SPEC §3.1 gives Tier 0 a single sentence to defend — **"provably
behaviour-preserving"** — and nothing else about it matters as much as that
sentence staying literally true. A repository Polish cannot run is a repository
whose behaviour Polish cannot observe, so every change here has to be
justified by reading alone. That rules out most of what a refactoring agent
would like to do, and it is why this tier ships one transform rather than
several.

The transform is unused-import removal, and it passes three gates in order:

1. **The removal rule** (`_inert`). Unused is not removable. Importing a module
   runs it, so a binding may only go when the module behind it is provably
   inert — the standard library, or a first-party module whose top level is
   definitions and literals. `import numpy` stays, with the reason recorded.
2. **AST-equivalence** (`_proof`). The file that reached the disk must be the
   original minus exactly those bindings. Comments and layout may move;
   nothing else may.
3. **The type check** (`_typecheck`). mypy over the whole file set, gated on
   introducing no diagnostic that was not already there. This is what catches
   the binding that was referenced somewhere the first gate could not see —
   most importantly from another file.

A change failing any gate is reverted and reported, never shipped
(SPEC §3.10). The caller commits the ones that pass, one commit each, and each
commit message carries the evidence above (SPEC §3.9).
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import _imports, _typecheck
from ._proof import Proof, prove
from ._typecheck import Unverifiable

#: The tier this engine is. Quoted in every commit message it produces.
TIER = 0

#: The commit-message category (SPEC §3.9). One transform, so one category.
CATEGORY = "imports"

#: Where the type checker's cache goes: beside the worktree, never inside it.
CACHE_DIR = "typecheck"

__all__ = [
    "CACHE_DIR",
    "CATEGORY",
    "TIER",
    "Change",
    "Proof",
    "Unverifiable",
    "prove",
    "refactor",
]


@dataclass(frozen=True)
class Change:
    """One file's refactor, and what became of it.

    A dropped change is still a change: SPEC §3.10 requires the failure and its
    output in the log rather than silence, because a reviewer who cannot see
    what Polish tried and rejected has no way to tell a careful night from an
    idle one.
    """

    path: str
    removed: tuple[str, ...]
    left_alone: tuple[tuple[str, str], ...]
    kept: bool
    #: Verification evidence when the change was kept; the single refusal when
    #: it was not.
    evidence: tuple[str, ...]

    def subject(self) -> str:
        return (
            f"refactor({CATEGORY}): drop "
            f"{_count(len(self.removed), 'unused import')} from {self.path}"
        )

    def message(self) -> str:
        """The commit message, evidence and all (SPEC §3.9)."""
        body = [
            self.subject(),
            "",
            _wrap(
                f"Removes {_names(self.removed)}: bound at module level, "
                f"referenced nowhere, and imported from a module that runs "
                f"nothing when it is imported."
            ),
        ]
        for _, reason in self.left_alone:
            body += ["", _wrap(f"Left alone: {reason}.")]
        body += ["", f"Tier: {TIER} (deterministic codemod, LibCST)", *self.evidence]
        return "\n".join(body) + "\n"

    def note(self) -> str:
        """One line for the run log — what happened to this file, and why."""
        return f"{self.path}: {self.evidence[0]}"


def refactor(
    worktree: Path,
    files: Sequence[str],
    *,
    verify_against: Sequence[str] | None = None,
) -> Iterator[Change]:
    """Refactor `files` inside `worktree`, yielding each change once settled.

    Every yielded change has already been decided: a kept one is on disk and
    proven, a dropped one has been reverted and the file is byte-identical to
    what it was. The caller is expected to commit each kept change before
    taking the next, which is what makes the branch reviewable commit by
    commit — but nothing here depends on it, because changes never span files
    and no change depends on another. That independence is what makes dropping
    any single commit safe (SPEC §4.7).

    `verify_against` is the file set the **type check** covers, defaulting to
    `files`. The two are separate because gate 3 is the only gate that can see
    a binding referenced from another file, so its surface decides what
    "provably behaviour-preserving" means. Once SPEC §3.6 lets a run refactor a
    scope rather than the whole repository, passing the scope here as well
    would quietly narrow the proof along with the work — and a removal that
    breaks a file just outside the scope would ship. The scope may narrow what
    Polish *edits*; it may not narrow what Polish *checks*.

    Raises `Unverifiable` if the type check cannot establish a baseline. That
    happens before anything is edited, so the worktree is untouched.
    """
    removals = _imports.candidates(worktree, files)
    if not removals:
        return

    checker = _typecheck.Checker(
        worktree,
        files if verify_against is None else verify_against,
        cache=worktree.parent / CACHE_DIR,
    )
    baseline = checker.run()

    for removal in removals:
        path = worktree / removal.path
        _imports.write_source(path, removal.after)

        # Proven against what is on the disk, not against what was meant to go
        # there. The claim being made is about the file a reviewer will read.
        proof = prove(removal.before, _imports.read_source(path), removed=removal.names)
        if not proof:
            yield _dropped(removal, path, f"AST-equivalence failed — {proof.reason}")
            continue

        try:
            checked = checker.run()
        except Unverifiable as failure:
            yield _dropped(removal, path, f"the type check could not run — {failure}")
            continue

        introduced = checked.new_since(baseline)
        if introduced:
            yield _dropped(
                removal,
                path,
                f"the type check reported {_count(len(introduced), 'new diagnostic')}"
                f", the first being — {introduced[0]}",
            )
            continue

        baseline = checked
        yield Change(
            path=removal.path,
            removed=removal.names,
            left_alone=removal.left_alone,
            kept=True,
            evidence=(
                f"AST-equivalence: proven — {proof.reason}",
                "Type check: clean — mypy reports no diagnostic that was not "
                "there before",
            ),
        )


def _dropped(removal: _imports.Removal, path: Path, reason: str) -> Change:
    _imports.write_source(path, removal.before)
    return Change(
        path=removal.path,
        removed=removal.names,
        left_alone=removal.left_alone,
        kept=False,
        evidence=(f"dropped, {reason}",),
    )


def _wrap(text: str) -> str:
    return textwrap.fill(text, width=72)


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")


def _names(names: Sequence[str]) -> str:
    quoted = [f"`{name}`" for name in names]
    if len(quoted) <= 2:
        return " and ".join(quoted)
    return f"{', '.join(quoted[:-1])} and {quoted[-1]}"
