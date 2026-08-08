# Polish — domain context

The vocabulary Polish uses about itself. Add a term when it earns its keep, not
before. Design reasoning lives in `docs/SPEC.md`; decisions that changed a
design live in `docs/adr/`.

## Glossary

**First-party file set** — the files Polish is willing to refactor: the user's
own Python source, as opposed to virtualenv, vendored, generated or build code
that merely lives in the same tree. Resolved by `polish.firstparty.scan()` and
printed by `polish scan`. Say *first-party*, not "project files", "user code" or
"source files" — the distinction being drawn is **authorship**, not location.

**Layer** — one source of evidence about first-partyness: packaging metadata,
the gitignore-aware walk, environment markers, the `polish.toml` override. They
compose by successive refinement, in the precedence set by
[ADR 0001](docs/adr/0001-marker-evidence-outranks-packaging-metadata.md).

**Marker** — a file or directory name that is decisive evidence code was not
written by the user: `pyvenv.cfg`, `conda-meta/`, `site-packages`,
`node_modules`, `_vendor`. The bar for a marker is that no reasonable person
would name their own package that.

**Declared root** — a directory packaging metadata names as holding the repo's
code (`package-dir`, `packages.find.where`, poetry `packages`, flit `module`).
Grants exemption from ignore rules, not from markers. Read by
`polish.firstparty.declared_roots()`, which is also where *import root* below
gets the evidence a walk cannot find.

**Import root** — a directory a module name is relative to: what would be on
`sys.path` for that name to work. Found by walking outwards from a file while
`__init__.py` keeps appearing, plus what packaging metadata declares, plus the
repo root. Say *import root* for the directory and *module name* for the name it
makes; `polish.modules` is the one place either is decided
([ADR 0009](docs/adr/0009-one-module-index-shared-by-the-probe-and-the-graph.md)).

**Address** — the one name the interpreter reaches a file by on its own terms,
as opposed to the other names it is legitimately importable by. A file under
`src/` is addressed `mypkg` and *also* indexed as `src.mypkg`, because PEP 420
makes both real. Anything that **executes** an import takes the address only —
gate 3 is demote-only, so a second spelling of a module already imported could
only ever cost a tier. Anything that **resolves** an import takes them all: an
edge the graph does not have is a file the closure never reaches.

**Safety envelope** — the apparatus around an edit: snapshot, sandbox, branch,
and the refusals that guard them. Built by `polish.envelope.run()` and driven by
`polish run`. The envelope is the product, not the packaging — a change Polish
cannot verify is a change Polish does not make.

**Run** — one pass of the envelope, identified by a **run id**: today's date,
suffixed `.2`, `.3` if the day already has runs. The id names the run's
snapshot, its sandbox directory and its branch, so all three line up.

**Snapshot** — the unedited copy of the first-party file set taken before any
work begins, at `.polish/snapshots/raw/<run id>`. Say *raw snapshot* when
distinguishing it from anything Polish produces later. It byte-matches the tree
it was taken from, and is never overwritten.

**Sandbox** — a git worktree checked out on the run's branch, plus its own
virtualenv beside it. Where all work happens. The working tree is isolated from
the sandbox absolutely; the machine is not isolated from it at all
([ADR 0002](docs/adr/0002-refuse-non-git-repositories.md), SPEC §3.3).

**Workspace** — `.polish/` at the repo root: everything Polish writes into a
repository, and nothing else — the observer's store, the raw snapshots, the
sandboxes, and the watermark the next run scopes from. Ignored by its own
`.gitignore`, because Polish never edits the user's.

**Mirrored work** — the uncommitted changes replayed from the working tree into
the sandbox, which is otherwise checked out from `HEAD`. Without it a run would
refactor the last commit rather than the day that just happened.

**Tier probe** — the component that decides whether Polish can *execute* a
repository, and therefore which engine and which claim it has earned. Run by
`polish.probe.probe()` inside the sandbox, never against the working tree. Say
*probe* for the act and *verdict* for what it produces.

