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

Terms from `docs/SPEC.md` not yet exercised by code — *tier probe*, *Tier 0 /
Tier 1*, *characterization*, *snapshot*, *observer daemon*, *refactor engine*,
*verifier*, *reporter*, *change queue*, *repair attempt* — are defined there and
move here as they get built.
