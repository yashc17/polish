"""The command allowlist — the only things the probe is allowed to execute.

SPEC §3.3 is honest that the machine is not isolated: the sandbox runs the
repository's real code against the real filesystem, and containment is a command
allowlist rather than a container. Until now that allowlist did not exist, and
the README said so. This module is it.

The rule it enforces is narrow and mechanical: **the repository never supplies a
program.** Every command is the sandbox virtualenv's own interpreter in one of
five fixed shapes:

    <venv>/bin/python -m pip install --only-binary :all: <requirement>...
    <venv>/bin/python -m pytest -q --tb=no -p no:cacheprovider
    <venv>/bin/python -c "import <module>"
    <venv>/bin/python -m <module>          | <script>.py     (gate 4, opt-in)
    <venv>/bin/python -P -c "<one of Polish's own harness programs>"

A `Command` cannot be constructed outside those shapes — validation is in
`__post_init__` rather than at dispatch, so an ill-formed command has no moment
in which it exists. That matters more than it sounds: the dispatch check is the
one a refactor deletes by accident.

What the allowlist does **not** contain is the honest part:

* Running a test suite *is* running the repository's code. `conftest.py` executes
  at collection and `addopts` in the repo's own config reaches pytest without
  passing through argv. Gate 2 accepts this knowingly; it is the same bargain
  SPEC §3.2 already made for Tier 1 edits.
* Requirement *files* are repo-authored, and pip reads options out of them
  (`--index-url`, `-e .`). So the probe never passes `-r`: `_discovery` parses
  the files itself and forwards only requirement specifiers, which is why the
  validator below can insist no argument begins with `-`.
* `--only-binary :all:` forbids building from source, so no third-party
  `setup.py` runs either. Repos that need a source build fail gate 1 and land
  Tier 0 — the acceptable error, not the dangerous one.

The fifth shape arrived with #13 and is the one exception that proves the rule:
its program text is a **constant in this file**, compared for exact equality by
the validator, so the command provably carries no repository input at all. See
`docs/adr/0007-a-polish-authored-harness-inside-the-allowlist.md`, which amends
ADR 0006 rather than routing around it.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

PIP_INSTALL = "pip-install"
PYTEST = "pytest"
IMPORT_SMOKE = "import-smoke"
ENTRYPOINT = "entrypoint"
HARNESS = "harness"

#: The module Polish copies into a scratch directory of its own and puts first
#: on `PYTHONPATH`. `-P` in the shape below is what makes the name unambiguous:
#: without it the repository's own directory leads `sys.path` for `python -c`,
#: and a repository file of this name would supply the program instead.
HARNESS_MODULE = "_polish_harness"

#: What Polish's harness is asked to do. The task is in the *program text* and
#: not in an argument, so the run log distinguishes the three without the
#: allowlist having to admit a single repository-influenced character.
SUITE = "suite"
TRACE = "trace"
ORACLES = "oracles"

#: The only three programs the fifth shape may carry. Compared for equality, so
#: this dictionary is the entire vocabulary — nothing is assembled at runtime.
HARNESS_PROGRAMS: Mapping[str, str] = {
    task: f"import {HARNESS_MODULE} as _h; _h.{task}()"
    for task in (SUITE, TRACE, ORACLES)
}

#: The fixed argument prefix each kind must begin with. The repository can
#: influence what follows, never this.
SHAPES: Mapping[str, tuple[str, ...]] = {
    PIP_INSTALL: ("-m", "pip", "install", "--only-binary", ":all:"),
    PYTEST: ("-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider"),
    IMPORT_SMOKE: ("-c",),
    ENTRYPOINT: (),
    HARNESS: ("-P", "-c"),
}

#: A dotted Python module path and nothing else. Anything a shell would find
#: interesting fails this, which is the point.
MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")

#: Host variables the child keeps. Everything else is dropped, so an untrusted
#: repository's test suite cannot read the API keys sitting in the user's shell
#: — which is also why the corpus's "needs API keys" repo behaves realistically.
PASSTHROUGH = ("SYSTEMROOT", "COMSPEC", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH")


class NotAllowed(Exception):
    """Raised when something asked for a command outside the allowlist.

    Reaching this is a bug or an attack, never a repository being unusual, so it
    is an exception rather than a downward tier resolution. `probe()` still
    converts it into Tier 0 at the boundary: an unattended run must not die of
    it, but it must not quietly proceed either.
    """


@dataclass(frozen=True)
class Command:
    """One allowlisted invocation, validated at construction."""

    kind: str
    python: Path
    arguments: tuple[str, ...]
    timeout: float

    def __post_init__(self) -> None:
        _validate(self)

    @property
    def argv(self) -> tuple[str, ...]:
        return (str(self.python), *self.arguments)

    @property
    def tail(self) -> tuple[str, ...]:
        """The part of the command the repository influenced, if any.

        Public because `Runner` is public: anyone writing one — the corpus
        replay, a future dry-run mode — needs to read what was asked for without
        reimplementing the prefix arithmetic, and a seam that forces callers
        into private modules is not really a seam.
        """
        return self.arguments[len(SHAPES[self.kind]) :]

    @property
    def modules(self) -> tuple[str, ...]:
        """The modules an import smoke asks for. Empty for every other kind."""
        if self.kind != IMPORT_SMOKE:
            return ()
        return tuple(self.tail[0].removeprefix("import ").split(", "))

    def display(self) -> str:
        """The command as it belongs in a run log.

        The interpreter is shown as `python` rather than as an absolute path
        into a sandbox directory that no longer exists by the time anyone reads
        this. The arguments are verbatim: they are the evidence.
        """
        return " ".join(("python", *self.arguments))


@dataclass(frozen=True)
class Completed:
    """What a command did. `timed_out` is separate from `code` on purpose.

    A timeout and a non-zero exit both resolve downward, but they are different
    facts about a repository and the run log has to be able to say which
    happened (issue #12, acceptance criterion 2).
    """

    code: int
    output: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out

    def tail(self, lines: int = 3) -> str:
        """The last few lines of output — enough evidence to act on, in a log."""
        if self.timed_out:
            return "timed out"
        kept = [line.strip() for line in self.output.splitlines() if line.strip()]
        return " / ".join(kept[-lines:]) if kept else f"exit {self.code}"


def pip_install(python: Path, requirements: Sequence[str], timeout: float) -> Command:
    return Command(
        kind=PIP_INSTALL,
        python=python,
        arguments=(*SHAPES[PIP_INSTALL], *requirements),
        timeout=timeout,
    )


def pytest(python: Path, timeout: float) -> Command:
    """Run the repository's suite. Note there is no repo input in argv at all.

    Test paths are deliberately not passed: pytest's own discovery from the
    rootdir is what the repository's authors configured, and second-guessing it
    would mean the probe measures a suite the user does not have.
    """
    return Command(
        kind=PYTEST, python=python, arguments=SHAPES[PYTEST], timeout=timeout
    )


def import_smoke(python: Path, modules: Sequence[str], timeout: float) -> Command:
    """`import a, b, c` — several modules per interpreter start.

    Batched because gate 3 imports every first-party module, and a repository
    with six hundred of them would otherwise spend its whole time budget on
    process startup and be demoted to Tier 0 for the probe's own cost rather
    than for anything about the repository. Python stops at the first failing
    name, so `_gates` re-runs a failed batch one module at a time to say which.
    """
    return Command(
        kind=IMPORT_SMOKE,
        python=python,
        arguments=("-c", f"import {', '.join(modules)}"),
        timeout=timeout,
    )


def entrypoint(python: Path, target: str, timeout: float) -> Command:
    """Gate 4's opt-in target: a dotted module, or a `.py` path inside the repo.

    Never a shell string. A user who wants `make test` does not get it here —
    that is a command the repository authored, and the whole allowlist exists to
    keep those out of an unattended run.
    """
    arguments = ("-m", target) if MODULE.match(target) else (target,)
    return Command(kind=ENTRYPOINT, python=python, arguments=arguments, timeout=timeout)


def harness(python: Path, task: str, timeout: float) -> Command:
    """Polish's own program, running one of three fixed tasks (#13).

    The characterization harness needs two things the four original shapes
    cannot express: per-test outcomes, which `pytest -q` does not print, and a
    `sys.settrace` tracer, which no pytest command line can install. ADR 0007
    admits it as a fifth shape rather than letting either need be met by
    widening one of the four.

    What keeps it inside the allowlist's rule is that the program is a constant
    in this module, compared for equality below. There is nothing for a
    repository to influence: parameters reach the harness through the
    environment Polish builds, and `-P` keeps the repository's own directory off
    `sys.path` so it cannot supply `_polish_harness` itself.
    """
    program = HARNESS_PROGRAMS.get(task)
    if program is None:
        raise NotAllowed(
            f"{task!r} is not one of Polish's harness tasks "
            f"({', '.join(sorted(HARNESS_PROGRAMS))})."
        )
    return Command(
        kind=HARNESS,
        python=python,
        arguments=(*SHAPES[HARNESS], program),
        timeout=timeout,
    )


@dataclass(frozen=True)
class Subprocess:
    """The real runner. Pinned to a directory and a minimal environment.

    `probe(runner=...)` takes any callable of the same shape, which is how the
    corpus replays hand-labelled repositories without executing them. Note that
    swapping the runner cannot widen the allowlist: validation lives in
    `Command`, so a runner only ever receives commands that were already legal.
    """

    cwd: Path
    env: Mapping[str, str] = field(default_factory=dict)

    def __call__(self, command: Command) -> Completed:
        try:
            completed = subprocess.run(
                command.argv,
                cwd=self.cwd,
                env=dict(self.env),
                capture_output=True,
                text=True,
                timeout=command.timeout,
                check=False,
                # No shell, ever, and no inherited stdin: an unattended run must
                # never be a process sitting at a prompt nobody will answer.
                shell=False,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return Completed(code=-1, output="", timed_out=True)
        except OSError as failure:
            # A missing interpreter or an unexecutable file is a fact about the
            # sandbox, not a crash worth ending the night over.
            return Completed(code=-1, output=str(failure))
        return Completed(
            code=completed.returncode, output=completed.stdout + completed.stderr
        )


def environment(
    python: Path,
    roots: Sequence[Path],
    home: Path,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The child's whole environment, built rather than inherited.

    Inheriting `os.environ` would hand an untrusted repository's test suite
    every credential in the user's shell. Building it instead costs pip's cache
    (`HOME` points at a scratch directory that dies with the run) and buys a
    probe that can be pointed at a stranger's repo, which is the requirement.

    `extra` is how the fifth shape's harness is parameterised (#13). It is
    applied last and is *Polish-authored only* — the point of putting the
    harness's parameters here rather than in `argv` is that the command stays a
    constant, so anything a repository influenced would defeat the exercise.
    """
    built = {
        "PATH": os.pathsep.join([str(python.parent), "/usr/bin", "/bin"]),
        "HOME": str(home),
        "TMPDIR": str(home / "tmp"),
        "PYTHONPATH": os.pathsep.join(str(root) for root in roots),
        # Bytecode in the sandbox would be swept into a refactoring commit.
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "LANG": "C.UTF-8",
        "VIRTUAL_ENV": str(python.parent.parent),
        # pip's progress bars and version nags are noise in an overnight log.
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
    }
    for name in PASSTHROUGH:
        if name in os.environ:
            built[name] = os.environ[name]
    built.update(extra or {})
    return built


def interpreter(venv: Path) -> Path:
    """The virtualenv's own python. Windows puts it somewhere else."""
    candidate = venv / "Scripts" / "python.exe"
    return candidate if candidate.exists() else venv / "bin" / "python"


def _validate(command: Command) -> None:
    shape = SHAPES.get(command.kind)
    if shape is None:
        raise NotAllowed(f"{command.kind!r} is not an allowlisted command kind.")
    if command.arguments[: len(shape)] != shape:
        raise NotAllowed(
            f"{command.kind} commands must begin {' '.join(shape)!r}; got "
            f"{' '.join(command.arguments)!r}."
        )
    _validate_tail(command, command.arguments[len(shape) :])


def _validate_tail(command: Command, tail: tuple[str, ...]) -> None:
    if command.kind == PIP_INSTALL:
        # `_discovery` strips option lines out of requirement files precisely so
        # that this can be an absolute rule.
        offending = [item for item in tail if item.startswith("-")]
        if offending:
            raise NotAllowed(
                "pip is never passed repository-supplied options, because a "
                f"requirements file can carry --index-url or -e .; got {offending}."
            )
    elif command.kind == PYTEST:
        if tail:
            raise NotAllowed(f"pytest takes no further arguments; got {list(tail)}.")
    elif command.kind == IMPORT_SMOKE:
        if len(tail) != 1 or not tail[0].startswith("import "):
            raise NotAllowed(f"import smoke must be a bare import; got {list(tail)}.")
        names = tail[0].removeprefix("import ").split(", ")
        offending = [name for name in names if not MODULE.match(name)]
        if not names or offending:
            raise NotAllowed(
                f"import smoke takes plain dotted module paths; {offending} "
                "is not one. Nothing else may be smuggled into `python -c`."
            )
    elif command.kind == ENTRYPOINT:
        _validate_entrypoint(tail)
    elif command.kind == HARNESS:
        # Equality against a constant, not a pattern over one. A shape whose
        # tail is checked for *structure* can always be argued into admitting
        # one more thing; a shape whose tail must equal a string in this file
        # cannot, and that is the whole reason ADR 0007 was allowed to exist.
        if len(tail) != 1 or tail[0] not in set(HARNESS_PROGRAMS.values()):
            raise NotAllowed(
                "the harness shape carries one of Polish's own programs and "
                f"nothing else; got {list(tail)}."
            )


def _validate_entrypoint(tail: tuple[str, ...]) -> None:
    if tail[:1] == ("-m",):
        if len(tail) != 2 or not MODULE.match(tail[1]):
            raise NotAllowed(f"{list(tail)} is not `-m <dotted.module>`.")
        return
    if len(tail) != 1:
        raise NotAllowed(
            "an entry point is one module or one script, never a command line; "
            f"got {list(tail)}. Shell strings are not allowlisted."
        )
    script = tail[0]
    if (
        not script.endswith(".py")
        or script.startswith(("-", "/"))
        or ".." in Path(script).parts
    ):
        raise NotAllowed(
            f"{script!r} is not a repository-relative .py script. An entry point "
            "must be a dotted module or a .py file inside the repository."
        )
