"""The `polish` command line.

Exit codes are part of the contract, because Polish is meant to be run
unattended from a scheduler:

    0  the command did what it said
    1  Polish declined, and said why on stderr
    2  the command line itself was wrong (argparse's own convention)
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from polish import __version__, envelope, observer
from polish.firstparty import NotAPythonProject, scan

EXIT_OK = 0
EXIT_DECLINED = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="polish",
        description="Autonomous night-shift refactoring agent.",
    )
    parser.add_argument("--version", action="version", version=f"polish {__version__}")
    subcommands = parser.add_subparsers(dest="command", required=True)

    scan_command = subcommands.add_parser(
        "scan",
        help="print the first-party source files Polish would refactor",
        description=(
            "Print the first-party file set for a repository: the files Polish "
            "considers the user's own source, one relative path per line. "
            "Virtualenvs, vendored code, generated files and build artifacts "
            "are excluded. Override the result in polish.toml."
        ),
    )
    scan_command.add_argument(
        "path",
        nargs="?",
        default=".",
        type=Path,
        help="repository root (default: the current directory)",
    )

    observe_command = subcommands.add_parser(
        "observe",
        help="run and inspect the observer daemon",
        description=(
            "The observer daemon watches the working tree during the day and "
            "maintains the code graph and the intra-day change history in "
            f"{observer.STORE_DIR}/. It is an ordinary process holding an "
            "ordinary pidfile: `watch` runs it in the foreground, `start` "
            "detaches it, and `kill` works as well as `stop` does."
        ),
    )
    observe_command.add_argument(
        "action",
        choices=("start", "stop", "status", "watch", "compact"),
        help=(
            "start: detach a daemon · stop: signal it · status: what is stored "
            "and where · watch: run in the foreground · compact: apply "
            "retention and reclaim space"
        ),
    )
    observe_command.add_argument(
        "path",
        nargs="?",
        default=".",
        type=Path,
        help="repository root (default: the current directory)",
    )

    run_command = subcommands.add_parser(
        "run",
        help="perform a night-shift run: snapshot, sandbox, branch",
        description=(
            "Perform a night-shift run. Polish copies the first-party source "
            "to a dated raw snapshot, checks a dated branch out into its own "
            "worktree with its own virtualenv, probes whether the repository "
            "can be executed, does its work there, and leaves the working tree "
            "byte-identical. The tier verdict and every command the probe ran "
            "are printed with the run. This release removes unused imports at "
            "Tier 0: every change is proven behaviour-preserving by "
            "AST-equivalence and a type check before it is committed, and one "
            "that fails either is reverted and reported rather than shipped."
        ),
    )
    run_command.add_argument(
        "path",
        nargs="?",
        default=".",
        type=Path,
        help="repository root (default: the current directory)",
    )
    run_command.add_argument(
        "--init-git",
        action="store_true",
        help=(
            "run git init and commit the tree first, if it is not a repository "
            "yet — never done without this flag"
        ),
    )
    run_command.add_argument(
        "--keep-sandbox",
        action="store_true",
        help="leave the worktree and virtualenv in place for inspection",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "scan":
        return _scan(args.path)
    if args.command == "observe":
        return _observe(args.action, args.path)
    if args.command == "run":
        return _run(args.path, init_git=args.init_git, keep_sandbox=args.keep_sandbox)
    raise AssertionError(f"unhandled command: {args.command}")  # pragma: no cover


def _scan(path: Path) -> int:
    try:
        result = scan(path)
    except NotAPythonProject as declined:
        print(f"polish: {declined}", file=sys.stderr)
        return EXIT_DECLINED
    for relative in result.files:
        print(relative)
    return EXIT_OK


def _observe(action: str, path: Path) -> int:
    """Drive the daemon. Only `watch` and `compact` need a Python project.

    `status` and `stop` deliberately do not: someone who has just found a
    `.polish/` directory in a repo Polish no longer recognises still needs to be
    able to ask what it is and make it go away.
    """
    root = Path(path).resolve()
    if action == "status":
        print(observer.describe(root))
        return EXIT_OK
    if action == "stop":
        pid = observer.stop(root)
        print(f"stopped pid {pid}" if pid else f"no observer running for {root}")
        return EXIT_OK
    try:
        if action == "start":
            pid = observer.start(root)
            print(f"observing {root} (pid {pid})")
        elif action == "watch":
            observer.observe(root)
        elif action == "compact":
            observer.compact(root)
    except (NotAPythonProject, observer.ObserverFailedToStart) as declined:
        print(f"polish: {declined}", file=sys.stderr)
        return EXIT_DECLINED
    return EXIT_OK


def _run(path: Path, *, init_git: bool, keep_sandbox: bool) -> int:
    try:
        record = envelope.run(path, init_git=init_git, keep_sandbox=keep_sandbox)
    except (NotAPythonProject, envelope.EnvelopeError) as declined:
        print(f"polish: {declined}", file=sys.stderr)
        return EXIT_DECLINED
    for line in record.summary():
        print(line)
    return EXIT_OK


def run() -> None:
    """Console-script entry point."""
    sys.exit(main())
