"""Running Polish's harness inside the sandbox, under a bounded clock.

Everything here is the plumbing between `characterize()` and `_harness`: the
scratch directory the harness is copied into, the JSON that parameterises it,
the runner it goes through, and the budget that stops it. The runner is
`polish.probe.sandbox_runner` — the probe's, not a second one. ADR 0006 says
downstream work "inherits this allowlist rather than inventing a second one",
and inheriting the function rather than the idea is what keeps that true.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic

from polish.probe import (
    Command,
    Completed,
    Runner,
    harness,
    interpreter,
    modules,
    sandbox_runner,
)

from . import _harness
from ._result import Policy

#: The scratch directory's contents. Named here because two of them are also
#: `_harness`'s side of the contract.
HARNESS_FILE = "_polish_harness.py"
CONFIG_FILE = "config.json"
OUTPUT_FILE = "result.json"
ORACLE_DIR = "oracles"


class Exhausted(Exception):
    """Raised when characterization has spent its time. Never a failure verdict.

    A repository whose suite is slow enough to exhaust the budget has told
    Polish nothing about whether its tests are flaky, and "nothing" must not
    read as "clean". The caller turns this into a declined characterization,
    which leaves every symbol at Tier 0.
    """


@dataclass(frozen=True)
class Outcome:
    """One harness run: what the process did, and what the harness reported.

    A non-zero exit is *normal* here and is not the error signal — pytest exits
    1 when tests fail, which is exactly the case the baseline exists to observe.
    The error signal is the harness failing to report at all.
    """

    completed: Completed
    payload: dict
    error: str = ""

    @property
    def tests(self) -> dict[str, str]:
        found = self.payload.get("tests", {})
        return found if isinstance(found, dict) else {}

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class Session:
    """The scratch directory, the runner, the clock, and the command log."""

    python: Path
    policy: Policy
    runner: Runner
    scratch: Path
    started: float = field(default_factory=monotonic)
    commands: list[str] = field(default_factory=list)

    @property
    def oracle_dir(self) -> Path:
        return self.scratch / ORACLE_DIR

    def run(self, task: str, seconds: float) -> Outcome:
        remaining = self.policy.total_seconds - (monotonic() - self.started)
        command = harness(self.python, task, min(seconds, max(remaining, 0.0)))
        if remaining <= 0:
            raise Exhausted(
                f"the {self.policy.total_seconds:.0f}s characterization budget "
                f"was spent before `{command.display()}` could run"
            )
        self.commands.append(command.display())
        return self._read(command, self.runner(command))

    def repeat(self, task: str, seconds: float, times: int) -> list[Outcome]:
        """Run the same task `times` over — the whole of the flake policy.

        Nothing varies between the runs except the things Polish does not
        control, which is the point: what differs is the repository's own
        non-determinism. Note in particular that `PYTHONHASHSEED` is *not* set
        in the sandbox environment, so each run gets its own hash seed and set
        and dict ordering really does move between them (issue #13, criterion
        6). Pinning it would hide exactly the flakes this is looking for.
        """
        return [self.run(task, seconds) for _ in range(times)]

    def _read(self, command: Command, completed: Completed) -> Outcome:
        """Read the harness's report, or say why there is not one.

        The output file is removed before every run, so a stale result from the
        previous one can never be read as this one's — the failure mode that
        would make a second baseline run silently agree with the first.
        """
        output = self.scratch / OUTPUT_FILE
        if completed.timed_out:
            return Outcome(completed, {}, f"`{command.display()}` timed out")
        try:
            payload = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return Outcome(
                completed,
                {},
                f"`{command.display()}` reported nothing — {completed.tail()}",
            )
        finally:
            output.unlink(missing_ok=True)
        if not isinstance(payload, dict):
            return Outcome(completed, {}, "the harness reported something unreadable")
        return Outcome(completed, payload, str(payload.get("error", "")))


def open_session(
    root: Path,
    *,
    venv: Path,
    first_party: Sequence[str],
    scratch: Path,
    policy: Policy,
    runner: Runner | None = None,
) -> Session:
    """Lay out the scratch directory and build the session that uses it.

    The scratch directory is deliberately *outside* the worktree. Everything in
    it — Polish's harness, the generated oracles, the JSON going each way —
    would otherwise show up in the sandbox's own `git status` and be swept into
    a refactoring commit, and the generated tests would be collected by the
    repository's suite during the very baseline that is supposed to measure it.
    """
    scratch = Path(scratch)
    (scratch / ORACLE_DIR).mkdir(parents=True, exist_ok=True)

    # Copied rather than imported: the sandbox virtualenv does not have Polish
    # installed, and installing it there would put Polish's own dependencies in
    # front of the repository's.
    shutil.copyfile(Path(_harness.__file__), scratch / HARNESS_FILE)

    (scratch / CONFIG_FILE).write_text(
        json.dumps(_configuration(root, first_party, scratch, policy)),
        encoding="utf-8",
    )
    (scratch / OUTPUT_FILE).unlink(missing_ok=True)

    return Session(
        python=interpreter(Path(venv)),
        policy=policy,
        scratch=scratch,
        runner=runner
        or sandbox_runner(
            root,
            venv=venv,
            home=scratch / "home",
            first_party=first_party,
            prepend=(scratch,),
            extra={
                _harness.CONFIG: str(scratch / CONFIG_FILE),
                _harness.OUTPUT: str(scratch / OUTPUT_FILE),
            },
        ),
    )


def _configuration(
    root: Path, first_party: Sequence[str], scratch: Path, policy: Policy
) -> dict:
    """The harness's parameters, including how to name a traced file.

    The file-to-module map is built here rather than in the sandbox because
    `polish.probe.modules` already answers it — a file's import root is found by
    walking outwards while `__init__.py` keeps appearing — and a second
    implementation inside the harness would be a second thing to keep true.
    Keyed by real path, because `co_filename` on a traced frame is whatever the
    interpreter resolved and symlinked checkouts are ordinary.
    """
    return {
        "modules": {
            str((Path(root) / module.path).resolve()): module.name
            for module in modules(Path(root), first_party)
        },
        "oracles": str(scratch / ORACLE_DIR),
        "samples": policy.samples_per_symbol,
        "chars": policy.value_chars,
        "depth": policy.value_depth,
        "total": policy.samples,
    }
