"""The tier probe — can Polish execute this repository, and therefore trust more?

`probe()` is the whole interface. Everything behind it is private.

SPEC §5 calls this the highest-risk unknown in the design, and its failure mode
is asymmetric in a way that decides the shape of everything here. A wrongly
pessimistic verdict costs a night of deeper refactoring. A wrongly optimistic
one produces characterization tests over code that never ran — the vacuous test
§3.1 calls "actively corrosive" — and attaches the claim "verified against
recorded behaviour" to a change nothing verified. So **every ambiguity, timeout,
error and refusal resolves downward**, and there is no path in this package that
resolves upward on anything but a repository actually executing.

The mechanics are four layered hard gates, decided in issue #3 and implemented
in `_gates`. The verdict they produce carries the evidence with it, because a
user whose repository landed Tier 0 is owed the reason.

The probe runs *inside the sandbox* (SPEC §3.3) and nowhere else. It installs
packages and executes the repository's test suite, which is not a thing to do to
someone's working tree — `polish.envelope.run()` is what calls it, after the
worktree and virtualenv exist.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from polish.firstparty import scan

from . import _commands, _discovery, _gates
from ._commands import (
    ENTRYPOINT,
    HARNESS,
    IMPORT_SMOKE,
    ORACLES,
    PIP_INSTALL,
    PYTEST,
    SUITE,
    TRACE,
    Command,
    Completed,
    NotAllowed,
    harness,
    interpreter,
)
from ._discovery import Module, modules
from ._gates import Runner, Session
from ._verdict import FAIL, PASS, SKIP, Budget, GateOutcome, Tier, TierVerdict

__all__ = [
    "ENTRYPOINT",
    "FAIL",
    "HARNESS",
    "IMPORT_SMOKE",
    "ORACLES",
    "PASS",
    "PIP_INSTALL",
    "PYTEST",
    "SKIP",
    "SUITE",
    "TRACE",
    "Budget",
    "Command",
    "Completed",
    "GateOutcome",
    "Module",
    "NotAllowed",
    "Runner",
    "Tier",
    "TierVerdict",
    "harness",
    "interpreter",
    "modules",
    "probe",
    "sandbox_runner",
]


def probe(
    root: Path,
    *,
    venv: Path,
    first_party: Iterable[str] | None = None,
    budget: Budget | None = None,
    runner: Runner | None = None,
) -> TierVerdict:
    """Classify the repository at `root`, using the virtualenv at `venv`.

    `root` is the sandbox worktree, not the user's tree.

    `runner` replaces the subprocess layer. It is public because the corpus
    replays hand-labelled repositories through it without executing them, and
    because a seam that only tests can reach is a seam nobody can inspect.
    Substituting one cannot widen what the probe is allowed to run: the
    allowlist is enforced when a `Command` is constructed, so a runner only ever
    receives commands that were already legal.
    """
    root = Path(root)
    files = tuple(first_party) if first_party is not None else tuple(scan(root).files)
    python = _commands.interpreter(Path(venv))

    with tempfile.TemporaryDirectory(prefix="polish-probe-") as home:
        session = Session(
            python=python,
            budget=budget or Budget(),
            runner=runner
            or sandbox_runner(root, venv=venv, first_party=files, home=Path(home)),
        )
        return _gates.resolve(root, files, session)


def sandbox_runner(
    root: Path,
    *,
    venv: Path,
    home: Path,
    first_party: Iterable[str] | None = None,
    prepend: Sequence[Path] = (),
    extra: Mapping[str, str] | None = None,
) -> Runner:
    """Real subprocesses, in the sandbox, with a built environment.

    `HOME` points at a scratch directory that dies with the caller rather than
    at the user's, and the environment is constructed rather than inherited, so
    an untrusted repository's test suite cannot read the API keys sitting in the
    shell that launched Polish. It costs pip's download cache; it buys a runner
    that can be pointed at a stranger's repository, which is the requirement.

    Public because the probe is not the only thing that has to execute inside
    the sandbox. The characterization harness (#13) has to run there too, and
    ADR 0006 says in as many words that anything downstream "inherits this
    allowlist rather than inventing a second one" — so what it inherits is this
    function, whole, rather than a second copy of the environment arithmetic
    that could drift from it.

    `prepend` goes ahead of the repository's own import roots on `PYTHONPATH`,
    which is how Polish's harness module is found before anything the repository
    ships. `extra` adds Polish-authored environment variables and nothing else.
    """
    root = Path(root)
    files = tuple(first_party) if first_party is not None else tuple(scan(root).files)
    home = Path(home)
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    roots = _discovery.import_roots(_discovery.modules(root, files))
    return _commands.Subprocess(
        cwd=root,
        env=_commands.environment(
            _commands.interpreter(Path(venv)),
            (*prepend, *(roots or (root,))),
            home,
            extra,
        ),
    )
