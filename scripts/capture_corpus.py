#!/usr/bin/env python3
"""Capture a real repository into a hand-labelled corpus manifest.

The corpus has to be real to be worth measuring against, and committed to be
worth regressing against. Vendoring whole repositories satisfies neither for
long, so a manifest stores the *shape* of a real tree — every path, and the
contents of only those files that detection actually reads.

Labelling is deliberately not done by Polish. The operator states the rule as
globs on the command line; the manifest records both the globs and the prose
reason, so a reviewer can check the labels without running the code they judge.
A label derived from `scan()` would make the metrics vacuous.

Usage:

    python scripts/capture_corpus.py /path/to/pip \\
        --name pip --source https://github.com/pypa/pip \\
        --not-first-party 'src/pip/_vendor/**' \\
        --note "Vendors its dependencies inside its own package." \\
        --reason "Everything pip wrote, which is everything outside _vendor."
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import date
from pathlib import Path

from pathspec import PathSpec

SOURCE_SUFFIXES = {".py", ".pyi"}

#: Files whose *contents* change what detection decides. Everything else is
#: captured as a path with empty contents.
CONTENT_CAPTURED = {
    "pyproject.toml",
    "setup.cfg",
    ".gitignore",
    "pyvenv.cfg",
    "polish.toml",
}

#: Repository plumbing that no scan should ever look at, and that would bloat
#: the manifest enormously. `.git/info/exclude` is the one exception.
SKIPPED_DIRS = {".git"}

BANNER_LINES = 5
BANNERS = ("@generated", "DO NOT EDIT")


def capture(root: Path, not_first_party: list[str]) -> tuple[dict, list[str]]:
    label_exclusions = PathSpec.from_lines("gitignore", not_first_party)
    files: dict[str, str] = {}
    first_party: list[str] = []

    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if _skipped(relative):
            continue
        if path.is_dir():
            if not any(path.iterdir()):
                files[f"{relative}/"] = ""
            continue
        if not path.is_file() or path.is_symlink():
            continue

        files[relative] = _contents(path)
        if path.suffix in SOURCE_SUFFIXES and not label_exclusions.match_file(relative):
            first_party.append(relative)

    exclude = root / ".git" / "info" / "exclude"
    if exclude.is_file():
        contents = _read(exclude)
        if contents.strip():
            files[".git/info/exclude"] = contents

    return files, first_party


def _skipped(relative: str) -> bool:
    return any(part in SKIPPED_DIRS for part in relative.split("/"))


def _contents(path: Path) -> str:
    """Full text for files detection reads; a banner header for generated code."""
    if path.name in CONTENT_CAPTURED:
        return _read(path)
    if path.suffix in SOURCE_SUFFIXES:
        header = "".join(_read(path).splitlines(keepends=True)[:BANNER_LINES])
        if any(banner in header for banner in BANNERS):
            return header
    return ""


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _commit(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--source", required=True, help="where the tree came from")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--not-first-party",
        action="append",
        default=[],
        metavar="GLOB",
        help="gitignore-style glob of paths a human judged NOT first-party",
    )
    parser.add_argument("--note", required=True, help="why this repo is in the corpus")
    parser.add_argument(
        "--reason", required=True, help="why the labels are what they are"
    )
    parser.add_argument(
        "--expect-error",
        action="store_true",
        help="scanning this repo must raise NotAPythonProject",
    )
    args = parser.parse_args()

    files, first_party = capture(args.repo.resolve(), args.not_first_party)
    manifest = {
        "name": args.name,
        "source": args.source,
        "commit": _commit(args.repo),
        "captured": date.today().isoformat(),
        "note": args.note,
        "label_rule": {
            "reason": args.reason,
            "first_party": "every .py/.pyi file, minus not_first_party",
            "not_first_party": args.not_first_party,
        },
        "expect_error": args.expect_error,
        "first_party": first_party,
        "files": files,
    }
    args.out.write_text(json.dumps(manifest, indent=1, sort_keys=False) + "\n")
    print(
        f"{args.name}: {len(files)} paths, "
        f"{len(first_party)} labelled first-party -> {args.out}"
    )


if __name__ == "__main__":
    main()
