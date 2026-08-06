# ADR 0001 — Marker evidence outranks packaging metadata

**Status:** Accepted · 2026-08-06
**Context:** [#9](https://github.com/yashc17/polish/issues/9), resolving
[#2](https://github.com/yashc17/polish/issues/2) · SPEC §5 Q2

## Context

Issue #9 specifies the resolution order for the first-party file set as
"packaging metadata, then a gitignore-aware walk, then environment marker files,
then the explicit config override" — most authoritative first.

Taken literally as a precedence chain, that puts packaging metadata above
environment markers. Two real repositories in the corpus break under that
reading:

- **pip** declares `src/pip` as its package via
  `[tool.setuptools.packages.find] where = ["src"]`. `src/pip/_vendor/` — 256
  files of other people's released code — sits inside the declared package.
- A virtualenv created *inside* a declared package directory (common when
  someone runs `python -m venv .venv` from the wrong directory) would be
  declared first-party by the same reasoning.

Metadata describes what goes into a wheel. It is not, and was never meant to
be, a claim of authorship over everything beneath the declared root.

## Decision

Layers are applied as **successive refinement**, not first-match-wins, with this
precedence per path:

1. **Environment / vendor / build markers** prune first and cannot be overruled
   by metadata. A `pyvenv.cfg` beneath a declared package is a packaging
   accident, not an instruction.
2. **Packaging metadata** rescues declared roots from the ignore layer. This is
   where "metadata is most authoritative" actually bites: an overbroad
   `.gitignore` line such as `lib/` does not remove a package the repo declares.
3. **The gitignore-aware walk** enumerates everything else.
4. **`polish.toml`** applies last and beats all three, including markers.

So #9's ordering holds for the metadata-versus-ignore conflict it was written
about, and inverts for metadata-versus-markers.

## Consequences

- A user whose genuine package is named `third_party`, `_vendor` or similar is
  wrong-footed by layer 1 and must say so in `polish.toml`. Accepted: that name
  collision is rare, and the failure is visible and correctable, whereas
  silently refactoring `site-packages` is neither.
- `polish.toml` `include` must be able to reach into a subtree the walk pruned,
  which costs a second traversal — paid only by repos that use `include`.
- Precision is protected at the cost of recall in the ambiguous cases. This
  matches the agreed floor: precision 1.00, recall ≥ 0.95.

## Alternatives rejected

**Literal first-match-wins in #9's order.** Would classify all 256 of pip's
vendored files as first-party — the exact failure SPEC §5 Q2 was written to
prevent.

**Trust metadata only for confirmation, never for rescue.** Simpler, but loses
the overbroad-`.gitignore` case, and leaves the "most authoritative" layer doing
no work at all.
