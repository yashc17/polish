"""Behaviour of the first-party file set, tested through `scan()`."""

from __future__ import annotations

from pathlib import Path

import pytest

from polish.firstparty import NotAPythonProject, scan


def test_returns_python_source_in_a_plain_directory(make_repo):
    repo = make_repo(
        {
            "pyproject.toml": '[project]\nname = "demo"\n',
            "demo.py": "x = 1\n",
            "README.md": "# demo\n",
        }
    )

    assert scan(repo).files == ("demo.py",)


def test_membership_accepts_a_path_or_a_string(make_repo):
    repo = make_repo(
        {"pyproject.toml": '[project]\nname = "demo"\n', "demo/core.py": ""}
    )

    result = scan(repo)

    assert "demo/core.py" in result
    assert Path("demo/core.py") in result
    assert "demo/missing.py" not in result
    assert len(result) == 1
    assert list(result) == ["demo/core.py"]


def test_excludes_a_virtualenv_identified_by_its_marker_file(make_repo):
    repo = make_repo(
        {
            "pyproject.toml": '[project]\nname = "demo"\n',
            "train.py": "",
            # A real virtualenv, whatever it is named, carries pyvenv.cfg.
            "myenv/pyvenv.cfg": "home = /usr/bin\n",
            "myenv/lib/python3.12/site-packages/torch/__init__.py": "",
            "myenv/lib/python3.12/site-packages/numpy/core.py": "",
        }
    )

    assert scan(repo).files == ("train.py",)


def test_excludes_a_virtualenv_when_the_marker_is_the_only_evidence(make_repo):
    # The env above is also caught by `site-packages`, so it cannot show that
    # the marker works. Here nothing else fires: the directory has an innocuous
    # name, no site-packages on the path to the file, and no ignore rule. Real
    # environments do contain this file — virtualenv and uv both write it.
    repo = make_repo(
        {
            "pyproject.toml": '[project]\nname = "demo"\n',
            "train.py": "",
            "myenv/pyvenv.cfg": "home = /usr/bin\n",
            "myenv/bin/activate_this.py": "",
        }
    )

    assert scan(repo).files == ("train.py",)


def test_excludes_directories_whose_name_is_unambiguous_evidence(make_repo):
    repo = make_repo(
        {
            "pyproject.toml": '[project]\nname = "demo"\n',
            "demo/__init__.py": "",
            # A conda env has no pyvenv.cfg, but site-packages is decisive.
            "conda/lib/python3.12/site-packages/pandas/__init__.py": "",
            "node_modules/leftpad/shim.py": "",
            ".tox/py311/lib/python3.11/site-packages/pytest/__init__.py": "",
            "demo/__pycache__/cached.py": "",
            ".mypy_cache/3.12/demo.py": "",
        }
    )

    assert scan(repo).files == ("demo/__init__.py",)


def test_excludes_vendored_code_even_when_it_is_committed(make_repo):
    # pip ships its dependencies inside its own package. They are checked in and
    # git-tracked, so only the directory name gives them away.
    repo = make_repo(
        {
            "pyproject.toml": '[project]\nname = "pip"\n',
            "src/pip/__init__.py": "",
            "src/pip/_vendor/__init__.py": "",
            "src/pip/_vendor/requests/api.py": "",
            "third_party/six.py": "",
        }
    )

    assert scan(repo).files == ("src/pip/__init__.py",)


def test_excludes_build_artifacts_at_the_repo_root_only(make_repo):
    # `build/` at the root is setuptools output. `demo/build/` is a plausible
    # name for a real subpackage, so the exclusion is anchored to the root.
    repo = make_repo(
        {
            "pyproject.toml": '[project]\nname = "demo"\n',
            "demo/__init__.py": "",
            "demo/build/planner.py": "",
            "build/lib/demo/__init__.py": "",
            "dist/demo-0.1.0/setup.py": "",
            "demo.egg-info/generated.py": "",
        }
    )

    assert scan(repo).files == ("demo/__init__.py", "demo/build/planner.py")


