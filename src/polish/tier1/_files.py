"""What the tree looked like before the agent touched it — and putting it back.

SPEC §3.10's second half is one sentence with a load-bearing word in it: "revert
**that change alone**, log it with the failure output, continue the queue." This
module is what makes *alone* true rather than aspirational.

It has to do two things Tier 0 never needed:

**Restore a set of files, not one.** Tier 0's changes never span two files, so
reverting one was a variable assignment. Tier 1's payoff transforms are
cross-file by nature — the dedup that merges five loaders, the extraction that
creates `prompts.py` — so a revert is a set operation, including deleting files
the agent created that the repository has never had.

**Notice what was touched that was not declared.** The agent is a black box
(SPEC §3.2) and nothing in the protocol makes it edit only the files it named.
An undeclared edit that survived would be a change that Polish neither verified
as part of this change nor reverted with it, sitting in the worktree waiting to
be swept into whichever later commit happens to name that path. So the snapshot
covers the **whole first-party set**, not the declared paths, and drift outside
the declaration is a refusal rather than a surprise.

The snapshot is taken once per run and updated in place as changes are kept, so
the tree is read once rather than once per candidate change.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: A path the snapshot holds but that did not exist when it was taken. Distinct
#: from "content we could not read": reverting to `ABSENT` means *delete*, and
#: conflating the two would leave a half-written file behind under the name of a
#: file the repository never had.
ABSENT = None


@dataclass
class Snapshot:
    """The bytes of every file Tier 1 is allowed to disturb, and how to get back.

    Held in memory for the length of the run. That is affordable in a way worth
    stating, because it looks like the expensive choice: the run has already
    read every one of these files through mypy and executed the repository's
    whole suite several times over. Source text is the cheapest thing here.
    """

    root: Path
    contents: dict[str, bytes | None] = field(default_factory=dict)

    @classmethod
    def take(cls, root: Path, paths: Iterable[str]) -> Snapshot:
        root = Path(root)
        snapshot = cls(root=root)
        for relative in paths:
            snapshot.contents[relative] = _read(root / relative)
        return snapshot

    def cover(self, paths: Iterable[str]) -> None:
        """Start tracking paths the snapshot does not know about yet.

        A change may declare a file that does not exist — extraction creates
        one. Recording it as `ABSENT` before the edit is what lets a revert
        delete it, and what stops it reading as drift when it appears.
        """
        for relative in paths:
            if relative not in self.contents:
                self.contents[relative] = _read(self.root / relative)

    def drifted(self, paths: Sequence[str] | None = None) -> tuple[str, ...]:
        """Tracked paths whose contents on disk differ from what is held.

        With `paths`, restricted to those; without, every tracked path. The
        caller asks both questions of one snapshot: what did the change do, and
        what did it do that it did not say it would.
        """
        considered = self.contents if paths is None else paths
        return tuple(
            sorted(
                relative
                for relative in considered
                if _read(self.root / relative) != self.contents.get(relative, ABSENT)
            )
        )

    def restore(self, paths: Iterable[str]) -> None:
        """Put these paths back exactly as the snapshot holds them.

        Byte-for-byte, including deleting anything recorded as absent and
        recreating anything the agent deleted. After this the tree is
        indistinguishable from before the change, which is the only definition
        of "reverted" that a reviewer reading a diff would accept.
        """
        for relative in paths:
            target = self.root / relative
            held = self.contents.get(relative, ABSENT)
            if held is ABSENT:
                target.unlink(missing_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(held)

    def adopt(self, paths: Iterable[str]) -> None:
        """Accept what is on disk as the new baseline, for a change being kept.

        Called after a change verifies and before the next one is attempted, so
        that the next change's drift check measures against the tree as it now
        is rather than as the run began. Without this every change after the
        first would read the previous one's work as drift.
        """
        for relative in paths:
            self.contents[relative] = _read(self.root / relative)

    def existing(self, paths: Iterable[str]) -> tuple[str, ...]:
        """Those of `paths` that are files on disk right now.

        The type check takes a file list and mypy cannot read a file that is not
        there yet, so a change that declares a not-yet-created path has to be
        checked over what exists at the moment of checking.
        """
        return tuple(relative for relative in paths if (self.root / relative).is_file())


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except (OSError, ValueError):
        # A missing file and an unreadable one both mean "there is nothing here
        # to preserve". They differ in interest but not in what a revert does,
        # and a run that raised on a permissions quirk would abort a night over
        # a file it was never going to edit.
        return ABSENT
