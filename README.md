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
The run scopes itself to what changed since the last one plus its dependency
closure, probes which tier your repository has earned, then makes edits: **Tier
0** removes unused imports it can prove are safe to remove, one reviewable
commit each. On a **Tier 1** repository the run also characterizes your code
before touching it — flaky tests are found and quarantined, behaviour is traced
into oracles, and coverage is reported per symbol. The Tier 1 *edits* those
oracles exist to verify are not built yet.

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
commit    5053339
snapshot  .polish/snapshots/raw/2026-08-07 (6 files)
mirrored  2 uncommitted files
sandbox   .polish/runs/2026-08-07 (removed)
branch    polish/2026-08-07
scope     full sweep: 6 first-party files in scope
why       first run: no record of a previous run at .polish/last-run.json, so there is nothing to be incremental against
tier      1 (characterization)
reason    a test suite ran, and first-party modules import cleanly
gate 1    pass venv: nothing declared; the sandbox virtualenv is enough
gate 2    pass suite: 7 passing tests across 2 test files
gate 3    pass imports: 4 first-party modules import cleanly — which promotes nothing (SPEC §3.1)
gate 4    skip entrypoint: not configured; never run without being asked
ran       python -c import pytest
ran       python -m pip install --only-binary :all: pytest
ran       python -m pytest -q --tb=no -p no:cacheprovider
ran       python -c import app, app.loader, app.render, app.slug
source    the repository's own test suite (gate 2)
baseline  3x suite, 6 trusted, 1 quarantined
  ~ tests/test_flaky.py::test_cache_warms_up (failed / passed / passed)
oracles   5 kept, 1 discarded
  - test_polish_app_loader.py::test_stamped_0 (did not reproduce on unmodified code (failed in all 3 replays))
coverage  5 of 6 symbols at tier 1, 6 executed
  app/loader.py: 2 symbols, 2 executed, 1 tier 1
  app/render.py: 2 symbols, 2 executed, 2 tier 1
  app/slug.py: 2 symbols, 2 executed, 2 tier 1
ran       python -P -c import _polish_harness as _h; _h.suite()
ran       python -P -c import _polish_harness as _h; _h.suite()
ran       python -P -c import _polish_harness as _h; _h.suite()
ran       python -P -c import _polish_harness as _h; _h.trace()
ran       python -P -c import _polish_harness as _h; _h.oracles()
ran       python -P -c import _polish_harness as _h; _h.oracles()
ran       python -P -c import _polish_harness as _h; _h.oracles()
changes   3 committed, 0 dropped
  + refactor(imports): drop 1 unused import from app/loader.py
  + refactor(imports): drop 1 unused import from app/render.py
  + refactor(imports): drop 2 unused imports from app/slug.py
```

Reading the middle of that: one test in this repository was not reproducible, so
it is named and excluded. One recorded call — a function returning
`time.time()` — did not survive replay, so its oracle was thrown away and
`app/loader.py` is left with one of its two symbols uncharacterized. That is the
whole design in three lines: what could not be established is *reported* rather
than assumed.

A run copies your first-party source to a dated raw snapshot, checks a dated
branch out into its own git worktree with its own virtualenv, works there, and
leaves your working tree byte-identical — `git status` reads exactly as it did
before.

### Scope — one big pass, then only what moved

The first run sweeps the whole repository, once, while you are paying attention.
Every run after it looks at **the files that changed since the last run, plus
their dependency closure** — and says which files, and why:

```console
$ polish run
run       2026-08-07.3
...
scope     incremental: 5 first-party files in scope
why       1 changed since run 2026-08-07.2; 4 pulled in by the dependency closure
  ~ app/slug.py
  + app/__init__.py
  + app/cli.py
  + app/loader.py
  + tests/test_app.py
