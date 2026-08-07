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

**Store** — `.polish/`, a SQLite database plus a `meta.json` and a pidfile. It
carries its own `.gitignore` containing `*`, so it hides from git without Polish
ever editing the user's own.

Terms from `docs/SPEC.md` not yet exercised by code — *tier probe*, *Tier 0 /
Tier 1*, *characterization*, *snapshot*, *refactor engine*, *verifier*,
*reporter*, *change queue*, *repair attempt* — are defined there and move here as
they get built.
