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

Early. `polish scan` is the only working command.

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
