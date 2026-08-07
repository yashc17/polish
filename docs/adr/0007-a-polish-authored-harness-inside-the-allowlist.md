# ADR 0007 — A fifth command shape, whose program is a Polish constant

**Status:** Accepted · 2026-08-07 · **amends
[ADR 0006](0006-a-command-allowlist-of-fixed-shapes.md)**
**Context:** [#13](https://github.com/yashc17/polish/issues/13), implementing
[#5](https://github.com/yashc17/polish/issues/5#issuecomment-5212774533) ·
SPEC §3.1, §3.3, §3.4, §4.5

## Context

ADR 0006 ends by saying that the Tier 1 loop "inherits this allowlist rather
than inventing a second one. Anything it needs to run that is not one of the
four shapes is a change to this ADR, deliberately." This is that change.

The characterization harness (#13) needs two things none of the four shapes can
express, and neither is a matter of adding a flag:

1. **Per-test outcomes.** The flake baseline quarantines *tests*, so it has to
   know which test did what. `python -m pytest -q --tb=no` prints a row of dots.
2. **A tracer.** SPEC §3.1 defines Tier 1 as characterization from real
   execution traces and SPEC §3.4 names `sys.settrace` as the mechanism. No
   pytest command line installs a trace function.

Three ways to get them were available, and two of them are worse than admitting
a fifth shape.

**Widening the pytest shape** (adding `-rA` and scraping the short summary)
solves the first need and not the second, so the harness would still be
unbuildable — and it would make the baseline depend on parsing pytest's human
output, which is not a stable interface.

**A `sitecustomize.py` written into the sandbox virtualenv** solves both,
needs no new shape, and is the worst option of the three. The traced run would
be issued as `python -m pytest`, indistinguishable in the run log from the
probe's gate 2. A mechanism that does not appear in the log is a mechanism a
user cannot audit, which is the thing the allowlist exists to prevent — and
achieving it by *not touching the allowlist* would be routing around this
document rather than amending it.

## Decision

**A fifth shape, carrying a program that is a constant in Polish's own source.**

```
<venv>/bin/python -P -c "import _polish_harness as _h; _h.<task>()"
```

where `<task>` is one of exactly three: `suite`, `trace`, `oracles`. The
validator compares the tail against the values of a dictionary in
`polish/probe/_commands.py` — **equality against a constant, not a pattern over
one.** A shape whose tail is checked for *structure* can always be argued into
admitting one more thing. A shape whose tail must equal a string in the source
cannot, and that is the only reason this shape is defensible at all.

Three properties hold, and together they are the argument:

1. **The command carries no repository input.** Not a path, not a test id, not a
   requirement name. It is byte-identical on every repository Polish is ever
   pointed at, which is why widening it is a source change and not a
   configuration one.
2. **The harness's parameters travel in the environment Polish builds** — a JSON
   file for the configuration and one for the result, both named by variables
   added to `probe.sandbox_runner(extra=...)`. The environment is constructed
   rather than inherited, so this channel is Polish-authored end to end.
3. **`-P` is load-bearing, not decoration.** For `python -c`, `sys.path[0]` is
   the current directory — which is the repository. Without `-P`, a repository
   containing `_polish_harness.py` would supply the program, and the shape would
   become precisely the thing ADR 0006 forbids. `-P` removes that entry;
   Polish's scratch directory then leads `PYTHONPATH`, ahead of the
   repository's own import roots.

The harness module itself is copied into that scratch directory from Polish's
source tree. It imports nothing from `polish` — the sandbox virtualenv does not
have Polish installed, and installing it there would put Polish's dependencies
in front of the repository's.

## What this does not contain, stated plainly

**Running the suite three times is still running the repository's code, three
times.** This shape does not narrow the exposure ADR 0006 already named; it
multiplies it by N. The bargain is unchanged and so is the reason for it —
what is being bought is the flake baseline, and there is no way to observe
whether a suite is deterministic without running it more than once.

**The tracer sees everything the suite executes.** `sys.settrace` is installed
process-wide, and the recorder filters to first-party frames. A repository could
make tracing expensive; it cannot make it read anything the suite was not
already able to read.

**One residual hole, named rather than hidden.** The generated oracles are run
with `-c` pointing at a `pytest.ini` Polish wrote, so the repository's `addopts`
do not reach them — but the repository's *installed pytest plugins* still load,
because they are entry points rather than configuration. That is inside "running
the suite", above.

## Why not the alternatives

Both are in **Context** above, because the argument for this shape is mostly the
argument against them: `-rA` cannot install a tracer, and `sitecustomize.py`
would work while making the run log lie by omission.

A third was considered and rejected earlier: **passing failing test ids back to
pytest** for the M=2 retry, which would be much cheaper on a large suite than
re-running the whole thing. A node id is repository-authored text — a file path,
a test name, a `parametrize` repr — and it would have been the first place such
text reached a command line. Issue #5's retry is therefore implemented as whole
suite re-runs. The cost is real and is documented where it is paid.

## Consequences

- The allowlist is five shapes, and `docs/agents/*`, the README and `CONTEXT.md`
  say five.
- `polish.probe.sandbox_runner` becomes public, because the harness has to
  execute in the same sandbox, with the same built environment, as the probe.
  Inheriting the function rather than the idea is what keeps ADR 0006's
  "not a second one" literally true.
- Characterization multiplies suite runtime by roughly `N + 1 + R` before any
  edit is attempted. `characterize.Policy.total_seconds` is where that is
  bounded, and it is the reason the policy exists as a separate injectable from
  the probe's `Budget`.
- A repository promoted to Tier 1 solely by gate 4's opted-in entry point is not
  traced: the harness's execution source is the suite. It comes back with no
  oracles and every symbol at Tier 0 — correct, and less useful than it should
  be. Tracked in [#32](https://github.com/yashc17/polish/issues/32) rather than
  papered over here. Note it needs a fourth task and not a sixth shape: the
  program stays a constant, and the entry point's target travels in the harness
  configuration like every other parameter.
