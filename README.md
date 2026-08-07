# Polish

[![CI](https://github.com/yashc17/polish/actions/workflows/ci.yml/badge.svg)](https://github.com/yashc17/polish/actions/workflows/ci.yml)

Autonomous "night-shift" code cleanup and refactoring agent.

Polish runs asynchronously in a sandboxed environment to review the day's
changes, refactor messy logic, generate documentation, update dependencies,
and verify results — leaving the codebase clean, tested, and ready for the
next day.

## Core capabilities

- **Persistent AST & code graph observer** — tracks code changes throughout
  the day and maintains an up-to-date structural map of the codebase.
- **Daily "time machine" snapshots** — saves a raw copy of the codebase
  before any nightly cleanup runs (`snapshots/raw/YYYY-MM-DD`).
- **Autonomous night-shift refactoring pipeline** — generates regression
  tests, refactors and deduplicates code, verifies functional parity, and
  updates documentation.
- **Simple CLI** — a single `polish run` command to kick off a nightly pass.

## Status

Early. Two commands work: `polish scan` and `polish run`.

### `polish run` — the safety envelope

```console
$ polish run
run       2026-08-06
commit    ab394d4
snapshot  .polish/snapshots/raw/2026-08-06 (37 files)
mirrored  3 uncommitted files
sandbox   .polish/runs/2026-08-06 (removed)
branch    polish/2026-08-06
changes   0 (this release ships the safety envelope only)
```

A run copies your first-party source to a dated raw snapshot, checks a dated
branch out into its own git worktree with its own virtualenv, works there, and
leaves your working tree byte-identical — `git status` reads exactly as it did
before. **This release makes no edits.** The envelope is the payload: the
refactoring is the commodity, and the guarantee around it is the point, so it
gets built and proven first.

Everything Polish writes lives in `.polish/`, which ignores itself via its own
`.gitignore` — your `.gitignore` is never edited.

Polish needs a git repository, because the sandbox is a worktree and the review
output is a branch. In a directory that isn't one it refuses, prints the `git
init` line that fixes it, and offers `polish run --init-git` to do it for you on
explicit opt-in. There is no degraded, non-git mode — the reasoning, including
the two fallbacks that were rejected, is in
[ADR 0002](docs/adr/0002-refuse-non-git-repositories.md).

### `polish scan` — the first-party file set

```console
$ polish scan
src/polish/cli.py
src/polish/firstparty/__init__.py
...
```

It prints the **first-party file set** — the files Polish considers your own
source and would be willing to refactor. Virtualenvs, vendored code, generated
files and build artifacts are excluded. Resolution is layered: packaging
metadata, then a gitignore-aware walk, then environment marker files, then your
own overrides in `polish.toml`:

```toml
[scan]
exclude = ["demo/migrations/**"]
include = ["third_party/ours/**"]   # wins over everything else
```

Detection is measured against a corpus of hand-labelled real repositories
(`tests/corpus/`) and held to **precision 1.00, recall ≥ 0.95** by a regression
test. Run `python scripts/corpus_report.py` to see the current numbers.

## Safety — what is isolated, and what is not

Polish is an unattended agent, so be clear-eyed about what it does and does not
contain. Two things want isolating, and only one of them is.

**Your working tree is isolated.** Polish never edits it. It reads your source,
copies it to a snapshot, and does all of its work in a separate git worktree.
After a run, your tree is byte-identical and `git status` is unchanged. This is
covered by tests, not just by intent.

**Your machine is not isolated.** The worktree runs your repository's real code
— its test suite, its imports, its entry points — as your user, against your
real filesystem, with your real environment. There is no container and no VM.
Containment is by command allowlist only, and *that allowlist does not exist
yet*: it arrives with the Tier 1 edit loop. Until then, treat `polish run` as
having exactly the privileges you do.

This is deliberate, and it is the design's weakest guarantee. Docker would give
real isolation but fails exactly where autonomous refactoring matters most —
CUDA repos, large local datasets, mounted credentials. Safe-by-default is a lie
if it doesn't run: a container that forces every ML repo down to the weaker
verification tier has made the product worse while sounding safer. The reasoning
is in [SPEC §3.3](docs/SPEC.md).

## Layout

```
polish/
├── src/polish/       # core library
│   ├── cli.py        #   the polish command
│   └── firstparty/   #   first-party source detection
├── tests/            # suite, plus the hand-labelled detection corpus
├── scripts/          # corpus capture and reporting
└── docs/             # design docs and specs
```

## Development

```bash
uv venv && uv pip install -e . pytest ruff
.venv/bin/pytest
```
