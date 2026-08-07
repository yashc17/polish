# ADR 0004 — mypy is the Tier 0 type check, gated on introducing no new diagnostic

**Status:** Accepted · 2026-08-07
**Context:** [#11](https://github.com/yashc17/polish/issues/11) · SPEC §3.1,
§3.4, §4.4

## Context

Tier 0's claim is "provably behaviour-preserving", and SPEC §3.1 names the
proof: **AST-equivalence plus a clean type check**. The AST half is Polish's
own (`polish.tier0.prove`). The type-check half needs a real checker, and
nothing was installed. §3.4's tooling table names *pyright / Jedi* under
"Resolution", but that row was written about symbol resolution for the Tier 1
edit loop, not about this gate, and it had never been tested against what this
gate actually has to do.

What it actually has to do is narrower and harder than "type check the repo":

- It runs **unattended, overnight, possibly offline**, once per candidate
  change.
- Its target is a repository **with no test suite**, which is not a repository
  that type checks clean today. Almost none do.
- The failure it exists to catch is a binding removed from `a.py` that `b.py`
  imports back out of it. That is invisible to anything reading one file.

## Decision

**mypy**, run as `python -m mypy` from Polish's own interpreter, over the whole
first-party file set, with the gate set at **"no diagnostic that was not
already in the baseline"**.

```
--ignore-missing-imports --no-site-packages --explicit-package-bases
```

Three consequences are deliberate:

1. **Whole-tree, not per-file.** The re-export failure above is caught because
   `b.py` is in the same invocation. A per-file check would ship it.
2. **Relative, not absolute.** The baseline is taken before the first edit and
   re-taken after each accepted change. Diagnostics are compared on file,
   severity and text, with line numbers discarded — deleting a line moves
   everything below it, and comparing on position would report the whole file
   as new.
3. **Unverifiable is a refusal, not a pass.** mypy exiting ≥ 2, failing to
   start, or timing out raises `Unverifiable` and the run ships nothing. A
   change Polish cannot verify is a change Polish does not make (§1.1).

`--no-site-packages` is what keeps it honest: Tier 0 is by definition the tier
that could not install the repository's dependencies, so Polish's own
site-packages must not be mistaken for the repo's.

## Alternatives rejected

**pyright.** Better inference and better name resolution, and it is what §3.4
names. But the pip distribution is a shim that downloads a Node runtime on
first use. An unattended 3am run on a machine that is offline, or behind a
proxy, would fail at the gate — and the gate failing means the night ships
nothing. It also puts a second language runtime inside a product whose install
story is `uv tool install polish`. Worth revisiting for Tier 1, where richer
resolution buys more than it costs here.

**An `ast`-based scope check with no dependency.** Zero install cost, fast,
fully under our control. But it is re-implementing pyflakes, it cannot see
across files without becoming a whole-program analysis of its own, and it
would force §3.1's wording to stop saying "type check". The one axis it is
weakest on is the axis that is the product.

**Requiring a clean baseline rather than no new diagnostics.** Simpler to
explain and a stronger claim per change. Rejected because on Tier 0's stated
target — a repository with no tests — it never passes, so it would be a gate
that never fires and therefore proves nothing. A gate that always ships
nothing and a gate that always ships everything are equally uninformative.

## Consequences

- `mypy` becomes a runtime dependency of `polish`, and a CI install cost.
- The gate's strength is bounded by the baseline's noise. A repository already
  full of diagnostics hides less than a clean one would — but the specific
  failure this exists to catch (`Module "demo.a" has no attribute "json"`) is
  a *new* message about a *specific* symbol, so it survives a noisy baseline.
- mypy's own version is now part of the verification story: an upgrade that
  changes a message's wording reads as one diagnostic disappearing and another
  appearing. Within a single run this cannot happen, because the baseline and
  the check are the same binary minutes apart.
