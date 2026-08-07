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

**Corpus** — the hand-labelled real repositories in `tests/corpus/` that
precision and recall are measured against. See `tests/corpus/README.md`.

**Precision / recall floor** — precision 1.00, recall ≥ 0.95, enforced by
`tests/test_corpus.py`. The asymmetry is deliberate: including a file that isn't
the user's is far worse than missing one that is.

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

Terms from `docs/SPEC.md` not yet exercised by code — *tier probe*, *Tier 0 /
Tier 1*, *characterization*, *refactor engine*, *verifier*,
*reporter*, *change queue*, *repair attempt* — are defined there and move here as
they get built.
