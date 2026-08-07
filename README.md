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

Early. `polish scan` and `polish observe` work; the night-shift run does not
exist yet.

### `polish observe` — the daytime observer

```console
$ polish observe start
observing /home/you/repo (pid 48213)

$ polish observe status
polish observer — /home/you/repo
  status    running (pid 48213)
  store     /home/you/repo/.polish/graph.db (412.0 KB of 100.0 MB ceiling)
  schema    version 1
  tracking  61 first-party files, 803 symbols (12 since deleted)
  history   1284 change events since 2026-07-24; 14 days retained
  uptime    2 window(s), 1 gap(s):
              2026-08-06 14:02–2026-08-06 17:31
            churn UNKNOWN for 12 file(s) — never read as 'no churn'
  inspect   sqlite3 /home/you/repo/.polish/graph.db .schema
  stop      polish observe stop /home/you/repo   (or: kill 48213)
```

It maintains the **code graph** and the **intra-day change history** — what
`git diff` erases: code written at 2pm and deleted at 5pm, and how often each
function was rewritten. A night run needs that to know which refactors are safe.

Everything it keeps lives in `.polish/`, which carries its own `.gitignore`
containing `*` — so it stays out of `git status` **without Polish ever editing
your `.gitignore`**. It is a plain SQLite database, so `sqlite3
.polish/graph.db .schema` is the whole inspection story, and `kill $(cat
.polish/daemon.pid)` works as well as `polish observe stop` does. Delete the
directory and nothing breaks: the graph rebuilds from your tree.

**The one thing worth knowing:** the graph is verifiable and the history is not.
If the observer was not running when something changed, Polish reports that
file's churn as **UNKNOWN, never as "no churn"** — reading absent data as quiet
would let it confidently deduplicate a function that had been rewritten four
times. See
[ADR 0003](docs/adr/0003-observer-state-in-a-self-ignoring-sqlite-store.md).

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

## Layout

```
polish/
├── src/polish/       # core library
│   ├── cli.py        #   the polish command
│   ├── firstparty/   #   first-party source detection
│   └── observer/     #   the daytime daemon: code graph and change history
├── tests/            # suite, plus the hand-labelled detection corpus
├── scripts/          # corpus capture and reporting
└── docs/             # design docs and specs
```

## Development

```bash
uv venv && uv pip install -e . pytest ruff
.venv/bin/pytest
```
