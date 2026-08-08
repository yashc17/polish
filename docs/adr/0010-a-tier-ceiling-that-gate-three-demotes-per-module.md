# ADR 0010 — The tier verdict is a repository ceiling that gate 3 demotes per module

**Status:** Accepted · 2026-08-07
**Context:** [#28](https://github.com/yashc17/polish/issues/28) ·
[question #4](https://github.com/yashc17/polish/issues/14) · SPEC §3.1, §3.9 ·
[ADR 0006](0006-a-command-allowlist-of-fixed-shapes.md)

## Context

`polish.probe.probe()` returned **one tier for the whole repository**. That was
deliberate: [#3's decision](https://github.com/yashc17/polish/issues/3#issuecomment-5212772529)
says "per-repo for v1. Granularity under partial coverage is #4's decision and
needs coverage distributions from #13; pre-empting it here would decide it on
intuition."

Question #4 has since been answered — **per-symbol / per-path tiering, an edit's
tier being the lowest tier among the symbols it touches** — so that reason has
expired, and the per-repository verdict is the thing that lags.

The cost of leaving it is committed to the corpus. `tests/corpus/probe/flask.json`
is a real repository where 485 tests pass and every `flask.*` module imports,
but eight modules under `examples/` and `docs/` want celery, which flask does
not declare. Gate 3 demoted the whole repository for those eight. The manifest
is hand-labelled Tier 1 against the code, so the row scored as a **false Tier 0**
— correctly, and visibly, for exactly as long as this ticket was open.

Two things made this more than a renaming exercise.

**Characterization cannot be the probe's input.** Question #4's rule is stated
over *symbols*, and a symbol's tier depends on whether an oracle survived
replay. But `characterize()` only runs on a repository the probe has already
promoted, so at the moment the probe answers, nothing has been traced, nothing
has been replayed, and there is no coverage table to consult. Making the probe
per-symbol in the literal sense would mean inverting that order.

**Gate 3 is demote-only, and per-path granularity has a way of quietly
reversing that.** The gate's whole point (SPEC §3.1) is that a clean import
grants nothing. The obvious implementation of "a tier per path" — a table with a
row per module gate 3 imported — makes the *absence* of a row mean Tier 0, which
turns the gate into a promotion gate: *only what I saw may be Tier 1*. That is
the vacuous-test bug the design is shaped against, arriving by the back door.

## Decision

**The verdict is a `ceiling` plus the modules gate 3 took it away from, and
`tier_of(path)` is the question callers ask.**

    ceiling            gates 1, 2, 4 — repository-wide by nature
    demoted            gate 3 — one row per module that did not import
    tier_of(path)      ZERO if the path is demoted, else the ceiling
    tier_for(paths)    min(tier_of(p) for p in paths), and ZERO for none

**The ceiling is a genuinely repository-wide fact and stays one.** A sandbox
either builds or does not; an execution source (a suite, or an opted-in entry
point) either exists or does not. Neither is a per-file question, and dressing
them up as one would be granularity theatre. What the ceiling means is *the best
tier anything here could reach* — never "this repository is Tier 1".

**Gate 3 subtracts from it one module at a time,** carrying the module's dotted
name, its path, and its own import error. The path is what makes the verdict
answerable downstream: a `Change` is one file (SPEC §3.9), so a demotion keyed
only by module name would need re-resolving at every call site.

**A path gate 3 never smoked takes the ceiling.** This is the demote-only rule
applied to absence, and it is the load-bearing choice in this ADR. What goes
unsmoked is the suite's own files, `__main__.py`, `setup.py`, `conftest.py` and
anything unaddressable — and gate 3 not having demoted them is not evidence
against them. The upward-resolution worry this raises is answered one layer
down instead: an edit reaches Tier 1 only if `characterize.Coverage` also holds
a surviving oracle for the symbols it touches, and that table resolves an
unknown symbol to Tier 0. **The probe supplies the ceiling; the coverage table
supplies the floor-by-evidence.** Composing them is `min()`, which is why
`TierVerdict.tier_for` and `Coverage.tier_for` agree about the empty case.

**A repository where nothing imports still pins to Tier 0 outright.** Demoting
every module and keeping a Tier 1 ceiling would be a claim about the empty set,
and it would read in the run log as a Tier 1 repository. Gate 4 is also skipped
there, unchanged: it is the riskiest command the probe issues, and it is not
worth running to decide the tier of nothing.

**The run log names the demoted paths, capped at twelve.** Issue #28 asks it to
say which code landed in which tier *and why*; a count answers only the first
half. `ModuleNotFoundError: celery` next to `examples/celery/make_celery.py` is
what tells a developer at 3am that the demotion is somebody's example app and
not their library.

## Alternatives rejected

**A tier per symbol, from the probe.** What question #4 says, read literally.
Rejected because the probe has no symbol-level evidence to offer: gate 3 imports
*modules*, and an import says nothing about which functions inside one could be
characterized. The probe would have to invent per-symbol answers by broadcasting
a module's result across its contents, which is per-module tiering wearing a
finer name — while the real per-symbol table already exists one layer down,
built from a trace, where the evidence is.

**Inverting the order so characterization runs first and the probe consumes its
coverage.** The honest way to make the probe per-symbol, and rejected because
characterization *executes the repository's suite four or more times*. The probe
is what currently earns the right to do that. Running it first would mean
executing a stranger's repository before deciding whether Polish should, which
inverts the safety argument SPEC §3.3 rests on.

**A complete per-path table, with an absent row meaning Tier 0.** The shape most
readers would reach for, and the one that silently converts gate 3 into a
promotion gate — see above. It also *looks* safer than what was chosen, which is
why it is written down here: the next person to find `tier_of` returning the
ceiling for an unsmoked path will think it is a bug.

**Keeping the field named `tier`.** Every call site would have gone on
compiling. That is the argument against it: the number means something new, and
a name that lets old code keep its old assumption is how a semantic change
becomes a silent one. `ceiling`, and `reaches_tier_one` in place of `tier_one`,
force each call site to be re-read.

**Demoting the transitive importers of a demoted module too.** Sound-looking,
and unnecessary: a module that imports a broken one is itself broken, so it
fails the smoke on its own and arrives in the demotion set without a graph walk.
The set is already closed under the import relation.

## Consequences

- **`flask` stops being a false Tier 0**, and the corpus confusion matrix
  changes shape: the row moves to the diagonal with 75 paths at Tier 1 and 8
  held at Tier 0. Zero false Tier 1 is unchanged and is now enforced at both
  granularities — no repository labelled Tier 0 may have *any* path resolve to
  Tier 1.
- **A Tier 1 verdict no longer means the whole repository is characterizable.**
  Anything reading the ceiling as a repository-wide promise is now wrong;
  `tier_of(path)` is the only thing entitled to decide an edit.
- **Per-path granularity made a pre-existing addressing hole visible.** Three
  real `flask` modules under `src/flask/sansio/` — a namespace subpackage —
  are addressed as bare `app`, `blueprints` and `scaffold` and fail the smoke.
  Under the per-repository verdict flask was Tier 0 anyway and this cost
  nothing; it is now three demotions of library code for a reason belonging to
  Polish. A false Tier 0, so nothing unverified ships, and it is pinned by name
  in `tests/test_probe_corpus.py` and tracked in
  [#34](https://github.com/yashc17/polish/issues/34).
- **The probe must keep being handed the whole first-party set.** ADR 0008
  already refused to scope it, on the grounds that a subset would be a narrower
  claim wearing the same name. Under a per-path verdict the argument is
  stronger and runs the other way: an unsmoked path keeps the ceiling, so
  scoping the probe would *widen* the verdict rather than narrow it.
- **The Tier 1 edit loop ([#15](https://github.com/yashc17/polish/issues/15))
  composes two rules rather than one.** The probe's per-path ceiling and the
  coverage table's per-symbol floor are both binding, and both resolve downward,
  so the composition is `min` and the order does not matter.
