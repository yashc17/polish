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
Grants exemption from ignore rules, not from markers.

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
repository, and nothing else. Ignored by its own `.gitignore`, because Polish
never edits the user's.

**Mirrored work** — the uncommitted changes replayed from the working tree into
the sandbox, which is otherwise checked out from `HEAD`. Without it a run would
refactor the last commit rather than the day that just happened.

**Tier probe** — the component that decides whether Polish can *execute* a
repository, and therefore which engine and which claim it has earned. Run by
`polish.probe.probe()` inside the sandbox, never against the working tree. Say
*probe* for the act and *verdict* for what it produces.

**Tier 0 / Tier 1** — what the verdict is. Tier 0 verifies by AST-equivalence
plus a type check and claims "provably behaviour-preserving". Tier 1 verifies
against characterization tests from real execution and claims "verified against
recorded behaviour" (SPEC §3.1). Per **repository**, not per file, for v1 —
granularity is open question #4 and needs coverage data from #13. Never say "the
repo is Tier 1" about anything the probe has not promoted.

**Gate** — one hard filter in the probe, evaluated in order: *venv*, *suite*,
*imports*, *entrypoint*. Hard, not scored: a gate cannot contribute partial
credit, so no repository can average its way into Tier 1. Say *gate*, not
"check" or "heuristic" — what it does is refuse.

**Execution source** — a suite (gate 2) or an opted-in entry point (gate 4).
**Tier 1 requires one.** Gate 3 is *demote-only*: a clean import smoke takes
nothing away and grants nothing, because importing a module traces definitions
and never enters a function body. Promoting on it would produce the vacuous test
SPEC §3.1 calls *actively corrosive*. This is the single most load-bearing rule
in the probe.

**Downward resolution** — the rule that ambiguity, timeout, error, exhausted
budget and refused command all resolve to Tier 0. Nothing in the probe resolves
upward on anything but a repository actually executing.

**False Tier 1** — a repository promoted that should not have been. The error
that must never happen: it attaches a verification claim to execution that never
occurred. **False Tier 0** is a lost night and is acceptable. Same asymmetry as
precision over recall in detection, applied one layer up.

**Command allowlist** — the four fixed command shapes the probe may execute,
always the sandbox interpreter, never a program the repository supplied. Its
claim is narrower than "sandboxed": Polish never becomes a way for a repository
to choose what runs. See
[ADR 0006](docs/adr/0006-a-command-allowlist-of-fixed-shapes.md) and SPEC §3.3.

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
repaired by re-parsing, because the tree is the source of truth.

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
existence is why a stale graph is never a correctness problem, only a cost.

**Store** — what the observer keeps inside the workspace: `graph.db` (SQLite),
`meta.json` and `daemon.pid`. Say *store* for the observer's state and
*workspace* for `.polish/` as a whole, which also holds snapshots and sandboxes.

Terms from `docs/SPEC.md` not yet exercised by code — *characterization*,
*refactor engine*, *verifier*, *reporter*, *change queue*, *repair attempt* —
are defined there and move here as they get built.
