# ADR 0003 — Observer state in a self-ignoring SQLite store, with history distrusted

**Status:** Accepted · 2026-08-06
**Context:** [#19](https://github.com/yashc17/polish/issues/19), resolving
[#6](https://github.com/yashc17/polish/issues/6) · SPEC §3.7, §5 Q6

## Context

SPEC §3.7 keeps the observer daemon, and is candid about the price: "an
always-on process users must install, trust, and debug — inside a product whose
pitch is safety. It must be trivially inspectable and killable."

The daemon holds two quite different things, and treating them the same is the
mistake this decision exists to avoid:

- **The code graph** — files, symbols, and first-party import edges. It feeds
  the dependency closure that decides a run's scope (§3.6).
- **The intra-day change history** — code written at 2pm and deleted at 5pm, the
  order changes happened in, which functions churned repeatedly. §3.7 says this,
  not speed, is the daemon's entire justification.

## Decision

### Format — SQLite in WAL mode

Stdlib, so no new dependency, and inspectable with one ubiquitous command —
which is how "trivially inspectable" is actually met rather than merely claimed:

```console
$ sqlite3 .polish/graph.db "select path, qualname, churn from symbols order by churn desc limit 10"
$ sqlite3 .polish/graph.db .schema
$ sqlite3 .polish/graph.db .dump      # → plain text, if you want it
```

The decisive property is **concurrency**: the daemon writes all day while
`polish run` reads at night. WAL gives a writer and readers without blocking.
Atomic transactions make torn writes structurally impossible, which hands us
most of crash recovery for free.

**Rejected — JSONL.** More literally readable, but unbounded on a churning repo,
unqueryable ("which functions churned?" means reparsing everything), and an
append interrupted mid-write leaves a torn line.

**Rejected — split SQLite + JSONL.** Each store would match its access pattern,
at the cost of two formats to inspect, bound, version-stamp and recover, inside
the component §3.7 already calls the spec's most awkward.

### Location — `.polish/` in-repo, ignoring itself

```
.polish/
  .gitignore     ← contains exactly: *
  graph.db       ← SQLite, WAL
  meta.json      ← schema version, pid, bounds, and what all this is
  daemon.pid     ← one integer, so `kill $(cat …)` needs no other tool
```

The directory carries its own `.gitignore` containing `*`, so it disappears from
`git status` **without Polish ever editing the user's `.gitignore`**. The same
trick `uv` uses for `.venv` — confirmed empirically while building the #9
corpus, where `.venv/.gitignore` turned out to be one of three independent
signals excluding that tree.

State sits next to the code it describes, so inspection is `ls .polish/` rather
than remembering a path; it is deleted with the repo, so there are no orphans and
no prune command; and it matches what every Python tool already does
(`.pytest_cache/`, `.ruff_cache/`, `.mypy_cache/` — all of which `polish scan`
already excludes by name).

On acceptance criterion 9, "the working tree is untouched throughout": that
criterion governs the night **run**. The daemon writes during the **day**. The
run itself still touches nothing.

**Rejected — XDG state dir.** Never writes to the user's tree at all, but
invisible without a lookup command, and orphans state when a repo is deleted.

**Rejected — appending to the user's `.gitignore`.** Edits a file the user owns
and has curated, unprompted, and shows up as a surprise in their next diff.

### The graph is verifiable; the history is not

This asymmetry decides the staleness rule, and it is the load-bearing part of
this ADR.

**Graph.** Every file carries an `(mtime, size)` fingerprint plus a content
digest, revalidated against the tree at run start. Drift cold-rebuilds those
files. A stale graph is **always recoverable**, because the tree is the source of
truth. The fingerprint is the cheap screen and the digest is the verdict, so a
rebuild or a checkout that restores identical bytes does not force a needless
re-parse.

**History.** Cannot be reconstructed. §3.7's whole justification is that it
captures what `git diff` erases. So the daemon records its **uptime windows**, and
any gap is reported as **UNKNOWN — never as "no churn"**.

That distinction is safety-critical. §3.7 gates dedup on churn ("a function
rewritten four times today is not a dedup candidate"). If a gap silently read as
"no churn", Polish would confidently dedup a function it should have left alone —
an unsafe edit produced by *absent* data rather than *wrong* data. So
churn-gated transforms are ineligible for code whose history falls in a gap, and
`Churn.count` **raises** rather than return a number, so that no caller can
mistake silence for evidence.

**Rejected — cold-rebuild on any doubt.** Simplest possible rule, but discards
the intra-day history the daemon exists to collect. A cold parse was always fast;
§3.7 says the daemon is justified by history, not speed. That option makes it
nearly pointless after any restart.

**Rejected — trust the daemon's own clean-shutdown flag.** Cheapest, but it
trusts the component being validated. A watcher that missed filesystem events
(inotify limits, editor atomic-rename, NFS) reports itself perfectly healthy.

### Scoping UNKNOWN, and the hole left in it

A gap does not blanket the whole repo. On restart the daemon reconciles against
the tree, and marks churn UNKNOWN for a file when it *changed across* the gap, or
first appeared during it. A file byte-identical on both sides keeps its history.
That is what makes the rule usable — "any gap poisons everything" would reproduce
the rejected cold-rebuild option by another route.

The residual hole, stated plainly: a file edited and then reverted to identical
bytes entirely inside a gap is indistinguishable from one nobody touched, and
keeps a churn count that is too low. Nothing on disk records that it happened.
Accepted as the cost of the scoping, and worth revisiting if churn gating turns
out to be sensitive to it.

### Version skew, and the untrustworthy store

The schema version is stamped in `PRAGMA user_version` and mirrored in
`meta.json`. On mismatch the store is **discarded and cold-rebuilt, never
migrated**: the graph is rebuildable from the tree in seconds, so a migration
would carry risk for no benefit.

A run can always declare the store untrustworthy — missing, version-skewed, or
unreadable — and fall back to a cold parse. `read()` never raises on a bad store;
it returns an observation that says which failure it hit, with a correct graph
built from the tree and every churn answer marked unknown.

### Size bounds

Intra-day history retained **14 days**; hard ceiling **100 MB**; eviction
oldest-first; compaction after each nightly run. The graph is proportional to
repo size and is not bounded — evicting it would trade a bounded disk cost for an
unbounded correctness one, since a run scopes itself by that graph. When the
ceiling is reached with no history left to evict, the store is allowed to stay
oversized rather than be silently truncated.

**These are starting numbers**, accepted without strong feelings and tunable once
there is real data from a churning repo. `compact()` takes both as parameters for
exactly that reason: a number nobody can move is a number nobody will ever
correct.

### Polling, not filesystem events

The daemon polls on a 2-second interval rather than subscribing to inotify /
FSEvents. Deliberate, and of a piece with the rejection of the clean-shutdown
flag above: watch limits, editors that save by atomic rename, and network
filesystems all drop events silently, and a watcher that believes it saw
everything is exactly what must not be trusted. Polling also costs no
dependency, and the fingerprint scan it performs is the same one used for
staleness detection.

The cost is real and worth naming: `Churn.count` is a **lower bound**, because
two edits inside one poll interval land as one. A churn gate should therefore be
written as "≥ N revisions means ineligible" rather than "< N means safe".

## Consequences

- `.polish/` appears in the user's repo. It hides itself from git, but it is
  still a directory they did not create, and `polish observe status` exists to
  answer "what is this?" in one command.
- Everything downstream of churn must handle `ChurnUnknown`. That is the point,
  but it does mean a chunk of API surface no consumer can ignore.
- Anyone shipping a schema change loses every user's history on upgrade. Accepted:
  the alternative is migration code for a store that is cheap to rebuild.
- The daemon runs as an ordinary child process with an ordinary pidfile, so
  `ps`, `kill` and `cat` are sufficient to operate it. `polish observe` is a
  convenience over them, never the only way in.
