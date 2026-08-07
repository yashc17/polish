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

Early. Three commands work: `polish scan`, `polish observe` and `polish run`.
The run now makes edits: **Tier 0** removes unused imports it can prove are
safe to remove, one reviewable commit each.

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

### `polish run` — the safety envelope

```console
$ polish run
run       2026-08-07
commit    ada4626
snapshot  .polish/snapshots/raw/2026-08-07 (37 files)
mirrored  3 uncommitted files
sandbox   .polish/runs/2026-08-07 (removed)
branch    polish/2026-08-07
changes   2 committed, 1 dropped
  + refactor(imports): drop 1 unused import from app/loader.py
  + refactor(imports): drop 3 unused imports from app/cli.py
  - app/shared.py: dropped, the type check reported 1 new diagnostic, the
    first being — app/consumer.py: error: Module "app.shared" has no
    attribute "hashlib"
```

A run copies your first-party source to a dated raw snapshot, checks a dated
branch out into its own git worktree with its own virtualenv, works there, and
leaves your working tree byte-identical — `git status` reads exactly as it did
before. Everything Polish writes lives in `.polish/`, which ignores itself via
its own `.gitignore`; your `.gitignore` is never edited.

Polish needs a git repository, because the sandbox is a worktree and the review
output is a branch. In a directory that isn't one it refuses, prints the `git
init` line that fixes it, and offers `polish run --init-git` to do it for you on
explicit opt-in. There is no degraded, non-git mode — the reasoning, including
the two fallbacks that were rejected, is in
[ADR 0002](docs/adr/0002-refuse-non-git-repositories.md).

### Tier 0 — what "provably behaviour-preserving" means here

Polish tiers itself by what it can verify. **Tier 0** is the tier for a
repository it cannot execute — no test suite, needs API keys, needs a GPU — and
its claim is not "the tests passed", because there are no tests. It is that
every change is *proven*, by two gates, before it is committed:

- **AST-equivalence.** The file that reached the disk is the original minus
  exactly the bindings the edit claimed to remove. Comments and layout may
  move; nothing else may, and import order may not, because import order is
  behaviour.
- **A type check.** mypy over your whole first-party file set, gated on
  introducing no diagnostic that was not already there. That is what catches
  the removal that breaks a *different* file — `b.py` doing
  `from a import json` when `a.py`'s import goes. If the check cannot run at
  all, the run ships nothing and says so.

A change failing either gate is reverted and reported, never shipped.

**Unused is not removable, and this is the part worth knowing.** Importing a
module runs it, and running it can register a plugin, patch a builtin or set a
matplotlib backend. So Tier 0 removes an unused import only when the module
behind it is *provably inert*: the standard library, or one of your own modules
whose top level is only definitions and literals. `import numpy as np` stays —
Tier 0 cannot execute the repo, so it cannot rule out an import side effect —
and the reason is written into the commit message rather than left as silence.
`__init__.py` re-exports, `__future__` imports, star imports, `TYPE_CHECKING`
blocks, `try/except ImportError` probes and files that use `eval` or `globals()`
are all left alone.

**And if you have already said an import is deliberate, that settles it.** An
import carrying a `noqa` directive, or written `import x as x`, is never
touched — whatever Polish's own analysis concludes. This rule exists because
the first run against `psf/requests` produced two changes and *both were
wrong*: one of them deleted `import encodings.idna`, which is there to register
a codec before threads can race on it, and which the maintainers had marked
accordingly. `ruff --fix` would remove it too. See
[ADR 0005](docs/adr/0005-tier-0-removes-only-provably-inert-imports.md) and
[ADR 0004](docs/adr/0004-mypy-as-the-tier-0-type-check.md).

Each change is its own commit, carrying its evidence:

```
refactor(imports): drop 1 unused import from app/loader.py

Removes `json`: bound at module level, referenced nowhere, and imported from a
module that runs nothing when it is imported.

Left alone: `numpy` is neither first-party nor standard library, and Tier 0
cannot rule out an import side effect without executing the repository.

Tier: 0 (deterministic codemod, LibCST)
AST-equivalence: proven — the diff removes `json` and nothing else
Type check: clean — mypy reports no diagnostic that was not there before
```

No commit depends on any other, so you can drop one you disagree with and keep
the rest. The day's uncommitted work is committed first, on its own, so that
every refactoring commit above it is a diff of Polish's work alone.

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
│   ├── envelope/     #   snapshot, sandbox and branch: the safety envelope
│   ├── firstparty/   #   first-party source detection
│   ├── observer/     #   the daytime daemon: code graph and change history
│   └── tier0/        #   deterministic codemods, proven rather than tested
├── tests/            # suite, plus the hand-labelled detection corpus
├── scripts/          # corpus capture and reporting
└── docs/             # design docs and specs
```

## Development

```bash
uv venv && uv pip install -e . pytest ruff
.venv/bin/pytest
```
