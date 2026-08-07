"""Walking the import graph out from the files that changed.

The closure is two walks from the same seeds, in opposite directions, unioned:

* **Forward** — everything the changed files transitively import. This is SPEC
  §3.6's stranding case: delete the last call to a helper and the helper is now
  dead, three files down the import chain from the edit.
* **Reverse** — everything that transitively imports them. The other half:
  change a shared utility and the callers are what break.

**The two walks are separate, and never alternate.** A walk that could turn
around mid-path — up an edge, then down another, then up again — is the
undirected connected component, and in a Python package where `__init__.py`
imports its submodules that component is the entire package. Every incremental
run would then scope to the whole repo, correctly and uselessly. Going out from
the seeds in each direction once keeps the answer proportional to what changed.

The graph walked is the **union** of the previous run's edges and today's, which
is what `_ledger` keeps yesterday's imports for: the edge that stranded a
function is by definition an edge that no longer exists.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

Edges = Mapping[str, frozenset[str]]


def merge(previous: Edges, current: Edges) -> dict[str, frozenset[str]]:
    """Every import edge either graph has, keyed by the file that imports."""
    merged: dict[str, frozenset[str]] = {}
    for graph in (previous, current):
        for path, targets in graph.items():
            merged[path] = merged.get(path, frozenset()) | targets
    return merged


def closure(seeds: Iterable[str], edges: Edges) -> frozenset[str]:
    """`seeds`, everything they transitively import, and everything importing them."""
    settled = tuple(seeds)
    return frozenset(_reachable(settled, edges) | _reachable(settled, _invert(edges)))


def _reachable(seeds: Iterable[str], edges: Edges) -> set[str]:
    seen: set[str] = set()
    pending = list(seeds)
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        pending.extend(edges.get(path, ()))
    return seen


def _invert(edges: Edges) -> dict[str, frozenset[str]]:
    reverse: dict[str, set[str]] = {}
    for importer, targets in edges.items():
        for target in targets:
            reverse.setdefault(target, set()).add(importer)
    return {path: frozenset(importers) for path, importers in reverse.items()}
