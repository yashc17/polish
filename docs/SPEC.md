# Polish — Project Spec

**Status:** Draft v0.1 — settled by design interview, 2026-08-06
**Owner:** Yash Chanchani

---

## 1. What Polish is

An autonomous night-shift refactoring agent for developers who write code fast
with AI assistance and accumulate debt doing it. You close the laptop; Polish
reviews the day's work, refactors it, verifies it didn't break anything, and
leaves a reviewable branch behind.

**Distribution:** open-source product, for all developers — not a personal tool.
Every design decision below is made for a stranger's repo, not a specific one.

### 1.1 The wedge

> **Autonomous refactoring you can actually leave unattended.**

Anyone can prompt an LLM to clean up a file. Nobody trusts it unsupervised.
Polish's product *is* the safety apparatus around the edit — snapshot,
characterize, refactor, verify, revert. The refactoring is the commodity; the
guarantee is the moat.

This has a consequence that governs everything downstream: **a change Polish
cannot verify is a change Polish does not make.** Any feature that weakens the
verification story is not a feature.

### 1.2 Non-goals

- Not a linter runner. `ruff` and `eslint` already exist and Polish will call
  them, not replace them.
- Not an interactive assistant. No chat, no prompt, no babysitting.
- Not a code generator. It only touches code that already exists.
- Not multi-model. The safety guarantee is model- and language-specific and
  cannot be made portable (see §3.4).

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ DAY                                                             │
│  observer daemon ──▶ code graph + intra-day change history      │
│  (file watcher)      (AST, call graph, churn, deleted-at-5pm)   │
└─────────────────────────────────────────────────────────────────┘
                              │
                       `polish run`
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ NIGHT                                                           │
│  1. snapshot     → snapshots/raw/YYYY-MM-DD  (unedited copy)    │
│  2. scope        → full sweep (first run) | incremental + closure│
│  3. tier probe   → can this repo be executed?                   │
│  4. plan         → agent proposes change queue                  │
│  5. per change:  → edit (Agent SDK) → verify → repair? → keep/drop│
│  6. output       → branch polish/YYYY-MM-DD, 1 commit / change  │
└─────────────────────────────────────────────────────────────────┘
```

Six components. Each is independently testable.

| Component | Responsibility |
|---|---|
| **Observer daemon** | Watches the working tree during the day; maintains the persistent code graph and intra-day change history. |
| **Snapshotter** | Writes an unedited copy of the tree before any night-shift work begins. |
| **Tier probe** | Determines whether the repo can be executed, which selects Tier 0 or Tier 1. |
| **Refactor engine** | Tier 0 deterministic codemods; Tier 1 Claude Agent SDK edits. |
| **Verifier** | Runs characterization tests, existing tests, type checks, linters. Owns keep/repair/drop. |
| **Reporter** | Branch, per-change commits, changelog, run log. |

---

## 3. Settled decisions

Each carries the reasoning, so future-you can tell a real constraint from an
arbitrary one.

### 3.1 Safety model — tiered by what's verifiable

Polish probes what it can prove, then earns the right to be aggressive.

| | **Tier 0** — cannot execute repo | **Tier 1** — repo runs |
|---|---|---|
| **Verification** | AST-equivalence + type check | Characterization tests from real execution traces |
| **Permitted** | Unused imports, unreachable branches, formatting, safe renames | Everything in Tier 0, plus cross-file dedup, dead-code removal, extraction, docstrings |
| **Edit engine** | Deterministic codemods (LibCST) | Claude Agent SDK |
| **Claim** | "Provably behavior-preserving" | "Verified against recorded behavior" |

**Why tiered.** The single-engine alternatives both fail: characterization-only
hard-fails on the AI/ML and research repos that need this most (no tests, needs
API keys, needs a GPU), and LLM-written tests give false confidence — a vacuous
test that passes before and after is actively corrosive to a trust-first
product. Tiering degrades gracefully and keeps every claim honest.

**Cost:** two engines to build, test, and document instead of one.

### 3.2 Edit engine — LLM edits, harness verifies

Tier 1 edits are made by an agent (Claude Agent SDK — Claude Code as a library,
shipping the agent loop plus built-in Read/Write/Edit/Bash/Glob/Grep). Polish
owns the envelope around it, not the edit itself.

**Why not codemods.** "Extract the 200 shared lines from these five files"
needs judgment. Generalizing a codemod for that shape is harder than verifying
the result.

**The bargain:** every edit is a black box, so the verification layer must be
genuinely airtight. That is the wedge, so the cost is one we already accepted.

### 3.3 Sandbox — git worktree + isolated venv

Two things need isolating: the working tree (from bad edits) and the machine
(from arbitrary execution).

Polish works in a detached worktree with its own virtualenv. **The working tree
is untouchable. The machine is not isolated** — mitigated by a command
allowlist, not by containment.

**Why not Docker.** Real isolation, but it fails exactly where Tier 1 matters —
CUDA repos, large local datasets, mounted credentials. Safe-by-default is a lie
if it doesn't run: a container that forces every ML repo down to Tier 0 has
made the product worse while sounding safer.

**This is the spec's weakest guarantee and it must be documented honestly in
the README**, not buried.

### 3.4 Languages — Python + TypeScript

The two dominant AI-assisted-development ecosystems. Depth over breadth
*within* that pair: the trust claim is only as strong as the weakest language's
analysis, so shipping four half-analyzed languages would mean four ways to
silently break someone's code.

| | Python | TypeScript |
|---|---|---|
| Transforms | LibCST | ts-morph |
| Resolution | pyright / Jedi | tsc |
| Tests | pytest + coverage | vitest / jest |
| Tracing | `sys.settrace` | instrumentation (TBD) |

### 3.5 Implementation — Python core, Node sidecar

Core in Python, distributed via `uv tool install polish` / `pipx`. A Node
sidecar handles TypeScript analysis over JSON-RPC.

**Why this way round.** The hardest, most safety-critical work —
`sys.settrace` characterization, coverage-driven dead-code detection, LibCST
transforms — is Python-only and cannot be driven from Node. TypeScript analysis
is comfortable behind a process boundary. Put the core where the irreducible
work is.

**Accepted cost:** Node's install UX (`npx`) is better than Python's, and
debugging a tracer across two runtimes is unpleasant.

### 3.6 Scope — full sweep once, incremental after

- **First run:** whole-repo deep pass. Opt-in, explicitly reviewed. The one big
  scary diff happens once, while the developer is paying attention.
- **Every run after:** files changed since the last run, **plus their
  dependency closure**.

The closure is where the code graph earns its keep: deleting a caller today can
strand a function three files away, and `git diff` will never surface that.

### 3.7 Observer daemon — kept

A persistent watcher maintains the code graph and intra-day history through the
day.

**The justification is history, not speed.** A cold parse is fast; what the
daemon captures is what `git diff` erases — code written at 2pm and deleted at
5pm, the order changes happened in, which functions churned repeatedly. That
signal is unavailable at run time and directly informs which refactors are safe
(a function rewritten four times today is not a dedup candidate).

**Cost:** an always-on process users must install, trust, and debug — inside a
product whose pitch is safety. It must be trivially inspectable and killable.

### 3.8 Auth and cost — inherit Claude Code auth, hard budget cap

The Agent SDK resolves credentials the same way Claude Code does: an existing
`ant auth login` profile, or `ANTHROPIC_API_KEY`. Anyone already running Claude
Code gets zero-config Polish on their existing plan; everyone else exports a
key.

**A per-run token budget is mandatory, not optional.** On exhaustion Polish
halts cleanly, ships what it verified, and reports. An unattended overnight
agent without a hard cap is indefensible.

Model economics for budgeting (per MTok):

| Model | ID | Input | Output |
|---|---|---|---|
| Opus 5 | `claude-opus-5` | $5 | $25 |
| Sonnet 5 | `claude-sonnet-5` | $3 ($2 intro → 2026-08-31) | $15 ($10 intro) |
| Haiku 4.5 | `claude-haiku-4-5` | $1 | $5 |

Default to `claude-opus-5`. Per-stage model routing is deferred (§5, open).

### 3.9 Review UX — branch, one commit per change

Output goes to `polish/YYYY-MM-DD`. **Each refactor is its own commit**, tagged
by category, with verification evidence in the message:

```
refactor(dedup): extract shared prompt-loading into prompts.py

