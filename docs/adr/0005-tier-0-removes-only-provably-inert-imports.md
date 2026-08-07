# ADR 0005 — Tier 0 removes an unused import only when the module behind it is provably inert

**Status:** Accepted · 2026-08-07
**Context:** [#11](https://github.com/yashc17/polish/issues/11) · SPEC §1.1,
§3.1, §4.4

## Context

"Remove unused imports" sounds like the safest refactor there is, and it is the
one Tier 0 ships first. It is not unconditionally safe.

Importing a module **runs** it. Running it can register a plugin, patch a
builtin, choose a matplotlib backend, install a readline hook or open a socket
— none of which is visible anywhere in the file being edited. `import seaborn`
sets matplotlib's styles. `import myapp.handlers` is how half the registration
patterns in Python are spelled. Removing either changes behaviour, and no
amount of reading the importing file will say so.

Tier 0 is by definition the tier that **cannot execute the repository**, so it
cannot find out by running it. That leaves a choice about what "unused import"
is allowed to mean.

## Decision

An unused module-level import is removed only when every module the statement
would execute is **provably inert**:

- **the standard library**, minus a denylist of modules whose import *is* the
  point — `readline`, `rlcompleter`, `site`, `sitecustomize`, `usercustomize`,
  `this`, `antigravity`;
- **a first-party module** whose entire top level is docstrings, literal
  assignments, undecorated `def`s, `class`es without decorators, bases or
  `metaclass=`, further inert imports, and `if TYPE_CHECKING:` /
  `if __name__ == "__main__":` blocks, which never run on import.

Parents count: `import demo.plugins.slack` executes `demo`, `demo.plugins` and
`demo.plugins.slack`, so a registration in any of the three blocks the removal.
Inertness is transitive and handles import cycles by assuming a module already
under examination is inert, which is sound because every statement of every
module in the cycle is still examined exactly once.

**Everything else is left alone, with the reason recorded in the commit
message** of whatever change did land in that file. A reviewer who sees one
import go and another stay is owed the difference between them.

**An author's own declaration outranks all of this.** An import carrying a
`noqa` directive, or written `import x as x` (PEP 484's explicit re-export), is
left alone whatever the inertness analysis concludes. This was added *after*
the first dogfood run against `psf/requests`, where both of the two changes
Polish produced were wrong:

```python
# Import encoding now, to avoid implicit import later.
# Implicit import within threads may cause LookupError when standard library
# is in a ZIP, such as in Embedded Python. See psf/requests#3578.
import encodings.idna  # noqa: F401
```

Every rule above was working correctly and still got this wrong, because
`encodings.idna` is a standard-library module and the standard library was
being treated as inert wholesale. The directive is a maintainer on record
saying the import stays, and an unattended agent that overrides an explicit
human decision has broken the thing that lets it be unattended. `encodings`
was added to the side-effecting denylist at the same time, but the directive is
the rule that generalises.

Independently of inertness, these are never touched: a package `__init__.py` or
a `.pyi` stub (re-export surfaces whose consumers are other repositories), a
`from __future__` import, a star import, any import not a direct child of the
module body (deferred imports, `TYPE_CHECKING` blocks and
`try/except ImportError` probes are all deliberate structures), a name bound
more than once in the file, and any file that reaches names dynamically —
`eval`, `exec`, `globals`, `locals`, `vars`, `__import__`, `importlib` or a
module-level `__getattr__`.

## Alternatives rejected

**Remove anything not visibly suspicious** — which is what `ruff --fix` does,
and it yields several times more removals per run. Rejected because Tier 0's
one sentence is "provably behaviour-preserving" (§3.1), and `import seaborn`
makes that sentence false. Downgrading it to "probably" gives up the wedge:
the product is not that the refactoring is good, it is that it can be left
unattended (§1.1). Note that ruff carries a `--unsafe-fixes` distinction for
exactly this reason, and a night-shift agent has no human present to opt in.

**Stdlib only**, dropping the first-party inertness analysis. Simplest to
explain and one fewer thing to get wrong. Rejected because the registration
import — `import myapp.handlers` — is both the case most worth being careful
about *and* the case where a static answer is actually available: the source is
right there in the tree. Refusing to look at it would mean being conservative
where we could be precise, and there is no safety gained by that.

## Consequences

- On a typical repository the yield is dominated by unused `typing` and
  standard-library imports. `import numpy as np` stays, forever, at Tier 0.
  That is the honest boundary of a tier that cannot run the code, and it is
  stated in the commit message rather than left as silence.
- Tier 1, which *can* execute the repository, is where the third-party case
  gets a real answer — characterization tests observe the import's effects
  rather than guessing at them (§3.1). This ADR is a Tier 0 rule, not a
  product-wide one.
- **The standard-library carve-out is a denylist, and that is the weak point.**
  It is only as good as what is on it, and `encodings` was not on it until a
  real repository proved it should be. The alternative — proving inertness
  from the standard library's own source, the way first-party modules are
  proven — rejects `os` and `json` along with everything else and leaves Tier 0
  with nothing to do. The directive rule above is the backstop, since an import
  kept for its side effect is nearly always marked as one, but a repository
  that neither marks it nor is on the denylist is still a way to get this
  wrong. Dogfood against a new repository before widening anything here.
- The inertness rule is deliberately syntactic and strict. `class Error(Exception)`
  is not provably inert here, because bases are evaluated at class creation and
  that is how ORMs and plugin registries collect subclasses. Refining it means
  proving something about the base, which is a whole-program analysis; the
  cost of not refining it is one skipped removal.
