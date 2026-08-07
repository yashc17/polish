"""The command contract for `polish scan` and `polish run`.

stdout, stderr and exit code — the whole surface an unattended scheduler sees.
"""

from __future__ import annotations

from datetime import date

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


def test_run_reports_the_snapshot_sandbox_and_branch_it_produced(make_git_repo, capsys):
    repo = make_git_repo(
        {"pyproject.toml": '[project]\nname = "demo"\n', "demo.py": "x = 1\n"}
    )
    today = date.today().isoformat()

    code = main(["run", str(repo)])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.err == ""
    reported = dict(line.split(maxsplit=1) for line in captured.out.splitlines())
    assert reported["run"] == today
    assert reported["snapshot"] == f".polish/snapshots/raw/{today} (1 file)"
    assert reported["sandbox"] == f".polish/runs/{today} (removed)"
    assert reported["branch"] == f"polish/{today}"
    assert reported["changes"].startswith("0 ")


def test_run_exits_non_zero_with_an_actionable_message_outside_a_repository(
    make_repo, capsys
):
    repo = make_repo({"pyproject.toml": '[project]\nname = "demo"\n', "demo.py": ""})

    code = main(["run", str(repo)])
    captured = capsys.readouterr()

    assert code == 1
    assert captured.out == ""
    assert "not a git repository" in captured.err
    assert "polish run --init-git" in captured.err
