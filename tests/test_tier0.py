"""Tier 0's contract: what it removes, what it refuses, and what it proves.

The refusals matter more than the removals here. Tier 0's claim is "provably
behaviour-preserving" (SPEC §3.1), and every case below that ends in an import
being *left alone* is a case where the claim would otherwise have been a
guess. Skipping a removable import costs nothing; removing a load-bearing one
costs the product.

Each `refactor()` call runs the type checker at least twice, so the rules are
exercised against one repository built to contain a case for each, and
interrogated from several angles — the same shape `test_envelope.py` uses.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from conftest import materialise
from polish import tier0
from polish.firstparty import scan

#: Spelled in two pieces so that ruff reads the fixture below as data rather
#: than as a directive suppressing its own diagnostics on this file.
NOQA = "# " + "noqa"

#: A repository with one file per rule. Nothing here has a test suite, which is
#: Tier 0's whole reason for existing (SPEC §3.1).
RULES: Mapping[str, str] = {
    "pyproject.toml": '[project]\nname = "demo"\n',
    # A package `__init__` is a re-export surface whose consumers are other
    # people's repositories. Never touched, however unused a name looks.
    "demo/__init__.py": "from demo.inert import KEEP\n",
    # Provably inert: literals and an undecorated def, so importing runs nothing.
    "demo/inert.py": (
        'KEEP = 1\nNAMES = ["a", "b"]\n\n\ndef helper():\n    return KEEP\n'
    ),
    # Not inert: importing it registers a handler, which is exactly the side
    # effect an unused-import remover must not silently undo.
    "demo/registry.py": (
        "HANDLERS = {}\n\n\ndef register(name):\n"
        '    HANDLERS[name] = 1\n\n\nregister("boot")\n'
    ),
    # The file that carries one removal and every reason for a refusal.
    "demo/mixed.py": (
        '"""Mixed."""\n\n'
        "import json\n"
        "import numpy as np\n"
        "import string\n"
        "from demo import registry\n"
        "from demo.inert import KEEP\n\n"
        'string = "shadowed"\n\n\n'
        "def go():\n"
        "    return KEEP\n"
    ),
    # Every import here is guarded, deferred or special. None is removable.
    "demo/guards.py": (
        "from __future__ import annotations\n\n"
        "from typing import TYPE_CHECKING\n\n"
        "from demo.inert import *\n\n"
        "if TYPE_CHECKING:\n"
        "    import decimal\n\n"
        "try:\n"
        "    import tomllib\n"
        "except ImportError:\n"
        "    tomllib = None\n\n\n"
        "def load():\n"
        "    import base64\n\n"
        "    return base64\n"
    ),
    # Reached by string, so no reading of the file can say what it uses.
    "demo/dynamic.py": (
        "import json\n\n\ndef fetch(name):\n    return globals()[name]\n"
    ),
    # Used only from a string annotation and from `__all__`.
    "demo/strings.py": (
        "import decimal\n\n"
        '__all__ = ["render"]\n\n\n'
        'def render(value: "decimal.Decimal") -> str:\n'
        "    return str(value)\n"
    ),
    # Both shapes below are taken verbatim from `requests`, which #11 removed
    # them from on its first dogfood run. `encodings.idna` is imported for the
    # codec it registers; the directive is the maintainer saying so.
    "demo/deliberate.py": (
        f"import encodings.idna  {NOQA}: F401\n"
        f"import socket  {NOQA}\n"
        "import string as string\n\n"
        "VALUE = 1\n"
    ),
    # A comment above the import goes with the import; a shebang does not.
    "demo/script.py": (
        "#!/usr/bin/env python\n"
        '"""A tool."""\n\n'
        "import os\n"
        "# belongs to nothing in particular\n"
        "import json\n\n"
        "VALUE = os.sep\n"
    ),
}


@dataclass(frozen=True)
class Pass:
    root: Path
    changes: tuple[tier0.Change, ...]

    def for_path(self, path: str) -> tier0.Change:
        return next(change for change in self.changes if change.path == path)

    def source(self, path: str) -> str:
        return (self.root / path).read_text()


@pytest.fixture(scope="module")
def rules(tmp_path_factory) -> Pass:
    root = materialise(tmp_path_factory.mktemp("rules"), RULES)
    return Pass(root=root, changes=tuple(tier0.refactor(root, scan(root).files)))


def untouched(rules: Pass, path: str) -> bool:
    """The file is unchanged *and* was never a candidate.

    Both halves are needed. Checking the file alone is not enough: a rule that
    stops holding lets the file through to the AST-equivalence proof and the
    type check, and either of those may well reject it — leaving the file
    identical and the broken rule invisible. Mutation testing found exactly
    that masking on three of these rules.
    """
    return rules.source(path) == RULES[path] and path not in [
        change.path for change in rules.changes
    ]


def test_it_removes_an_unused_import_of_an_inert_module(rules):
    change = rules.for_path("demo/mixed.py")

    assert change.kept
    assert change.removed == ("json",)
    assert "import json\n" not in rules.source("demo/mixed.py")


def test_it_leaves_a_third_party_import_alone_and_says_why(rules):
    reasons = dict(rules.for_path("demo/mixed.py").left_alone)

    assert "import numpy as np\n" in rules.source("demo/mixed.py")
    assert "neither first-party nor standard library" in reasons["np"]
    assert "without executing the repository" in reasons["np"]


def test_it_leaves_an_import_of_a_module_that_runs_code_alone(rules):
    reasons = dict(rules.for_path("demo/mixed.py").left_alone)

    assert "from demo import registry\n" in rules.source("demo/mixed.py")
    assert reasons["registry"] == (
        "importing `demo.registry` runs code, so removing it could change behaviour"
    )


def test_it_leaves_a_name_that_is_bound_twice_alone(rules):
    reasons = dict(rules.for_path("demo/mixed.py").left_alone)

    assert "import string\n" in rules.source("demo/mixed.py")
    assert reasons["string"] == "`string` is bound more than once in this file"


def test_it_never_touches_a_package_init(rules):
    assert rules.source("demo/__init__.py") == RULES["demo/__init__.py"]
    assert not [
        change for change in rules.changes if change.path.endswith("__init__.py")
    ]


def test_it_leaves_guarded_deferred_and_star_imports_alone(rules):
    assert untouched(rules, "demo/guards.py")


def test_it_leaves_a_file_it_cannot_read_statically_alone(rules):
    assert untouched(rules, "demo/dynamic.py")


def test_it_counts_string_annotations_and_dunder_all_as_uses(rules):
    assert untouched(rules, "demo/strings.py")


def test_it_defers_to_an_author_who_marked_the_import_deliberate(rules):
    """The `requests` regression: `# noqa` and `import x as x` outrank any rule.

    Found by running `polish run` against `psf/requests`, where both of the
    two changes it produced were wrong for this reason. No unit test would
    have found it, because every rule Polish had was working correctly.
    """
    assert untouched(rules, "demo/deliberate.py")


def test_a_stdlib_module_that_registers_a_codec_is_not_inert(make_repo):
    root = make_repo(
        {
            "pyproject.toml": '[project]\nname = "demo"\n',
            "demo.py": "import encodings.idna\n\nVALUE = 1\n",
        }
    )

    assert list(tier0.refactor(root, scan(root).files)) == []


def test_it_keeps_the_comments_and_layout_around_what_it_removes(rules):
    assert rules.source("demo/script.py") == (
        "#!/usr/bin/env python\n"
        '"""A tool."""\n\n'
        "import os\n"
        "# belongs to nothing in particular\n\n"
        "VALUE = os.sep\n"
    )


def test_every_kept_change_carries_its_evidence(rules):
    for change in rules.changes:
        if not change.kept:
            continue
        message = change.message()
        assert "Tier: 0 (deterministic codemod, LibCST)" in message
        assert "AST-equivalence: proven" in message
        assert "Type check: clean" in message


def test_it_changes_one_file_per_change(rules):
    paths = [change.path for change in rules.changes]

    assert sorted(paths) == sorted(set(paths))


#: Removing `json` from `a.py` is invisibly wrong: `b.py` imports the name
#: back out of it. Nothing that reads `a.py` alone can see that, which is why
#: the type check runs over the whole file set rather than the edited file.
RE_EXPORT = {
    "pyproject.toml": '[project]\nname = "demo"\n',
    "demo/__init__.py": "",
    "demo/a.py": "import json\n\n\ndef go():\n    return 1\n",
    "demo/b.py": (
        "from demo.a import json\n\n\ndef dump(x):\n    return json.dumps(x)\n"
    ),
}


def test_a_removal_that_breaks_another_file_is_dropped(make_repo):
    root = make_repo(RE_EXPORT)

    changes = list(tier0.refactor(root, scan(root).files))

    assert [change.path for change in changes] == ["demo/a.py"]
    assert not changes[0].kept
    assert "the type check reported 1 new diagnostic" in changes[0].note()
    assert 'has no attribute "json"' in changes[0].note()


def test_a_dropped_change_leaves_the_file_exactly_as_it_was(make_repo):
    root = make_repo(RE_EXPORT)

    list(tier0.refactor(root, scan(root).files))

    assert (root / "demo/a.py").read_text() == RE_EXPORT["demo/a.py"]


def test_nothing_is_edited_when_the_type_check_cannot_run(make_repo):
    root = make_repo(RE_EXPORT)
    stale = (*scan(root).files, "demo/deleted_underneath_us.py")

    with pytest.raises(tier0.Unverifiable) as declined:
        list(tier0.refactor(root, stale))

    assert "mypy" in str(declined.value)
    assert (root / "demo/a.py").read_text() == RE_EXPORT["demo/a.py"]


def test_it_removes_one_alias_and_leaves_its_neighbours(make_repo):
    root = make_repo(
        {
            "pyproject.toml": '[project]\nname = "demo"\n',
            "demo/core.py": "import os, json, sys\n\nVALUE = os.sep + sys.platform\n",
        }
    )

    changes = list(tier0.refactor(root, scan(root).files))

    assert changes[0].kept and changes[0].removed == ("json",)
    assert (root / "demo/core.py").read_text() == (
        "import os, sys\n\nVALUE = os.sep + sys.platform\n"
    )


def test_it_keeps_the_line_endings_the_file_had(make_repo):
    root = make_repo({"pyproject.toml": '[project]\nname = "demo"\n'})
    (root / "demo.py").write_bytes(
        b"import json\r\nimport os\r\n\r\nVALUE = os.sep\r\n"
    )

    changes = list(tier0.refactor(root, ("demo.py",)))

    assert changes[0].kept
    # Rewriting CRLF as LF would pass the proof — line endings are not
    # behaviour — while turning a one-line removal into a whole-file diff.
    assert (root / "demo.py").read_bytes() == b"import os\r\n\r\nVALUE = os.sep\r\n"


def test_it_finds_nothing_to_do_in_a_tidy_repository(make_repo):
    root = make_repo(
        {
            "pyproject.toml": '[project]\nname = "demo"\n',
            "demo/core.py": "import os\n\nVALUE = os.sep\n",
        }
    )

    assert list(tier0.refactor(root, scan(root).files)) == []


BEFORE = '"""Doc."""\n\nimport json\nimport os\n\nVALUE = os.sep\n'


def test_the_proof_holds_for_the_removal_it_describes():
    after = '"""Doc."""\n\nimport os\n\nVALUE = os.sep\n'

    proof = tier0.prove(BEFORE, after, removed=["json"])

    assert proof.holds
    assert proof.reason == "the diff removes `json` and nothing else"


def test_the_proof_ignores_comments_and_blank_lines():
    after = '"""Doc."""\n# a new comment\nimport os\n\n\n\nVALUE = os.sep\n'

    assert tier0.prove(BEFORE, after, removed=["json"]).holds


def test_the_proof_rejects_an_edit_that_touched_anything_else():
    mangled = '"""Doc."""\n\nimport os\n\nVALUE = os.sep + "!"\n'

    proof = tier0.prove(BEFORE, mangled, removed=["json"])

    assert not proof.holds
    assert proof.reason == "the edit changed something other than those imports"


def test_the_proof_rejects_an_edit_that_reordered_the_surviving_imports():
    reordered = '"""Doc."""\n\nVALUE = os.sep\nimport os\n'

    assert not tier0.prove(BEFORE, reordered, removed=["json"]).holds


def test_the_proof_rejects_an_edit_that_removed_more_than_it_claimed():
    stripped = '"""Doc."""\n\nVALUE = os.sep\n'

    assert not tier0.prove(BEFORE, stripped, removed=["json"]).holds


def test_the_proof_rejects_a_binding_the_original_never_had():
    proof = tier0.prove(BEFORE, BEFORE, removed=["pathlib"])

    assert not proof.holds
    assert "nothing at module level binds `pathlib`" in proof.reason


def test_the_proof_rejects_an_edit_that_does_not_parse():
    broken = '"""Doc."""\n\nimport os\n\nVALUE = os.\n'

    proof = tier0.prove(BEFORE, broken, removed=["json"])

    assert not proof.holds
    assert "the edited file does not parse" in proof.reason