Merges duplicated loader from qa.py, tc.py, ii.py (203 lines → 68).

Tier: 1 (characterization)
Tests generated: 14 · passed: 14
Type check: clean
Repair attempts: 0
```

`polish run --pr` opens a pull request when a remote exists.

**Why not always-PR.** It hard-requires a GitHub remote, which a large share of
local and experimental repos don't have. Per-commit granularity also *is* a
trust feature: nothing is all-or-nothing, and a developer who distrusts one
category can drop it without losing the night.

### 3.10 Failure policy — one repair attempt, then drop

When verification fails on a change:

1. Feed the failing output back to the agent for **exactly one** bounded repair
   attempt. Same move a human makes; well within what the Agent SDK loop does.
2. Still failing → revert **that change alone**, log it with the failure
   output, continue the queue.

**Never ship a red change.** Aborting the whole run on one failure means a
single flaky test wastes the night; dropping without repair discards work an
obvious fix would have saved.

### 3.11 v1 scope — full pipeline, Python only

Every layer end-to-end — daemon, tier probe, snapshot, characterization, both
tiers, Agent SDK edits, verification with repair, branch output — **Python
only**. TypeScript is v2, via the sidecar seam designed in from the start.

**Why.** Half a safety envelope is worth nothing. The wedge can only be
demonstrated by the complete loop, so depth-first is the only sequencing that
produces a releasable v1. One language proven beats two half-analyzed.

**Deferred to v2+:** TypeScript, dependency updates, module-level doc
generation.

---

## 4. v1 acceptance criteria

Polish v1 ships when, on a Python repo it has never seen:

1. `polish run` completes unattended with no human input.
2. A raw snapshot exists at `snapshots/raw/YYYY-MM-DD` and byte-matches the
   pre-run tree.
3. The tier probe correctly classifies runnable vs non-runnable repos.
4. **Tier 0 on a repo with no tests produces a diff that is provably
   behavior-preserving** (AST-equivalence + clean type check).
5. **Tier 1 generates characterization tests from real execution, and every
   shipped change passes them.**
6. A deliberately broken refactor is caught, gets one repair attempt, and is
   dropped if unrepaired — with the failure in the log.
7. Output is a branch with one reviewable commit per change, each carrying
   verification evidence.
8. The run halts cleanly at the token budget and reports what it completed.
9. The working tree is untouched throughout.

Criteria 4, 5, and 6 are the product. The rest is plumbing.

---

## 5. Open questions

Real unknowns, not decisions being avoided. Each is tracked as a GitHub issue
whose number matches the question number, and the answer is written up in that
issue rather than here — this table is the index, not the record.

A question is closed when it has been genuinely answered, and never deleted:
the rejected alternatives are the most useful part of the answer, and code
merged against these numbers still cites them.

### Still open — three, all evidence-blocked

None of these can be settled by argument. Each is waiting on a measurement, and
settling it by conversation beforehand would manufacture a confidence nothing
had earned — which is the failure §3.1 rejects LLM-written tests for.

| # | Question | Why it matters | What would settle it |
|---|---|---|---|
| [4](https://github.com/yashc17/polish/issues/4) | **Characterization coverage.** What if execution only reaches 15% of the code? Tier 1 for covered paths and Tier 0 elsewhere, or per-file tiering? | Partial coverage is the common case, not the exception. | Real coverage distributions from the harness in [#13](https://github.com/yashc17/polish/issues/13); decided in [#14](https://github.com/yashc17/polish/issues/14). First datum: `flask` in the probe corpus passes 485 tests with every `flask.*` module importing, and is demoted to Tier 0 by eight unimportable modules under `examples/` and `docs/`. |
| [7](https://github.com/yashc17/polish/issues/7) | **Model routing.** Single model, or cheap models for mechanical passes? | Big cost lever; a cheap model making an unsafe edit violates the wedge. Needs data, not a guess. | The cost-per-verified-and-shipped-change experiment in [#17](https://github.com/yashc17/polish/issues/17). |
| [8](https://github.com/yashc17/polish/issues/8) | **Budget default.** What per-run cap is generous enough to be useful and small enough to be safe? | Sets the first-run impression for every user. | Token accounting from [#16](https://github.com/yashc17/polish/issues/16), then decided in [#18](https://github.com/yashc17/polish/issues/18). |

### Answered

| # | Question | Answer | Recorded in |
|---|---|---|---|
| [1](https://github.com/yashc17/polish/issues/1) | **Non-git repos.** Shadow-init, or refuse with instructions? | Refuse and exit non-zero, printing the `git init` line that fixes it, plus `--init-git` on explicit opt-in. No degraded non-git mode — both fallbacks would have added a second, less-exercised safety path. | [ADR 0002](adr/0002-refuse-non-git-repositories.md) · shipped in [#10](https://github.com/yashc17/polish/issues/10) |
| [2](https://github.com/yashc17/polish/issues/2) | **First-party detection.** Distinguishing source from vendored/venv code. | Layers compose by successive refinement, not first-match-wins: environment markers prune first and outrank packaging metadata, and `polish.toml` beats everything. | [ADR 0001](adr/0001-marker-evidence-outranks-packaging-metadata.md) · shipped in [#9](https://github.com/yashc17/polish/issues/9) |
| [3](https://github.com/yashc17/polish/issues/3) | **Tier probe mechanics.** How is "can I run this repo" determined? | Four layered hard gates, no scoring: venv build, test suite, import smoke, opt-in entry points. Import smoke is **demote-only** — passing it promotes nothing, because import-time tracing produces the vacuous tests §3.1 calls corrosive. | [#3](https://github.com/yashc17/polish/issues/3) · allowlist in [ADR 0004](adr/0004-a-command-allowlist-of-fixed-shapes.md) · shipped in [#12](https://github.com/yashc17/polish/issues/12) |
| [5](https://github.com/yashc17/polish/issues/5) | **Flaky test detection.** Repeat-run detection, or quarantine? | Both, asymmetrically. The user's suite: a 3× pre-run baseline quarantines flakes, with 2× retry as a backstop. Generated oracles are stricter — any variance across 3 replays and the test is **discarded, never emitted**, because a flaky oracle can pass a broken change. | [#5](https://github.com/yashc17/polish/issues/5) · to be built in [#13](https://github.com/yashc17/polish/issues/13), [#15](https://github.com/yashc17/polish/issues/15) |
| [6](https://github.com/yashc17/polish/issues/6) | **Daemon storage.** Format, location, size bounds, crash and version skew. | SQLite in a `.polish/` that ignores itself. The graph is verifiable and cold-rebuilds from the tree; the history is not, so a downtime gap reports churn as **UNKNOWN, never "no churn"**. | [ADR 0003](adr/0003-observer-state-in-a-self-ignoring-sqlite-store.md) · shipped in [#19](https://github.com/yashc17/polish/issues/19) |

---

## 6. Decision log

Settled by the design interview. Decisions taken *since*, while building, are
recorded as ADRs in `docs/adr/` and indexed from §5 — each one arrived by
resolving an open question against real code, which is why it has its own
document rather than a row here.

| # | Decision | Chosen |
|---|---|---|
| 1 | Ambition | Open-source product |
| 2 | Wedge | Trust — safe unattended |
| 3 | Safety model | Tiered by verifiability |
| 4 | Edit engine | LLM edits, harness verifies |
| 5 | Sandbox | Git worktree + isolated venv |
| 6 | Blast radius | Full sweep once, incremental after |
| 7 | Observer | Persistent daemon (kept) |
| 8 | Languages | Python + TypeScript |
| 9 | Implementation | Python core + Node sidecar |
| 10 | Auth & cost | Inherit Claude Code auth + hard budget cap |
| 11 | Review UX | Branch, one commit per change, `--pr` opt-in |
| 12 | Failure policy | One repair attempt, then drop |
| 13 | v1 scope | Full pipeline, Python only |
