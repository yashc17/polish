# ADR 0002 — Refuse non-git repositories, with an opt-in `--init-git`

**Status:** Accepted · 2026-08-06
**Context:** [#10](https://github.com/yashc17/polish/issues/10), resolving
[#1](https://github.com/yashc17/polish/issues/1) · SPEC §5 Q1

## Context

The safety envelope is a git worktree (SPEC §3.3) and the review output is a
branch with one commit per change (§3.9). Both need a repository. A meaningful
share of Polish's stated audience — experimental and scratch projects written
fast with AI assistance — works in directories that were never `git init`-ed,
so this cannot be deferred past the ticket that builds the envelope.

## Decision

`polish run` in a directory that is not a git repository **refuses and exits
non-zero**, printing the command that fixes it and the flag that fixes it
automatically:

```
$ polish run
polish: /Users/x/scratch is not a git repository.

Polish sandboxes itself in a git worktree, and the review output is a
branch with one commit per change. Neither is possible here.

  git init && git add -A && git commit -m "initial"

or let Polish do it for you:

  polish run --init-git
```

`--init-git` runs `git init` plus an initial commit in the user's directory —
**on explicit opt-in only, never automatically** — and then proceeds down the
ordinary path. There is one sandbox path and no degraded mode.

Two adjacent cases refuse on the same terms, because they fail for the same
reason — there is no `HEAD` to check a worktree out from, or the repository
being branched is not the tree being scanned:

- a repository with no commits yet (`--init-git` commits it);
- a subdirectory of a repository, which names the root to run against instead.

## Consequences

- Every v1 acceptance criterion holds unmodified, because there is only one
  path. No statement of degraded criteria is required.
- §4.9 — the working tree is untouched — holds. `--init-git` **adds** a
  `.git/` directory and modifies no source file; criterion 9 is about the tree
  Polish refactors. It is explicit opt-in and trivially undone with
  `rm -rf .git`. The default path writes nothing at all: Polish scans and
  refuses before it creates even its own workspace.
- The initial commit is made with the user's git identity, not Polish's. On a
  machine with no identity configured, `git commit` fails and its own message —
  which already names the fix — is what the user sees.

## Alternatives rejected

**Shadow-init: a repository somewhere else, pointed at the user's tree.** and
**a non-git fallback sandbox (copy the tree, diff it back).** Both fail on the
same two counts.

1. *§3.9's review UX degrades to a patch file.* Neither can produce a branch,
   because there is no repository for a branch to live in. For an agent that
   runs unattended overnight and may produce twenty changes, one patch is a
   materially worse review surface — and per-commit granularity is described in
   §3.9 as a trust feature, not a convenience: "a developer who distrusts one
   category can drop it without losing the night."
2. *Both add a second sandbox path.* In a product whose entire pitch is the
   safety envelope, a second, less-exercised safety path is the most expensive
   complexity available. It would need its own tests, its own failure modes and
   its own statement of which acceptance criteria survive — for the benefit of a
   case that one `git init` removes.

The stated cost of refusing was that part of the target audience hits a wall on
first run. `--init-git` removes the wall without building a second envelope: the
wall becomes one flag.

## Notes on the implementation

Two choices were made here rather than in #1, and neither is load-bearing enough
for an ADR of its own:

- **The workspace is `.polish/`, carrying its own `.gitignore` containing `*`.**
  Polish never edits the user's `.gitignore`; writing to a tracked file to make
  room for itself would show up as a diff in the review surface it is supposed
  to keep clean. A directory that ignores itself is the trick `uv` uses for
  `.venv`.
- **The snapshot copies the first-party file set, not the whole tree.** Those
  are the only files Polish will ever edit, so they are the only ones a recovery
  could need, and bounding the copy is what stops a repo with a large dataset
  beside it from costing that much disk every night.
