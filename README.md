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

Early. Three commands work: `polish scan`, `polish observe` and
`polish run`. The run ships the safety envelope and the tier probe — it makes no
edits yet.

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
commit    d8b62a3
snapshot  .polish/snapshots/raw/2026-08-07 (35 files)
mirrored  3 uncommitted files
sandbox   .polish/runs/2026-08-07 (removed)
branch    polish/2026-08-07
tier      1 (characterization)
reason    a test suite ran, and first-party modules import cleanly
gate 1    pass venv: 3 declared requirements installed
gate 2    pass suite: 113 passing tests across 7 test files
gate 3    pass imports: 24 first-party modules import cleanly — which promotes nothing (SPEC §3.1)
gate 4    skip entrypoint: not configured; never run without being asked
ran       python -m pip install --only-binary :all: pathspec>=1.0 pytest>=8 ruff>=0.6
ran       python -c import pytest
ran       python -m pytest -q --tb=no -p no:cacheprovider
ran       python -c import polish, polish.cli, polish.envelope, ...
changes   0 (this release ships the safety envelope only)
```

A run copies your first-party source to a dated raw snapshot, checks a dated
branch out into its own git worktree with its own virtualenv, works there, and
leaves your working tree byte-identical — `git status` reads exactly as it did
before. **This release makes no edits.** The envelope is the payload: the
refactoring is the commodity, and the guarantee around it is the point, so it
gets built and proven first.

### The tier probe — what Polish is allowed to claim about your repo

Before anything else happens in the sandbox, Polish works out whether it can
*execute* your repository, because that decides what it is allowed to promise:

| | **Tier 0** — cannot execute it | **Tier 1** — it runs |
|---|---|---|
| Verification | AST-equivalence + type check | characterization tests from real execution |
| Claim | "provably behaviour-preserving" | "verified against recorded behaviour" |

Four hard gates decide it, in order. No scores and no thresholds — a repository
cannot average its way into Tier 1:

1. **venv** — do your declared dependencies install? No → Tier 0. This gate runs
   no code of yours at all.
2. **suite** — is there a test suite, does it run, does anything pass? Yes → a
   Tier 1 candidate.
3. **imports** — does every first-party module import? No → Tier 0. **Yes →
   nothing.** An import smoke can take Tier 1 away; it can never grant it.
4. **entrypoint** — only what you named in `polish.toml`, never inferred.

**Gate 3 being demote-only is the point of the design.** Importing a module
traces constants and definitions — it never enters a function body. A repository
promoted on that alone would reach Tier 1 with nothing to characterize, and the
generated tests would pass before and after because the code they cover never
ran either time. A test like that is worse than no test in a product that sells
trust, so Tier 1 requires an execution source or it does not happen.

Everything ambiguous — a timeout, an error, an exhausted budget, a refused
command — resolves **downward**. Being wrongly held at Tier 0 costs you a deeper
refactor. Being wrongly promoted would attach a verification claim to nothing.

Accuracy is measured against a second hand-labelled corpus (`tests/corpus/probe/`
— a healthy library, one needing API keys, one needing a GPU, one with no tests)
and held to **zero false Tier 1** by a regression test. `python
scripts/corpus_report.py` prints the confusion matrix.

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
— its test suite, its imports, its opted-in entry points — as your user, against
your real filesystem. There is no container and no VM. Containment is by command
allowlist only.

The allowlist constrains command *shapes*, not a list of program names: **your
repository never supplies a program.** Everything Polish runs is the sandbox
virtualenv's own interpreter in one of four forms —

```
python -m pip install --only-binary :all: <requirement>...
python -m pytest -q --tb=no -p no:cacheprovider
python -c "import <module>"
python -m <module>  |  <script>.py     # only if you asked for it, below
```

— never through a shell, never with your environment inherited (the child gets a
built `PATH` and a throwaway `HOME`, so the API keys in your shell do not reach
it), and never with options your `requirements.txt` chose. Every command a run
issued is printed in its output, so this is something you can check rather than
believe.

**What that does not cover, plainly: running your test suite is running your
code.** `conftest.py` executes at collection, and your own `addopts` reach pytest
without passing through the allowlist. So the honest claim is narrower than
"sandboxed" — it is that Polish never becomes a way for a repository to choose
what runs. Treat `polish run` as having exactly the privileges you do.

Entry points are never run unless you name one yourself:

```toml
[probe]
entrypoint = "yourpkg.demo"   # a module, or "scripts/demo.py" — never a shell string
```

The reasoning, and the two residual holes, are in
[ADR 0006](docs/adr/0006-a-command-allowlist-of-fixed-shapes.md).

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
│   └── probe/        #   the tier probe: four gates and the command allowlist
├── tests/            # suite, plus two hand-labelled corpora
├── scripts/          # corpus capture and reporting
└── docs/             # design docs and specs
```

## Development

```bash
uv venv && uv pip install -e . pytest ruff
.venv/bin/pytest
```