def test_respects_gitignore_including_nested_and_negated_rules(make_repo):
    repo = make_repo(
        {
            "pyproject.toml": '[project]\nname = "demo"\n',
            ".gitignore": "generated/\n*.gen.py\nscratch/\n",
            "demo/__init__.py": "",
            "demo/schema.gen.py": "",
            "generated/models.py": "",
            "scratch/.gitignore": "!keep.py\n",
            "scratch/throwaway.py": "",
            ".git/info/exclude": "secret.py\n",
            "secret.py": "",
        }
    )

    assert scan(repo).files == ("demo/__init__.py",)


def test_gitignore_in_a_subdirectory_applies_from_that_directory_down(make_repo):
    repo = make_repo(
        {
            "pyproject.toml": '[project]\nname = "demo"\n',
            "demo/__init__.py": "",
            "demo/.gitignore": "local_*.py\n",
            "demo/local_settings.py": "",
            # Same name higher up the tree — the nested rule must not reach it.
            "local_settings.py": "",
        }
    )

    assert scan(repo).files == ("demo/__init__.py", "local_settings.py")


def test_declared_packages_survive_an_overbroad_gitignore(make_repo):
    # A `lib/` line meant for build output also swallows the real package. The
    # metadata declares it, and metadata is the more authoritative layer.
    repo = make_repo(
        {
            "pyproject.toml": (
                "[project]\nname = 'demo'\n"
                "[tool.setuptools]\npackage-dir = {'' = 'lib'}\n"
            ),
            ".gitignore": "lib/\n",
            "lib/demo/__init__.py": "",
            "lib/demo/core.py": "",
        }
    )

    assert scan(repo).files == ("lib/demo/__init__.py", "lib/demo/core.py")


def test_declared_packages_do_not_rescue_vendored_or_environment_code(make_repo):
    # pip declares `src/pip` as its package, and `src/pip/_vendor` sits inside
    # it. Markers outrank metadata: a declared package containing a virtualenv
    # or a vendor drop is a packaging accident, not an instruction.
    repo = make_repo(
        {
            "pyproject.toml": (
                "[project]\nname = 'pip'\n"
                "[tool.setuptools.packages.find]\nwhere = ['src']\n"
            ),
            "src/pip/__init__.py": "",
            "src/pip/_vendor/requests/api.py": "",
            "src/pip/.venv/pyvenv.cfg": "home = /usr/bin\n",
            "src/pip/.venv/lib/python3.12/site-packages/click/core.py": "",
        }
    )

    assert scan(repo).files == ("src/pip/__init__.py",)


def test_reads_poetry_and_setup_cfg_package_declarations(make_repo):
    poetry = make_repo(
        {
            "pyproject.toml": (
                "[tool.poetry]\nname = 'demo'\n"
                "packages = [{ include = 'demo', from = 'lib' }]\n"
            ),
            ".gitignore": "lib/\n",
            "lib/demo/__init__.py": "",
        }
    )
    assert scan(poetry).files == ("lib/demo/__init__.py",)

    cfg = make_repo(
        {
            "setup.py": "from setuptools import setup\nsetup()\n",
            "setup.cfg": "[options]\npackage_dir =\n    = source\n",
            ".gitignore": "source/\n",
            "source/demo/__init__.py": "",
        }
    )
    # setup.py is the repo's own code too, so it comes back with the package.
    assert scan(cfg).files == ("setup.py", "source/demo/__init__.py")


def test_setup_cfg_declarations_survive_a_percent_sign(make_repo):
    # setup.cfg is read with configparser, whose default interpolation treats
    # `%` as a directive. A repo with a literal `%` in any value would raise,
    # and the declared roots would be silently lost.
    repo = make_repo(
        {
            "setup.cfg": (
                "[metadata]\n"
                "description = 100% typed\n"
                "[options.packages.find]\n"
                "where = source\n"
            ),
            ".gitignore": "source/\n",
            "source/demo/__init__.py": "",
        }
    )

    assert scan(repo).files == ("source/demo/__init__.py",)


