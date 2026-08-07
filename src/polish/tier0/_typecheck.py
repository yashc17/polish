"""The Tier 0 type check (SPEC §3.1) — mypy over the whole worktree.

Two things about the shape of this are load-bearing, and both are argued in
`docs/adr/0004-mypy-as-the-tier-0-type-check.md`.

**It checks the whole file set, not the edited file.** Removing an import from
`a.py` breaks `b.py` when `b.py` does `from a import json`, and nothing that
reads `a.py` alone can see that. A whole-tree check is what makes the
re-export case a caught failure rather than a shipped bug.

**The gate is "no new diagnostic", not "no diagnostic".** Tier 0's stated
target is a repository with no test suite, which is not a repository that type
checks clean today. Demanding a clean baseline would mean shipping nothing on
every real repo, which is a gate that never fires and therefore proves nothing.

If the check cannot run at all, `Unverifiable` is raised and nothing ships: a
change Polish cannot verify is a change Polish does not make (SPEC §1.1).
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

#: One check on a large repository, generously. An unattended run needs a
#: bound; the incremental cache makes every check after the first one cheap.
TIMEOUT_SECONDS = 600

MYPY_ARGUMENTS = (
    # Tier 0 cannot install the repository's dependencies — that is what makes
    # it Tier 0 — so third-party imports are expected to be unresolvable and
    # Polish's own site-packages must not be mistaken for the repo's.
    "--ignore-missing-imports",
    "--no-site-packages",
    # Two files with the same basename in unpackaged directories are a fatal
    # error to mypy unless module names are derived from the tree root.
    "--explicit-package-bases",
    "--no-error-summary",
    "--no-color-output",
    "--no-pretty",
    "--hide-error-context",
)

_DIAGNOSTIC = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+): (?P<severity>\w+): (?P<message>.*)$"
)


class Unverifiable(Exception):
    """The type check could not run, so nothing can be proven by it."""


@dataclass(frozen=True)
class Diagnostics:
    """What mypy said, with line numbers deliberately discarded.

    An edit that deletes a line moves every diagnostic below it. Comparing on
    position would report all of them as new, so the identity of a diagnostic
    here is its file, its severity and its text — counted, so that a second
    copy of a message that already appeared once still reads as new.
    """

    counted: Counter[str]

    def new_since(self, baseline: Diagnostics) -> tuple[str, ...]:
        return tuple(
            sorted(
                message
                for message, count in self.counted.items()
                if count > baseline.counted[message]
            )
        )


class Checker:
    """Runs the type check, once per candidate change.

    The cache directory lives beside the worktree rather than inside it, for
    the same reason the sandbox's virtualenv does: nothing Polish creates may
    show up in the sandbox's own `git status` and get swept into a commit.
    """

    def __init__(self, worktree: Path, files: Sequence[str], cache: Path) -> None:
        self._worktree = worktree
        self._cache = cache
        self._arguments = cache / "files.txt"
        cache.mkdir(parents=True, exist_ok=True)
        # A response file rather than an argument list: a large repository's
        # file set is long enough to exceed the operating system's limit.
        self._arguments.write_text("\n".join(files) + "\n", encoding="utf-8")

    def run(self) -> Diagnostics:
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mypy",
                    *MYPY_ARGUMENTS,
                    "--cache-dir",
                    str(self._cache / "mypy"),
                    f"@{self._arguments}",
                ],
                cwd=self._worktree,
                capture_output=True,
                text=True,
                check=False,
                timeout=TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as expired:
            raise Unverifiable(
                f"mypy did not finish within {TIMEOUT_SECONDS}s"
            ) from expired
        except OSError as failure:  # pragma: no cover - a broken interpreter
            raise Unverifiable(f"mypy could not be started: {failure}") from failure

        counted = _parse(completed.stdout)
        # mypy exits 0 clean, 1 having found errors, and 2 or more when it could
        # not do the job — a fatal internal error, an unreadable file, a bad
        # option. The last of those is the case that must not read as "clean".
        if completed.returncode >= 2 or (completed.returncode and not counted):
            raise Unverifiable(_detail(completed))
        return Diagnostics(counted=counted)


def _parse(output: str) -> Counter[str]:
    counted: Counter[str] = Counter()
    for line in output.splitlines():
        match = _DIAGNOSTIC.match(line.strip())
        if match is None or match["severity"] == "note":
            # Notes are context attached to the error above them, and they
            # carry positions of their own. The error is the diagnostic.
            continue
        counted[f"{match['file']}: {match['severity']}: {match['message']}"] += 1
    return counted


def _detail(completed: subprocess.CompletedProcess[str]) -> str:
    reported = (completed.stderr.strip() or completed.stdout.strip()).splitlines()
    said = reported[0] if reported else f"exit status {completed.returncode}"
    return f"mypy: {said}"
