"""Module naming — what a first-party file is importable as, and from where.

Issue #30. The bug this file exists to keep fixed is a *missing* edge, which is
the kind a green suite hides: the code graph named files by their path, so on a
`src/` layout an absolute `import mypkg` resolved to nothing and the repository's
tests and its source sat in two disconnected halves of one graph. Nothing failed.
The scope was simply smaller than the repository deserved.

So the assertions here are mostly about names that must be *present*, and the
two that are about absence — the stub, the unnameable directory — are the cases
where being generous would make an edge point at the wrong file.

`test_the_probe_and_the_graph_agree_on_names` is the one that fails if a second
copy of this logic ever grows back inside either consumer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from polish import modules, probe
from polish.firstparty import scan

#: `psf/requests` in miniature — the shape #30 was found on. A package under
#: `src/`, and a suite beside it that imports it absolutely, from the repository
#: root. Neither half of the graph can reach the other unless `import mypkg`
#: names `src/mypkg/__init__.py`.
SRC_LAYOUT = {
    "pyproject.toml": '[project]\nname = "mypkg"\n',
    "src/mypkg/__init__.py": "from mypkg.core import go\n",
    "src/mypkg/core.py": "def go():\n    return 1\n",
    "tests/__init__.py": "",
    "tests/test_go.py": "import mypkg\n\n\ndef test_go():\n    assert mypkg.go()\n",
}


def index(root: Path) -> modules.Modules:
    return modules.index(root, scan(root).files)


# --- the name a file is actually importable by ----------------------------


def test_a_src_layout_package_is_named_the_way_it_is_imported(make_repo):
    repo = make_repo(SRC_LAYOUT)

    named = index(repo).by_name

    assert named["mypkg"] == "src/mypkg/__init__.py"
    assert named["mypkg.core"] == "src/mypkg/core.py"


def test_both_spellings_resolve_where_both_are_legitimate(make_repo):
    """`src.mypkg` is importable too, and indexing one must not drop the other.

    PEP 420 makes `src/` an implicit namespace package, so a file run from the
    repository root can legitimately write either spelling. The old index held
    only the path-derived one; holding only the interpreter-derived one would
    trade the blind spot for a new one rather than closing it.
    """
    repo = make_repo(SRC_LAYOUT)

    named = index(repo).by_name

    assert named["src.mypkg"] == "src/mypkg/__init__.py"
    assert named["mypkg"] == named["src.mypkg"]


def test_a_flat_layout_package_keeps_the_name_it_always_had(make_repo):
    repo = make_repo(
        {
            "pyproject.toml": '[project]\nname = "demo"\n',
            "demo/__init__.py": "",
            "demo/core.py": "",
        }
    )

    named = index(repo).by_name

    assert named["demo"] == "demo/__init__.py"
    assert named["demo.core"] == "demo/core.py"


def test_a_namespace_package_is_named_from_the_root_its_metadata_declares(make_repo):
    """No `__init__.py` means nothing for a walk to find — only the metadata knows.

    `where = ["src"]` is the repository saying `src` goes on `sys.path`, which
    is the only evidence that `mypkg.core` is the name this file answers to.
    """
    repo = make_repo(
        {
            "pyproject.toml": (
                '[project]\nname = "mypkg"\n'
                '[tool.setuptools.packages.find]\nwhere = ["src"]\n'
            ),
            "src/mypkg/core.py": "def go():\n    return 1\n",
        }
    )

    assert index(repo).by_name["mypkg.core"] == "src/mypkg/core.py"


def test_a_declared_package_directory_is_named_from_its_parent(make_repo):
    """Poetry declares the package, not the root above it. The tree says which.

    `packages = [{include = "demo", from = "src"}]` names `src/demo`, a package
    directory — so the root that reaches it is `src`, and reading the entry as a
    root would name the package's own contents `util` instead of `demo.util`.
    """
    repo = make_repo(
        {
            "pyproject.toml": (
                '[tool.poetry]\nname = "demo"\n'
                'packages = [{include = "demo", from = "src"}]\n'
            ),
            "src/demo/__init__.py": "",
            "src/demo/util.py": "",
        }
    )

    assert index(repo).by_name["demo.util"] == "src/demo/util.py"


def test_a_directory_no_import_could_name_is_dropped_for_that_root_alone(make_repo):
    """A hyphenated directory is unreachable from above and fine from inside."""
    repo = make_repo(
        {
            "pyproject.toml": '[project]\nname = "demo"\n',
            "my-scripts/tool.py": "",
        }
    )

    named = index(repo).by_name

    assert named["tool"] == "my-scripts/tool.py"
    assert not [name for name in named if "my-scripts" in name]


def test_an_implementation_outranks_its_own_stub(make_repo):
    """A `.py` and a `.pyi` are one module, and the edge wants the one that runs."""
    repo = make_repo(
        {
            "pyproject.toml": '[project]\nname = "demo"\n',
            "demo/__init__.py": "",
            "demo/core.py": "",
            "demo/core.pyi": "",
        }
    )

    assert index(repo).by_name["demo.core"] == "demo/core.py"


# --- the address: one name per file, for anything that executes an import --


def test_the_address_is_the_name_the_interpreter_reaches_on_its_own(make_repo):
    repo = make_repo(SRC_LAYOUT)

    addressed = {module.path: module for module in index(repo).addressed}

    assert addressed["src/mypkg/core.py"].name == "mypkg.core"
    assert addressed["src/mypkg/core.py"].root == repo / "src"


def test_a_file_is_addressed_once_however_many_names_it_has(make_repo):
    """Gate 3 demotes on failure, so a second spelling can only ever cost a tier."""
    repo = make_repo(SRC_LAYOUT)

    found = index(repo)
    paths = [module.path for module in found.addressed]

    assert sorted(paths) == sorted(set(paths))
    assert len(found.names("src/mypkg/__init__.py")) > 1


def test_the_probe_and_the_graph_agree_on_names(make_repo):
    """One implementation, asserted from both sides (#30, ADR 0009).

    The probe narrows — it drops the suite and anything that runs on import —
    but every name it keeps has to be a name the graph would resolve to the same
    file. If it is not, a repository is addressed one way for execution and
    another way for its graph, which is the state #30 found.
    """
    repo = make_repo(SRC_LAYOUT)
    files = scan(repo).files

    named = modules.index(repo, files).by_name
    imported = probe.modules(repo, files)

    assert [module.name for module in imported] == ["mypkg", "mypkg.core"]
    for module in imported:
        assert named[module.name] == module.path


def test_a_name_is_claimed_by_one_file_and_the_loser_keeps_its_others(make_repo):
    """Two files can make the same claim; the answer still has to be one path."""
    repo = make_repo(
        {
            "pyproject.toml": '[project]\nname = "demo"\n',
            "scripts/build.py": "",
            "tools/build.py": "",
        }
    )

    named = index(repo).by_name

    assert named["build"] in {"scripts/build.py", "tools/build.py"}
    assert named["scripts.build"] == "scripts/build.py"
    assert named["tools.build"] == "tools/build.py"


def test_import_roots_are_the_directories_that_reach_what_was_addressed(make_repo):
    repo = make_repo(SRC_LAYOUT)

    assert modules.roots(index(repo).addressed) == (repo / "src", repo)


@pytest.mark.parametrize("relative", ["README.md", "docs/conf.txt", "Makefile"])
def test_a_file_that_is_not_python_is_not_a_module(make_repo, relative):
    repo = make_repo({"pyproject.toml": '[project]\nname = "demo"\n', relative: ""})

    assert modules.index(repo, [relative]).found == ()