def test_reads_a_plain_setuptools_package_list(make_repo):
    repo = make_repo(
        {
            "pyproject.toml": (
                "[project]\nname = 'demo'\n[tool.setuptools]\npackages = ['demo']\n"
            ),
            ".gitignore": "demo/\n",
            "demo/__init__.py": "",
        }
    )

    assert scan(repo).files == ("demo/__init__.py",)


def test_config_can_remove_paths_the_layers_included(make_repo):
    repo = make_repo(
        {
            "pyproject.toml": '[project]\nname = "demo"\n',
            "polish.toml": '[scan]\nexclude = ["demo/migrations/**"]\n',
            "demo/__init__.py": "",
            "demo/migrations/0001_initial.py": "",
        }
    )

    assert scan(repo).files == ("demo/__init__.py",)


def test_config_include_overrides_every_other_layer(make_repo):
    # The escape hatch has to be able to beat a marker, or a user whose real
    # package is called `third_party` has no way to disagree with Polish.
    repo = make_repo(
        {
            "pyproject.toml": '[project]\nname = "demo"\n',
            "polish.toml": '[scan]\ninclude = ["third_party/ours/**"]\n',
            "demo/__init__.py": "",
            "third_party/ours/patched.py": "",
            "third_party/upstream/lib.py": "",
        }
    )

    assert scan(repo).files == ("demo/__init__.py", "third_party/ours/patched.py")


def test_config_include_wins_over_config_exclude(make_repo):
    repo = make_repo(
        {
            "pyproject.toml": '[project]\nname = "demo"\n',
            "polish.toml": (
                '[scan]\nexclude = ["demo/**"]\ninclude = ["demo/keep_me.py"]\n'
            ),
            "demo/__init__.py": "",
            "demo/keep_me.py": "",
        }
    )

    assert scan(repo).files == ("demo/keep_me.py",)


def test_excludes_generated_files_by_name_and_by_header(make_repo):
    repo = make_repo(
        {
            "pyproject.toml": '[project]\nname = "demo"\n',
            "demo/__init__.py": "",
            "demo/service_pb2.py": "",
            "demo/service_pb2_grpc.py": "",
            "demo/schema.py": "# @generated by datamodel-codegen\n\nclass A: ...\n",
            "demo/parser.py": "# Generated by Lark. DO NOT EDIT.\n\nRULES = {}\n",
            # The same words far enough down the file are prose, not a banner.
            "demo/docs.py": "\n" * 40 + "# DO NOT EDIT this by hand, ask first\n",
        }
    )

    assert scan(repo).files == ("demo/__init__.py", "demo/docs.py")


def test_rejects_a_directory_that_is_not_a_python_project(make_repo):
    repo = make_repo({"index.js": "", "package.json": "{}\n"})

    with pytest.raises(NotAPythonProject) as raised:
        scan(repo)

    assert "not a Python project" in str(raised.value)


def test_rejects_a_missing_directory(tmp_path):
    with pytest.raises(NotAPythonProject) as raised:
        scan(tmp_path / "nowhere")

    assert "does not exist" in str(raised.value)


def test_rejects_a_repo_whose_python_is_entirely_excluded(make_repo):
    # The 99.6%-virtualenv case with the last 0.4% missing. Returning an empty
    # set would look like success; it is the one result most worth shouting
    # about, because it means every candidate was thrown away.
    repo = make_repo(
        {
            "pyproject.toml": '[project]\nname = "demo"\n',
            ".venv/pyvenv.cfg": "home = /usr/bin\n",
            ".venv/lib/python3.12/site-packages/torch/__init__.py": "",
            ".venv/lib/python3.12/site-packages/torch/nn.py": "",
        }
    )

    with pytest.raises(NotAPythonProject) as raised:
        scan(repo)

    message = str(raised.value)
    assert "2 Python files" in message
    assert "polish.toml" in message


def test_accepts_a_project_that_simply_has_no_source_yet(make_repo):
    repo = make_repo({"pyproject.toml": '[project]\nname = "demo"\n'})

    assert scan(repo).files == ()
