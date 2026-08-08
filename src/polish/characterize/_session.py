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
    NotAllowed,
    Runner,
    ensure_pytest,
    entrypoint_target,
    harness,
    interpreter,
    modules,
    opted_in,
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
    #: Gate 4's opted-in target, when the repository has one and the allowlist
    #: accepts it. `None` means the suite is the only execution source there is.
    entrypoint: str | None = None
    started: float = field(default_factory=monotonic)
    commands: list[str] = field(default_factory=list)

    @property
    def oracle_dir(self) -> Path:
        return self.scratch / ORACLE_DIR

    def run(self, task: str, seconds: float) -> Outcome:
        command = harness(self.python, task, seconds)
        return self._read(command, self.issue(command))

    def issue(self, command: Command) -> Completed:
        """Dispatch one allowlisted command against the clock, and log it.

        Every command characterization issues goes through here, so the budget
        and the run log are one place rather than one place per task. The
        allowlist is not re-checked and does not need to be: a `Command` that
        exists is a `Command` that was validated when it was constructed.
        """
        remaining = self.policy.total_seconds - (monotonic() - self.started)
        bounded = Command(
            kind=command.kind,
            python=command.python,
            arguments=command.arguments,
            timeout=min(command.timeout, max(remaining, 0.0)),
        )
        if remaining <= 0:
            raise Exhausted(
                f"the {self.policy.total_seconds:.0f}s characterization budget "
                f"was spent before `{bounded.display()}` could run"
            )
        self.commands.append(bounded.display())
        return self.runner(bounded)

    def ensure_pytest(self) -> str:
        """Put pytest in the sandbox, or say why the oracles cannot be replayed.

        Only the entry-point source needs this, and needs it badly. A repository
        promoted by gate 2 has pytest in its sandbox *because* gate 2 ran its
        suite; one promoted by gate 4 alone never reached that code, so nothing
        put pytest there — and Polish's generated oracles are pytest tests. Left
        unchecked, every entry-point repository would record real calls, write
        real oracles, and then discard all of them for a reason that belongs to
        the sandbox rather than to the repository (#32).
        """
        return ensure_pytest(
            self.python,
            self.issue,
            import_seconds=self.policy.import_seconds,
            install_seconds=self.policy.install_seconds,
        )

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
    target = _target(root)
    (scratch / ORACLE_DIR).mkdir(parents=True, exist_ok=True)

    # Copied rather than imported: the sandbox virtualenv does not have Polish
    # installed, and installing it there would put Polish's own dependencies in
    # front of the repository's.
    shutil.copyfile(Path(_harness.__file__), scratch / HARNESS_FILE)

    (scratch / CONFIG_FILE).write_text(
        json.dumps(_configuration(root, first_party, scratch, policy, target)),
        encoding="utf-8",
    )
    (scratch / OUTPUT_FILE).unlink(missing_ok=True)

    return Session(
        python=interpreter(Path(venv)),
        policy=policy,
        scratch=scratch,
        entrypoint=target,
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


def _target(root: Path) -> str | None:
    """Gate 4's entry point, or `None` — never a target the allowlist refuses.

    A target `polish.probe.entrypoint_target` will not accept is treated here as
    no target at all, rather than as a reason to decline. Gate 4 already refused
    it, in the verdict the user reads, and with the actionable message about the
    `polish.toml` line that caused it; raising a second time out of the harness
    would blame characterization for something the probe has already reported —
    and it would do so on repositories whose suite was going to be the execution
    source anyway, where the bad line changes nothing.
    """
    found = opted_in(Path(root))
    if found is None:
        return None
    try:
        return entrypoint_target(found)
    except NotAllowed:
        return None


def _configuration(
    root: Path,
    first_party: Sequence[str],
    scratch: Path,
    policy: Policy,
    target: str | None,
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
        # Repository-named, and therefore here rather than in the command: the
        # fifth shape's program has to stay a constant (ADR 0007), so the one
        # parameter #32 adds travels the same channel as all the others.
        "entrypoint": target,
        "samples": policy.samples_per_symbol,
        "chars": policy.value_chars,
        "depth": policy.value_depth,
        "total": policy.samples,
    }