**Tier 1** — the tier for code Polish *can* execute. Verification is
characterization tests from real execution traces and the claim is **verified
against recorded behaviour** (SPEC §3.1); *Tier 0* below is its counterpart.
Granularity is **per symbol / per path**: an edit's tier is the *lowest* tier
among the code it touches ([question
#4](https://github.com/yashc17/polish/issues/14)) — asked of the probe through
`TierVerdict.tier_of` and of the trace through `Coverage.tier_for`, both
binding. Say *this code is Tier 1*, never *this repo is Tier 1*: since
[ADR 0010](docs/adr/0010-a-tier-ceiling-that-gate-three-demotes-per-module.md) a
repository does not have one tier to be.

**Ceiling** — the best tier any code in a repository could reach, set by gates
1, 2 and 4 and carried as `TierVerdict.ceiling`. Repository-wide because what it
answers is: a sandbox either builds or it does not, and an *execution source*
either exists or it does not. A **maximum, not a verdict about any file** —
`reaches_tier_one` asks whether anything here is eligible at all, which is what
decides whether characterization runs.

**Demotion** — one module gate 3 held below the ceiling, carrying its dotted
name, its path and its own import error. Say *demoted*, not "failed": the
verdict is about what may be claimed, not about who is at fault, and a module
Polish cannot import is unverifiable rather than broken. A path the probe never
smoked is **not** demoted — it takes the ceiling, because gate 3 is demote-only
and absence of evidence cannot demote either.

**Gate** — one hard filter in the probe, evaluated in order: *venv*, *suite*,
*imports*, *entrypoint*. Hard, not scored: a gate cannot contribute partial
credit, so no repository can average its way into Tier 1. Say *gate*, not
"check" or "heuristic" — what it does is refuse.

**Execution source** — a suite (gate 2) or an opted-in entry point (gate 4).
**Tier 1 requires one.** Gate 3 is *demote-only*: a clean import smoke takes
nothing away and grants nothing, because importing a module traces definitions
and never enters a function body. Promoting on it would produce the vacuous test
SPEC §3.1 calls *actively corrosive*. This is the single most load-bearing rule
in the probe, and it is what decides the shape of a *demotion*: gate 3 subtracts
from the *ceiling*, never adds to it.

Characterization traces whichever source the repository has, suite first
([#32](https://github.com/yashc17/polish/issues/32)) — the same order and the
same rule as the probe, where gate 4 is not run once gate 2 has supplied one.
Which source it was is carried on the `Characterization`, because it decides
what verification is able to check. Say *execution source* for the concept and
name the gate when it matters; never say "the suite" about an entry point.

**Downward resolution** — the rule that ambiguity, timeout, error, exhausted
budget and refused command all resolve to Tier 0. Nothing in the probe resolves
upward on anything but a repository actually executing.

**False Tier 1** — a repository promoted that should not have been. The error
that must never happen: it attaches a verification claim to execution that never
occurred. **False Tier 0** is a lost night and is acceptable. Same asymmetry as
precision over recall in detection, applied one layer up.

**Command allowlist** — the five fixed command shapes Polish may execute, always
the sandbox interpreter, never a program the repository supplied. Its claim is
narrower than "sandboxed": Polish never becomes a way for a repository to choose
what runs. See [ADR 0006](docs/adr/0006-a-command-allowlist-of-fixed-shapes.md)
and SPEC §3.3.

**Harness** — the fifth shape, and Polish's own program running inside the
sandbox: `python -P -c "<a constant in polish's source>"`, doing one of four
tasks (*suite*, *trace*, *oracles*, *entrypoint*). Its parameters travel in the
environment because the command must stay a constant, and `-P` is what stops the
repository supplying the module. The entry point's *target* is one of those
parameters, for exactly that reason. See
[ADR 0007](docs/adr/0007-a-polish-authored-harness-inside-the-allowlist.md). Say
*harness* for Polish's program and *suite* for the repository's tests.

**Corpus** — the hand-labelled real repositories the product claims are measured
against. Two of them now: `tests/corpus/` for detection precision and recall,
and `tests/corpus/probe/` for tier accuracy. See `tests/corpus/README.md`.

**Precision / recall floor** — precision 1.00, recall ≥ 0.95, enforced by
`tests/test_corpus.py`. The asymmetry is deliberate: including a file that isn't
the user's is far worse than missing one that is. The probe's mirror of it is
**zero false Tier 1**, enforced by `tests/test_probe_corpus.py`.

**Recording** — what a probe manifest holds instead of a live repository: what
each command really did on a real machine, once, captured by
`scripts/capture_probe_corpus.py`. A replay exercises the gate logic offline; it
cannot prove the probe executes anything, which is why `tests/test_probe.py`
builds repositories that really run.

**Observer** — the daemon that watches the working tree through the day, keeping
the code graph and the change history current. Run by `polish observe`,
implemented in `polish.observer`. Say *observer*, not "watcher" or "indexer" —
what it does is witness, and the value is in having been present.

**Code graph** — the first-party files, the symbols they define, and the import
edges between them. Verifiable: every file carries a fingerprint, and drift is
repaired by re-parsing, because the tree is the source of truth. Edges are
resolved through `polish.modules`, the same naming the tier probe imports by, so
the graph and the probe cannot disagree about what `import x` means.

**Change history** — what the observer saw happen during the day, which
`git diff` erases: code written at 2pm and deleted at 5pm, and how often a symbol
was rewritten. *Not* verifiable — nothing can reconstruct it after the fact.
Distinguishing this from the code graph is the whole of
[ADR 0003](docs/adr/0003-observer-state-in-a-self-ignoring-sqlite-store.md).

**Uptime window** — a stretch the observer was actually watching. Recorded, so
that the stretches it was not are visible rather than invisible.

**Gap** — a stretch of the day inside no uptime window. Not an error; an absence
of evidence, and it must read as one.

**Churn** — the revisions the observer watched a file or symbol go through. A
*lower bound*, and **UNKNOWN rather than zero** for anything whose history falls
in a gap. Never say "no churn" about a gap: SPEC §3.7 gates dedup on churn, so
absent data read as quiet produces an unsafe edit.

**Cold parse** — rebuilding the graph from the tree, ignoring the store. The
fallback whenever the store is missing, version-skewed, unreadable or stale. Its
existence is why a stale graph is never a correctness problem, only a cost. The
cost is paid in scope: a cold-parsed run sweeps, because the graph it rebuilt is
correct and the history it could not rebuild is what says a file was quiet.

**Scope** — the first-party files one run will refactor, and why each of them is
there. Decided by `polish.scope.select()` before the tier probe, and printed by
`polish run` as `scope` / `why` plus one line per file. Say *scope* for the set,
and *sweep* or *incremental* for which kind it is.

**Full sweep** — a scope that is the whole first-party set: what the first run
does (SPEC §3.6), what `--full` asks for, and what every widening fallback
resolves to. *Incremental* is its counterpart — the changed files and their
closure. The scope narrows what Polish **edits** and never what it **checks**:
Tier 0's type check and the tier probe both still see the whole repository.

**Dependency closure** — the changed files, everything they transitively import,
and everything that transitively imports them. Two separate walks from the same
seeds, unioned — never one walk that changes direction, which is the undirected
connected component and, in any package with an `__init__.py`, the whole package.
Walked over the union of the previous run's import edges and today's, because the
edge that stranded a function is by definition one today's graph no longer has.
See [ADR 0008](docs/adr/0008-the-incremental-scope-is-a-closure-over-two-runs-graphs.md).

**Watermark** — the record of the tree as the previous run found it, at
`.polish/last-run.json`: a digest and the import edges per file. Written at the
*end* of a run, because it is a claim that the tree was looked at. Every way of
failing to read one resolves to a full sweep. Say *watermark*, not "cache" —
what it holds is a claim, not a saving.

**Unwitnessed** — a file byte-identical to the watermark whose history has a hole
in it. Scoped in anyway. This is **churn**'s rule one layer up: reading "the
bytes match" as "nothing happened here" is the same mistake as reading a gap as
quiet.

**Store** — what the observer keeps inside the workspace: `graph.db` (SQLite),
`meta.json` and `daemon.pid`. Say *store* for the observer's state and
*workspace* for `.polish/` as a whole, which also holds snapshots and sandboxes.

**Tier 0** — the tier for a repository Polish cannot execute. Verification is
AST-equivalence plus a type check, the edits are deterministic codemods, and the
claim is **provably behaviour-preserving**. Implemented in `polish.tier0`. Say
*Tier 0*, not "the safe tier" — Tier 1 is not less safe, it verifies differently.

**Change** — one file's worth of refactoring, decided as a unit: applied,
verified, then either committed on its own or reverted. Never spans two files,
which is what makes dropping any single commit safe. A dropped change is still a
change, and still appears in the run log.

**Provably inert** — a module that can be imported without running anything: the
standard library minus a denylist, or a first-party module whose top level is
only definitions and literals. The bar an unused import must clear before Tier 0
will remove it, because importing a module runs it. See
[ADR 0005](docs/adr/0005-tier-0-removes-only-provably-inert-imports.md).

**Left alone** — an unused import Tier 0 declined to remove, carried into the
commit message with its reason. Distinct from "not found": the reason is the
product, in the same way the precision floor is.

**AST-equivalence** — the proof that an edit removed exactly the bindings it
claimed and changed nothing else. `polish.tier0.prove()`, checkable on its own.
Comments and layout may move; import order may not, because import order is
behaviour.

**Baseline** *(Tier 0)* — the diagnostics the type checker reported *before* any
edit. The gate is that a change introduces none that were not already there —
Tier 0's target repository does not type check clean, so an absolute bar would
never pass. See [ADR 0004](docs/adr/0004-mypy-as-the-tier-0-type-check.md).
Tier 1 has a baseline of its own, below; say which when both are in scope.

**Unverifiable** — the state where verification could not run at all. Not a
verdict, and never read as one: the run ships nothing and says so.

**Characterization** — what Tier 1 knows about a repository before any edit: the
suite's flake baseline, the generated oracles that survived replay, and the
per-symbol coverage. Produced by `polish.characterize.characterize()` inside the
sandbox, from whichever **execution source** the repository has. Say
*characterization* for the whole record and *oracle* for one test in it.

**Oracle** — one generated test asserting that a recorded call still returns
what it was observed returning. Written as a `repr` comparison, not `==`, so
that dict and set ordering is a determinism signal rather than something a
tolerant equality forgives. Polish owns its oracles — which is why they are
*discarded*, never quarantined.

**Recordable** — a call whose arguments and return value can be written back out
as source that reconstructs them. The Tier 1 counterpart of *provably inert*: an
oracle that cannot rebuild its own inputs is not an oracle. Methods are excluded
by it for free, because `self` is not a literal.

**Baseline** *(characterization)* — the repository's own suite, run **N=3** times
before any edit exists to bias it. Authoritative because it is the only thing
that can tell a pre-existing flake from one Polish caused. Distinct from Tier 0's
type-check baseline; say which when both are in scope.

A repository whose execution source is an entry point has **none**, because an
entry point runs once and there is nothing to compare. The run log says `none`
and never "0 quarantined" — that would read exactly like a suite measured and
found stable, which is **churn**'s UNKNOWN-not-zero rule one component along
([#32](https://github.com/yashc17/polish/issues/32)).

**Quarantined** — a test whose result was not identical across the baseline's N
runs. Excluded from the night's verification, **named in the run log**, and
never able to drop a change. Applied to the *user's* tests, because you cannot
delete someone else's test. An entry-point repository has no quarantine step at
all, rather than an empty one.

**Discarded** — a generated oracle that did not pass identically across all
**R=3** replays against unmodified code. Deleted, not quarantined. The asymmetry
with *quarantined* is the point: a flaky user test drops good work, a flaky
oracle can pass a broken change.

**Replay** — running a generated oracle against unmodified code to find out
whether it may be believed. *Replay before trust*: nothing is emitted that has
not survived R of them.

**Retry** — the M=2 backstop during the run: a regression is re-checked over M
more **whole-suite** runs before it is believed. Whole-suite because a test id is
repository-authored text, and the allowlist keeps such text out of every command
line.

**Regression** — a trusted test that got worse than the baseline recorded, or
stopped being collected at all. Not "a test that failed": gate 2 promotes
repositories whose suites already have failures, so holding every test to green
would blame Polish for a repository that was already red.

**Coverage tier** — a symbol's tier: **Tier 1 only with a surviving oracle**.
Executed is not enough — code with no oracle falls back to Tier 0, or SPEC §3.9's
commit message would claim "verified against recorded behaviour" for a change
whose oracle was thrown away. An edit's tier is the lowest among the symbols it
touches ([question #4](https://github.com/yashc17/polish/issues/14)), looked up
through `Coverage.tier_for`. The probe's *ceiling* is the other half of the same
rule and binds independently: the probe says how high a path may reach, the
coverage tier says what evidence there is, and an edit gets the lower of the two.

Terms from `docs/SPEC.md` not yet exercised by code — *Agent SDK edits*,
*verifier*, *reporter*, *change queue*, *repair attempt* — are defined there and
move here as they get built.
