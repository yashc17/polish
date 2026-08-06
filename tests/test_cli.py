"""The `polish scan` command contract: stdout, stderr, and exit code."""

from __future__ import annotations

from polish.cli import main


def test_prints_one_path_per_line_and_nothing_else(make_repo, capsys):
    repo = make_repo(
        {
            "pyproject.toml": '[project]\nname = "demo"\n',
            "demo/__init__.py": "",
            "demo/core.py": "",
            "README.md": "# demo\n",
            ".venv/pyvenv.cfg": "home = /usr/bin\n",
            ".venv/lib/python3.12/site-packages/torch/__init__.py": "",
        }
    )

    code = main(["scan", str(repo)])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out == "demo/__init__.py\ndemo/core.py\n"
    assert captured.err == ""


def test_defaults_to_the_current_directory(make_repo, capsys, monkeypatch):
    repo = make_repo({"pyproject.toml": '[project]\nname = "demo"\n', "demo.py": ""})
    monkeypatch.chdir(repo)

    code = main(["scan"])

    assert code == 0
    assert capsys.readouterr().out == "demo.py\n"


def test_exits_non_zero_with_an_actionable_message_on_a_non_python_directory(
    make_repo, capsys
):
    repo = make_repo({"index.js": "", "package.json": "{}\n"})

    code = main(["scan", str(repo)])
    captured = capsys.readouterr()

    assert code == 1
    assert captured.out == ""
    assert "not a Python project" in captured.err
    assert "Run polish from the root" in captured.err


def test_exits_non_zero_when_every_python_file_was_excluded(make_repo, capsys):
    repo = make_repo(
        {
            "pyproject.toml": '[project]\nname = "demo"\n',
            ".venv/pyvenv.cfg": "home = /usr/bin\n",
            ".venv/lib/python3.12/site-packages/torch/__init__.py": "",
        }
    )

    code = main(["scan", str(repo)])
    captured = capsys.readouterr()

    assert code == 1
    assert captured.out == ""
    assert "polish.toml" in captured.err