```

`~` changed · `?` unchanged, but the observer was not watching · `-` deleted
since the last run · `+` pulled in by the closure.

**The closure is the point.** Scoping to the changed files alone would make
every night after the first quietly less safe than the first: deleting a caller
today can strand a function three files away, and no diff of changed files will
ever surface that. So Polish walks the import graph out from what changed —
both to what those files import and to what imports them — over the union of
today's graph and the one the last run recorded. The edge that stranded the
function is, by definition, one today's graph no longer has.

Three things make a run sweep the whole repository instead, and all three widen
the scope rather than narrow it:

- there is no record of a previous run in `.polish/last-run.json`;
- the observer's store could not be believed, so the graph was cold-parsed —
  with no intra-day history, a file changed and changed back would read as
  untouched. **A repository with no observer daemon running sweeps every
  night**, which is the honest consequence of the daemon being the only thing
  that witnesses the day;
- you passed `--full`, which is how to ask for the first run's deep pass again.

The scope narrows what Polish *edits*. It never narrows what Polish *checks*:
Tier 0's type check always covers the whole first-party set, because it is the
only gate that can see a binding referenced from a file just outside the scope.
The tier probe likewise still sees the whole repository — and since its verdict
became per path, that is what keeps it honest rather than merely thorough: a
file the probe never looked at would inherit the ceiling it never earned. See
[ADR 0008](docs/adr/0008-the-incremental-scope-is-a-closure-over-two-runs-graphs.md).

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
3. **imports** — does each first-party module import? No → **that module** is
   Tier 0. **Yes → nothing.** An import smoke can take Tier 1 away; it can never
   grant it.
4. **entrypoint** — only what you named in `polish.toml`, never inferred. Yes →
   a Tier 1 candidate, and it is skipped entirely when gate 2 already passed.

Gates 1, 2 and 4 set a **ceiling** for the repository, because what they ask
about is repository-wide: a sandbox either builds or it does not, and there
either is something Polish can execute or there is not. Gate 3 then takes that
ceiling away **one module at a time**, so the verdict is a tier per path rather
than one tier for everything. An example app needing a dependency you never
declared is held at Tier 0 on its own, and does not drag your library down with
it — while a change spanning both resolves to the lower of the two.

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

### Characterization — what Polish believes, and what it refuses to

On a Tier 1 repository, before any edit, Polish works out what your code
actually does and which tests it is entitled to trust.

**Your suite is run three times.** Any test whose result is not identical every
time is **quarantined**: excluded from the night's verification, named in the
output, and unable to drop a change. During the run a failure is retried over
two more full suite runs before it is believed. A flaky suite is information you
want, which is why the names are printed rather than counted.

**Polish's own generated tests are held to a stricter rule.** Behaviour is
traced with `sys.settrace` while your suite runs, and each recorded call becomes
a test asserting it still returns what it was observed returning. Each one is
then replayed three times against unmodified code, and anything that varies is
**discarded — never emitted**.

**If you have no suite, the entry point you opted in to is traced instead.**
That is the same recorder and the same replay gate over the other execution
source the tier probe accepts, so a research or ML repository promoted by gate 4
alone gets real oracles rather than an empty night. It runs once, so there is
nothing to run three times: the run log reports `baseline none` — never "0
quarantined", which would read like a suite that was measured and found stable —
and the oracles' three replays are then the whole determinism gate. If your
repository has both, the suite is traced and your entry point is left alone.

The asymmetry is deliberate. A flaky test of yours *drops good work*. A flaky
oracle of Polish's *can pass a broken change*, which is a false green on the one
thing this tool sells. Polish owns its oracles, so it deletes them rather than
working around them. Nothing is pinned to rescue one — freezing a clock or
seeding an RNG would assert something about your code's behaviour that Polish
has not observed, and a wrong pin is worse than no oracle because it looks like
verification.

Coverage is reported **per symbol**, not per repository, and a symbol reaches
Tier 1 only if it has a surviving oracle. Code that ran but could not be
characterized falls back to Tier 0. An edit's tier is the lowest among the
symbols it touches.

The counts are yours to change:

```toml
[characterize]
baseline_runs = 3   # N — suite runs before any edit
retries       = 2   # M — extra whole-suite runs before a failure is believed
replays       = 3   # R — replays before a generated oracle is trusted
```

This costs real time: `N + 1 + R` suite runs before the first edit, with the
traced run several times slower than a plain one. `total_seconds` in the same
table bounds it, and a run that exhausts it keeps the baseline it already paid
for and reports the rest as undone.

Everything Polish writes lives in `.polish/`, which ignores itself via its own
`.gitignore`; your `.gitignore` is never edited.

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
— its test suite, its imports, its opted-in entry points — as your user, against
your real filesystem. There is no container and no VM. Containment is by command
allowlist only.

The allowlist constrains command *shapes*, not a list of program names: **your
repository never supplies a program.** Everything Polish runs is the sandbox
virtualenv's own interpreter in one of five forms —

```
python -m pip install --only-binary :all: <requirement>...
python -m pytest -q --tb=no -p no:cacheprovider
python -c "import <module>"
python -m <module>  |  <script>.py     # only if you asked for it, below
python -P -c "<one of Polish's own harness programs>"
```

The fifth is Polish's own characterization harness, and its program text is a
constant in Polish's source, compared for exact equality — not one character of
it comes from your repository. `-P` is what makes that true: without it, `python
-c` puts your repository's own directory first on `sys.path`, and a file of the
right name sitting there would supply the program instead. That holds even when
the harness runs *your* entry point: the target is a parameter it reads from a
file Polish wrote, checked by the same rule that guards it on the command line,
so the command itself stays identical on every repository Polish is pointed at.

Nothing here runs through a shell, never with your environment inherited (the child gets a
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

Naming one has two consequences, and the second is the reason to bother: it is
what a repository with no test suite is promoted to Tier 1 *by*, and it is what
Polish then traces to build that repository's oracles. It is run at most twice —
once by the probe, once under the recorder — and not at all if your suite
already passed.

The reasoning, and the residual holes, are in
[ADR 0006](docs/adr/0006-a-command-allowlist-of-fixed-shapes.md) and
[ADR 0007](docs/adr/0007-a-polish-authored-harness-inside-the-allowlist.md).

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
│   ├── characterize/ #   Tier 1's oracles, the flake gate and per-symbol coverage
│   ├── envelope/     #   snapshot, sandbox and branch: the safety envelope
│   ├── firstparty/   #   first-party source detection
│   ├── observer/     #   the daytime daemon: code graph and change history
│   ├── probe/        #   the tier probe: four gates and the command allowlist
│   ├── scope/        #   what a run looks at: the changed files and their closure
│   └── tier0/        #   deterministic codemods, proven rather than tested
├── tests/            # suite, plus two hand-labelled corpora
├── scripts/          # corpus capture and reporting
└── docs/             # design docs and specs
```

## Development

```bash
uv venv && uv pip install -e . pytest ruff
.venv/bin/pytest
```
