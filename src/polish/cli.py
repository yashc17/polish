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

from polish import __version__
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "scan":
        return _scan(args.path)
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


def run() -> None:
    """Console-script entry point."""
    sys.exit(main())
