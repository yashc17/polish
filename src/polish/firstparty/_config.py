"""Layer 4 — the explicit config override.

Applied last and unconditionally, because it is the user's escape hatch. Every
other layer is inference; this one is a statement. A user whose real package
happens to be called `third_party` has no other way to disagree with Polish, and
a trust-first product that cannot be corrected is not trustworthy.

`polish.toml` at the repo root:

    [scan]
    exclude = ["demo/migrations/**"]
    include = ["third_party/ours/**"]
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from pathspec import PathSpec

CONFIG_FILENAME = "polish.toml"


@dataclass(frozen=True)
class Overrides:
    include: PathSpec | None = None
    exclude: PathSpec | None = None

    @property
    def has_include(self) -> bool:
        return self.include is not None

    def includes(self, relative_path: str) -> bool:
        return self.include is not None and self.include.match_file(relative_path)

    def excludes(self, relative_path: str) -> bool:
        return self.exclude is not None and self.exclude.match_file(relative_path)

    def apply(self, files: set[str], resurrected: set[str]) -> set[str]:
        """Drop what `exclude` names, then add back everything `include` names."""
        kept = {path for path in files if not self.excludes(path)}
        return kept | resurrected


def load(root: Path) -> Overrides:
    section = _read_scan_section(root / CONFIG_FILENAME)
    return Overrides(
        include=_compile(section.get("include")),
        exclude=_compile(section.get("exclude")),
    )


def _read_scan_section(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    section = data.get("scan", {})
    return section if isinstance(section, dict) else {}


def _compile(patterns) -> PathSpec | None:
    if not isinstance(patterns, list):
        return None
    lines = [str(pattern) for pattern in patterns if str(pattern).strip()]
    return PathSpec.from_lines("gitignore", lines) if lines else None
