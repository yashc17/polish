#!/usr/bin/env python3
"""Capture a real repository into a hand-labelled tier-probe manifest.

`capture_corpus.py` records the *shape* of a tree, which is all detection needs
because detection only reads files. The probe executes: it installs, it runs a
suite, it imports modules. So a probe manifest records the shape **plus what
those commands actually did** on a real machine, once, so that the corpus can be
replayed offline, deterministically, and in CI.

    python scripts/capture_probe_corpus.py /tmp/nanoGPT \\
        --name nanogpt --source https://github.com/karpathy/nanoGPT \\
        --out tests/corpus/probe/nanogpt.json \\
        --expected-tier 0 \\
        --note   "Needs a GPU, and has no test suite at all." \\
        --reason "No test_*.py anywhere and no polish.toml, so no execution source."

**The label is not the recording.** `--expected-tier` is stated by a human who
read the repository, next to a prose reason, exactly as `--not-first-party` is in
the detection corpus. The recording says what the commands did; the label says
what the verdict should be. A label derived from the probe would measure nothing.

**What a recording does not prove.** It was made on one machine, on one date,
with one Python. `pip install torch` failing is a fact about that machine as much
as about the repository, and a machine with a GPU would record something else.
That is stated in `tests/corpus/README.md` rather than papered over, and it is
why `tests/test_probe.py` exists: replay can never catch a runner mutated into a
no-op, so the claims that are about execution really happening live there.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import venv
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from capture_corpus import _commit, capture  # noqa: E402

from polish.firstparty import scan  # noqa: E402
from polish.probe import Budget, Command, Completed, probe  # noqa: E402
from polish.probe._commands import (  # noqa: E402
    ENTRYPOINT,
    IMPORT_SMOKE,
    PIP_INSTALL,
    PYTEST,
)

#: Enough of a command's output to be evidence, little enough to commit. pytest
#: puts its pass count in the last line, which is the part the gate reads.
KEPT_CHARACTERS = 4000


class Recorder:
    """Runs commands for real, and reduces them to a replayable environment.

    Raw `(argv -> outcome)` pairs would be a brittle recording: the import smoke
    batches modules, so every key would depend on `_gates.BATCH` and on module
    ordering, and a change to either would silently turn the corpus into a set
    of cache misses. Reducing to per-module outcomes instead is lossless, since
    a passing batch means every member passed and a failing one is re-run member
    by member anyway.
    """

    def __init__(self, inner):
        self.inner = inner
        self.install: dict | None = None
        self.pytest: dict | None = None
        self.entrypoint: dict | None = None
        self.pytest_available = False
        self.imports: dict[str, int] = {}

    def __call__(self, command: Command) -> Completed:
        result = self.inner(command)
        self._observe(command, result)
        return result

    def _observe(self, command: Command, result: Completed) -> None:
        tail = command.arguments[len(_prefix(command.kind)) :]
        if command.kind == PIP_INSTALL:
            if tuple(tail) == ("pytest",):
                self.pytest_available = result.ok
            else:
                self.install = _outcome(result)
        elif command.kind == PYTEST:
            self.pytest = _outcome(result)
        elif command.kind == ENTRYPOINT:
            self.entrypoint = _outcome(result)
        elif command.kind == IMPORT_SMOKE:
            self._observe_imports(tail[0].removeprefix("import ").split(", "), result)

    def _observe_imports(self, names: list[str], result: Completed) -> None:
        if names == ["pytest"]:
            self.pytest_available = result.ok
        elif len(names) == 1:
            self.imports[names[0]] = 0 if result.ok else 1
        elif result.ok:
            # A batch that imported cleanly settles every module in it. A batch
            # that failed settles nothing: the individual re-runs that follow do.
            self.imports.update(dict.fromkeys(names, 0))

    def recorded(self) -> dict:
        return {
            "install": self.install,
            "pytest_available": self.pytest_available,
            "pytest": self.pytest,
            "entrypoint": self.entrypoint,
            # Only the failures are stored; anything unlisted imported cleanly.
            "unimportable": sorted(
                name for name, code in self.imports.items() if code != 0
            ),
            "imported": len(self.imports),
        }


def _prefix(kind: str) -> tuple[str, ...]:
    from polish.probe._commands import SHAPES

    return SHAPES[kind]


def _outcome(result: Completed) -> dict:
    return {
        "code": result.code,
        "timed_out": result.timed_out,
        "output": result.output[-KEPT_CHARACTERS:],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--source", required=True, help="where the tree came from")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--expected-tier",
        type=int,
        choices=(0, 1),
        required=True,
        help="the tier a human says this repository should get",
    )
    parser.add_argument("--note", required=True, help="why this repo is in the corpus")
    parser.add_argument("--reason", required=True, help="why the label is what it is")
    parser.add_argument(
        "--shape",
        default=None,
        help="name of an existing detection manifest to reuse instead of "
        "capturing the tree again (default: capture it)",
    )
    parser.add_argument("--total-seconds", type=float, default=1800.0)
    args = parser.parse_args()

    root = args.repo.resolve()
    sandbox = root.parent / f".probe-venv-{args.name}"
    print(f"building {sandbox}", flush=True)
    venv.EnvBuilder(with_pip=True, symlinks=os.name != "nt", clear=True).create(sandbox)

    first_party = scan(root)
    print(f"probing {root} ({len(first_party)} first-party files)", flush=True)

    recorder: Recorder | None = None

    def runner(command: Command) -> Completed:
        assert recorder is not None
        print(f"  {command.display()[:110]}", flush=True)
        return recorder(command)

    from polish.probe import _commands, _discovery

    home = sandbox / "home"
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    roots = _discovery.import_roots(_discovery.modules(root, first_party.files))
    recorder = Recorder(
        _commands.Subprocess(
            cwd=root,
            env=_commands.environment(
                _commands.interpreter(sandbox), roots or (root,), home
            ),
        )
    )

    verdict = probe(
        root,
        venv=sandbox,
        first_party=first_party.files,
        budget=Budget(total_seconds=args.total_seconds),
        runner=runner,
    )

    manifest = {
        "name": args.name,
        "source": args.source,
        "commit": _commit(root),
        "captured": date.today().isoformat(),
        "captured_on": f"{platform.system().lower()} python{platform.python_version()}",
        "note": args.note,
        "label_rule": {"reason": args.reason, "expected_tier": args.expected_tier},
        "recorded": recorder.recorded(),
        # What the probe said at capture time. Not a label — a reviewer reading
        # a manifest whose observed tier differs from its label is looking at
        # either a bug or a deliberately hard case, and should be able to tell.
        "observed_at_capture": {
            "tier": int(verdict.tier),
            "reason": verdict.reason,
            "gates": [
                f"{gate.number} {gate.status} {gate.name}: {gate.detail}"
                for gate in verdict.gates
            ],
        },
    }
    if args.shape:
        manifest["shape"] = args.shape
    else:
        files, labelled = capture(root, [])
        manifest["files"] = files
        manifest["first_party"] = labelled

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, indent=1, sort_keys=False) + "\n")
    print(
        f"\n{args.name}: labelled Tier {args.expected_tier}, "
        f"observed Tier {int(verdict.tier)} -> {args.out}"
    )
    for line in verdict.summary():
        print(f"  {line}")


if __name__ == "__main__":
    main()
