# ADR 0006 — A command allowlist of fixed shapes, not of programs

**Status:** Accepted · 2026-08-07
**Context:** [#12](https://github.com/yashc17/polish/issues/12), implementing
[#3](https://github.com/yashc17/polish/issues/3#issuecomment-5212772529) ·
SPEC §3.3, §4.1

## Context

SPEC §3.3 accepts that Polish cannot contain the machine, and says so in the
strongest terms available to a spec: "**This is the spec's weakest guarantee and
it must be documented honestly in the README**, not buried." The mitigation it
names is a command allowlist. Until #12 there was no allowlist, and the README
said that too.

The tier probe is the first component that has to have one. It installs
packages and runs an unknown repository's test suite, unattended, at 3am, as the
user, against the user's filesystem. Issue #12 makes it an acceptance criterion:
"the probe respects the command allowlist and is safe to run unattended on an
untrusted repo."

## Decision

**The allowlist constrains command *shapes*, not a set of program names. The
repository never supplies a program.**

Every command Polish issues is the sandbox virtualenv's own interpreter in one
of four forms, and a `Command` object cannot be constructed outside them:

```
<venv>/bin/python -m pip install --only-binary :all: <requirement>...
<venv>/bin/python -m pytest -q --tb=no -p no:cacheprovider
<venv>/bin/python -c "import <module>[, <module>...]"
<venv>/bin/python -m <module>   |   <script>.py         (gate 4, opt-in only)
```

Four consequences follow, and each is a rule rather than a habit:

1. **No shell, ever.** `argv` lists, `shell=False`, `stdin` closed. An
   unattended run must never be a process waiting at a prompt nobody will answer.
2. **pip is never handed repository-authored options.** A `requirements.txt` is
   content the repository wrote and pip reads as *flags* — `--index-url` would
   redirect the install to a host the repository chose, `-e .` would build the
   project. So Polish never passes `-r`: it parses the files itself and forwards
   only requirement specifiers, which is what lets the validator refuse any pip
   argument beginning with a dash.
3. **`--only-binary :all:`.** No source distribution is built, so no third-party
   `setup.py` runs, and the project itself is never installed. Gate 1 therefore
   executes no repository code at all, which is what makes it safe to run first.
   A repository genuinely needing a source build fails gate 1 and lands Tier 0 —
   the acceptable error.
4. **A built environment, not an inherited one.** The child gets a constructed
   `PATH`, a `HOME` pointing at a scratch directory that dies with the probe, and
   nothing else. An untrusted test suite cannot read the API keys sitting in the
   shell that launched Polish.

Validation lives in `Command.__post_init__`, not at dispatch. An ill-formed
command has no moment in which it exists, and substituting the runner — which
the corpus does — cannot widen what is legal.

Gate 4's entry point is the one place a *user* names something to execute. It
accepts a dotted module or a repository-relative `.py` file and nothing else.
`make test` is not available here, and neither is any other string a repository
authored.

## What this does not contain, stated plainly

**Running a test suite is running the repository's code.** `conftest.py`
executes at collection, and `addopts` in the repository's own config reaches
pytest without passing through `argv`. Gate 2 accepts this knowingly. It is the
same bargain SPEC §3.2 already made for Tier 1 edits, and pretending otherwise
would be worse than the exposure.

So the allowlist's real claim is narrower than "the probe is sandboxed": it is
that **Polish never becomes a vector for a repository to choose what runs.** The
repository's own code runs, in the sandbox, under the interpreter Polish built.
Nothing else does.

Two residual holes, named rather than hidden:

- A `pyproject.toml` can point pytest at plugins that then execute. That is
  inside "running the suite", above.
- `--only-binary :all:` prevents source builds; it does not prevent a malicious
  *wheel* from executing on install. Wheels can carry install-time hooks.

## Why not the alternatives

**A user-extendable allowlist** (`[probe] allow = ["make test"]`). More
repositories reach Tier 1, and the user takes the risk explicitly. Rejected for
v1 because the first version of a safety mechanism should be the one with no
escape hatch: an escape hatch added later is a decision; one shipped on day one
is a default. It remains available if the corpus shows Tier 1 reach is the
binding constraint.

**Honouring the repository's declared test command** (`tox.ini`, `Makefile`,
`noxfile.py`). Maximum Tier 1 coverage, and impossible to square with "safe to
run unattended on an untrusted repo": the probe would execute a command string
the repository authored, which is precisely the thing an allowlist exists to
prevent.

**Docker.** Already rejected in SPEC §3.3, for reasons that apply here more
sharply than anywhere else: a container that forces every CUDA repository down
to Tier 0 has made the product worse while sounding safer.

## Consequences

- The README's "that allowlist does not exist yet" comes out. What replaces it
  says what the allowlist does and does not cover, in the register §3.3 demands.
- Gate 1's "executes no repository code" is now a property of the design rather
  than an aspiration, and `--only-binary :all:` is load-bearing for it.
- Some repositories will fail gate 1 for needing a source build, and land Tier 0
  with a correct verdict for a reason that is about packaging rather than about
  characterizability. That is a false Tier 0, which the corpus permits and
  measures.
- The Tier 1 edit loop (#13 onwards) inherits this allowlist rather than
  inventing a second one. Anything it needs to run that is not one of the four
  shapes is a change to this ADR, deliberately.
