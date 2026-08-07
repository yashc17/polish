# ADR 0008 — The incremental scope is a two-way closure over two runs' import graphs

**Status:** Accepted · 2026-08-07
**Context:** [#20](https://github.com/yashc17/polish/issues/20) · SPEC §1.1,
§3.6, §3.7

## Context

SPEC §3.6 settles the shape of an incremental run in one line — "files changed
since the last run, **plus their dependency closure**" — and justifies it with
one sentence:

> The closure is where the code graph earns its keep: deleting a caller today
> can strand a function three files away, and `git diff` will never surface
> that.

Building it turned up three things the sentence does not say, each of which
decides whether the scope is safe or merely small.

**Which direction is "dependency"?** The observer shipped
`Observation.dependents()` — everything that transitively *imports* a file —
and its docstring claimed to be this closure. It is not, or not all of it. Read
§3.6's own example carefully: the caller is what was edited, and the stranded
function is in a file the edited file *imports*. That is the forward direction.
The reverse direction is a different, equally real case: change a shared utility
and its callers are what break.

**Which graph?** The stranding case is caused by an edge being **deleted**. If
`a.py` stopped importing `b.py` today, today's graph has no `a → b` edge, so no
walk over it reaches the function that edge orphaned. The only graph that still
contains the edge is yesterday's.

**What does "the graph is unavailable" mean,** given that `observer.read()`
never fails — it cold-parses whatever it cannot believe and always returns a
correct graph of the current tree?

## Decision

**The scope is the union of two single-direction walks from the same seeds,
over the union of the previous run's import edges and today's.**

    scope = closure(seeds) ∩ first-party set
    closure(S) = S ∪ transitive-dependencies(S) ∪ transitive-dependents(S)

The seeds are three things, all of which mean "this may have moved since the
last run":

- files whose bytes differ from the previous run's record, and files that are
  new;
- files unchanged on disk whose **history has a hole in it** — ADR 0003's rule
  one layer up: absent history is not evidence of quiet, so a file the daemon
  was not watching cannot be assumed to have been still;
- paths the previous run recorded that the tree no longer has. A deleted file
  cannot be refactored, but its former imports are exactly what it stranded.

**The two walks never alternate.** A walk that may go forward along an edge and
then back up another is the undirected connected component, and in any Python
package whose `__init__.py` imports its submodules that component is the entire
package — every incremental run would be a full sweep, correctly and uselessly.
Going out from the seeds once in each direction keeps the answer proportional
to what changed.

**Yesterday's tree is recorded in `.polish/last-run.json`** — a digest and the
import edges per file, written at the *end* of a run. The end, because the
record is a claim that the tree was looked at, and a run that died halfway
through has not looked at it. Every way of failing to read it — absent,
truncated, hand-edited, written by a future version — resolves to a full sweep.

**A cold-parsed graph forces a full sweep.** The graph would be fine to walk;
the *history* is what is missing, and without it "the digest matches yesterday"
cannot be read as "nothing happened here". A file written and reverted during
the day leaves no trace on disk, and SPEC §3.7 keeps the daemon for precisely
that signal. So the criterion "when the graph is stale or unavailable, fall
back to a full sweep" is implemented as: no store the run can believe, no
incremental run.

**The scope narrows what Polish edits. It never narrows what Polish checks.**
Tier 0's type check is the only gate that can see a binding referenced from
another file, so its surface is what "provably behaviour-preserving" means.
`tier0.refactor()` therefore takes the file set to verify against separately
from the file set to edit, and the envelope always passes the whole first-party
set as the former.

## Alternatives rejected

**Reverse only** — `Observation.dependents()`, already built and cheaper.
Rejected because §3.6's own motivating example runs the other way, and issue
#20's acceptance criteria name it directly: "a function stranded three files
away from the edit is brought into scope".

**Forward only.** Covers the motivating case and misses the ordinary one: a
change to a widely-imported utility would ship without anything that imports it
ever being looked at.

**The undirected connected component**, i.e. one walk that ignores edge
direction. It is the natural-sounding reading of "both directions" and it is
degenerate — see above. Worth naming here because the code that produces it is
*shorter* than the code that does not, so it is what a well-meaning
simplification would arrive at.

**Today's graph only**, dropping the edges from the previous run's record. Half
the size on disk, and it fails the one case §3.6 uses to justify the feature.

**`git diff` against the previous run's `HEAD`** instead of a digest record.
Familiar, needs no new state, and blind to exactly what Polish exists to work
on: the day's uncommitted work. It would also be confused by any rebase or
amend.

**Making the tier probe see the scope too.** Cheaper in principle. Rejected
because the probe's verdict is per-repository (#28 will make it per-symbol), so
one computed from a subset is a narrower claim wearing the same name — and the
saving is not where it sounds: gates 1 and 2 take no file set at all, so
scoping would shrink only gate 3, which is *demote-only*. The single effect
would be to make demotion less likely, which is the false Tier 1 the probe
exists to prevent.

## Consequences

- **Over-scoping is the safe error, and it is the one this design makes.** A
  scope that is too wide costs a longer night and degrades into the full sweep
  Polish shipped with. A scope that is too narrow ships an edit nobody looked
  at the consequences of, which SPEC §1.1 does not allow. Every uncertainty
  here widens.
- **A repository with no observer daemon never gets an incremental run.** Every
  night cold-parses, and every night sweeps. That is the honest consequence of
  §3.7's justification: the daemon's value *is* the history, and a run without
  it has no basis for believing an unchanged file was quiet.
- **On a package whose `__init__.py` re-exports its submodules, a change to any
  one of them reaches most of the package.** Dogfooding against `psf/requests`
  scoped a one-file edit to 19 of 37 files — the whole `requests` package, and
  nothing under `tests/` or `docs/`. That is correct rather than disappointing,
  but it sets expectations: the saving on this shape is real and modest.
- **The closure is only as complete as the import graph.**
  `observer._graph.module_index` addresses modules by path, so on a `src/`
  layout an absolute `import mypkg` does not resolve to `src/mypkg/__init__.py`
  and the edge is missing. The consequence is a narrower scope, not an unproven
  edit — the type check still covers the whole repository — but it is a real
  hole, found by dogfooding this change against `psf/requests`, and it is
  tracked in [#30](https://github.com/yashc17/polish/issues/30).
- The watermark is per-workspace, so two checkouts of the same repository scope
  independently. That is the desired behaviour and it falls out of `.polish/`
  living beside the tree it describes.
