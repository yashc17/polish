# ADR 0009 — One module index, shared by the tier probe and the code graph

**Status:** Accepted · 2026-08-07
**Context:** [#30](https://github.com/yashc17/polish/issues/30) · SPEC §3.6,
§3.7 · [ADR 0008](0008-the-incremental-scope-is-a-closure-over-two-runs-graphs.md)

## Context

Two components had to answer "what is this file importable as", and they
answered it differently.

The tier probe walked outwards from a file while `__init__.py` kept appearing —
the interpreter's own rule — and so addressed `src/requests/__init__.py` as
`requests`, from the root `src`. The code graph derived the name from the path
and called the same file `src.requests`.

On a `src/` layout the second answer is not a name any absolute import writes.
Dogfooding ADR 0008's closure against `psf/requests` produced a graph with **no
edge at all** between the 18 files under `tests/` and the 19 under `src/`: two
disconnected components where the repository has one. `import requests`
resolved to nothing, silently, and every assertion about the graph still passed.

Nothing here produces an unproven edit — Tier 0's type check covers the whole
first-party set precisely so a hole in the graph cannot narrow a proof. What it
produces is **a narrower scope than the repository deserves**, which is ADR
0008's claim ("an incremental run is not quietly less safe than a sweep") being
less true than the run log implies.

## Decision

**Naming is one implementation, `polish.modules`, and both consumers use it.**

`index(root, files)` answers with two views of the same table, because the two
consumers want different things from it:

- `by_name` — **every** name a file is importable by, to the path it names.
  What the code graph resolves an import statement against.
- `addressed` — **one** module per file, the *address*: the single name the
  interpreter reaches it by on its own terms. What anything that has to execute
  an import uses.

**A file has more than one legitimate name, and all of them are indexed.** With
the repository root on `sys.path`, `src/requests/__init__.py` is importable as
`src.requests` as well, because PEP 420 makes `src/` an implicit namespace
package. Both spellings occur in real repositories, so indexing one and dropping
the other would have traded the old blind spot for a new one.

**Roots come from three places, in precedence order:** the package chain above
the file (the interpreter's rule), the roots packaging metadata declares, and
the repository root. The metadata layer earns its place on the case the walk
cannot see: a namespace package leaves no `__init__.py` to walk over, so
`where = ["src"]` is the only evidence that `src/mypkg/core.py` answers to
`mypkg.core`. It is read through `polish.firstparty.declared_roots()` — the
layer that already reads packaging metadata — rather than by a second parser.

A declared entry is either a directory *holding* packages (`package-dir`,
`packages.find.where`) or a package directory itself (poetry, hatch, flit).
**The tree decides which**, by whether the directory carries an `__init__.py`,
rather than the table the entry was written in.

**The probe takes addresses only, never a second spelling.** Gate 3 is
demote-only: importing one file twice under two names can take a tier away and
can never grant one.

**Name collisions resolve deterministically and lose nothing.**
`scripts/build.py` and `tools/build.py` both claim `build` from their own
directory. Addresses are claimed before alternate spellings, ties break on
sorted path order, and the file that loses a name keeps every other name it has.

## Alternatives rejected

**Fix `observer._graph.module_index` in place.** Smallest diff, and it leaves
two implementations of one rule that already drifted once — with no test that
can fail when they drift again. #30 asks for the shared-helper decision to be
made on purpose rather than in passing, which is this ADR.

**Have the observer import `polish.probe`.** Wrong direction and wrong weight:
the probe installs packages and executes test suites, and the daemon that runs
all day in the user's tree should not be able to reach any of that. The shared
piece is a static naming table, so it lives below both.

**Put naming inside `polish.firstparty`.** Tempting — it already has the file
set and the declared roots. Rejected because first-partyness is a question about
*authorship* (CONTEXT.md is explicit) and naming is a question about the import
system; a package that answered both would be answering "is this file mine" and
"what does `import x` mean" under one name. Only the metadata accessor moved.

**One name per file — the address, and nothing else.** Half the entries and a
simpler table. It re-breaks the repositories the old rule got right: a file run
from the repository root legitimately writes `src.mypkg`, and dropping that
spelling would strand exactly the kind of script the old index handled.

**Let declared metadata outrank the tree for the address too.** It would help a
namespace package whose modules import each other absolutely — addressed from
its own directory, `from mypkg import util` fails and gate 3 demotes a healthy
repository. Rejected here because the tree is ground truth and the metadata is a
claim, and because changing what gate 3 imports is a probe change wearing a
naming change's clothes. The name is indexed either way, so only the probe's
choice of address is at stake; if that demotion is ever observed, it gets its
own issue and its own evidence.

## Consequences

- **The observer's output changes for existing users.** A `src/`-layout
  repository gains import edges it never had, so the first incremental run after
  this lands scopes wider than the one before it. That is the hole closing, and
  ADR 0008's "over-scoping is the safe error" is the rule it lands under.
- **More names than any one interpreter would accept at once.** `mypkg` and
  `src.mypkg` cannot both be live on a single `sys.path`, and the index holds
  both. For a graph that is right: the question is what an import statement in
  this repository could mean, not what one particular invocation resolved. The
  cost is a possible spurious edge, which widens a scope; the alternative was a
  missing edge, which narrows one.
- **`polish.firstparty` has a second public function.** `declared_roots()` is
  now part of its interface, so the "scan() is the whole interface" claim in its
  docstring is retired rather than quietly false.
- **`polish.tier0._modules` is a third copy of the addressing rule, and it
  stays for now.** It agrees with this one — same walk, same result — but it
  answers a different question on top of naming: resolving relative imports and
  mapping a path back to its own name, in service of the proof that decides
  which imports Tier 0 may remove. Folding it in changes what "provably
  behaviour-preserving" is computed from, which deserves its own change, its own
  evidence, and not a passing mention in an issue about the code graph.
