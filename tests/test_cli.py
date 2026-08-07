"""The command contract for `polish scan`, `polish observe` and `polish run`.

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
    assert reported["changes"] == "0 committed, 0 dropped"


def test_run_reports_each_change_it_committed(make_git_repo, capsys):
    repo = make_git_repo(
        {
            "pyproject.toml": '[project]\nname = "demo"\n',
            "demo.py": "import json\nimport os\n\nVALUE = os.sep\n",
        }
    )

    code = main(["run", str(repo)])
    captured = capsys.readouterr()

    assert code == 0
    assert "changes   1 committed, 0 dropped" in captured.out
    assert "  + refactor(imports): drop 1 unused import from demo.py" in captured.out


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


# --- polish observe -------------------------------------------------------


def test_observe_status_before_anything_has_run(make_repo, capsys):
    repo = make_repo({"pyproject.toml": '[project]\nname = "demo"\n', "demo.py": ""})

    code = main(["observe", "status", str(repo)])
    captured = capsys.readouterr()

    assert code == 0
    assert "not running" in captured.out
    assert "none yet" in captured.out
    assert captured.err == ""


def test_observe_stop_when_nothing_is_running(make_repo, capsys):
    repo = make_repo({"pyproject.toml": '[project]\nname = "demo"\n', "demo.py": ""})

    code = main(["observe", "stop", str(repo)])

    assert code == 0
    assert "no observer running" in capsys.readouterr().out


def test_observe_start_declines_a_non_python_directory(make_repo, capsys):
    repo = make_repo({"index.js": "", "package.json": "{}\n"})

    code = main(["observe", "start", str(repo)])
    captured = capsys.readouterr()

    assert code == 1
    assert "not a Python project" in captured.err
    assert not (repo / ".polish").exists()


def test_observe_start_and_stop_drive_a_real_detached_process(make_repo, capsys):
    """The daemon has to survive being started the way a user starts it.

    `start` returning before the child has claimed the pidfile would make the
    command a lie, and `kill $(cat .polish/daemon.pid)` — the fallback SPEC §3.7
    requires to work without Polish — would have nothing to read.
    """
    import os
    import time

    repo = make_repo(
        {
            "pyproject.toml": '[project]\nname = "demo"\n',
            "demo.py": "def f():\n    pass\n",
        }
    )
    try:
        assert main(["observe", "start", str(repo)]) == 0
        pid = int((repo / ".polish" / "daemon.pid").read_text())
        os.kill(pid, 0)

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (repo / ".polish" / "graph.db").is_file():
                break
            time.sleep(0.05)

        capsys.readouterr()
        assert main(["observe", "status", str(repo)]) == 0
        assert f"running (pid {pid})" in capsys.readouterr().out
    finally:
        assert main(["observe", "stop", str(repo)]) == 0
    assert not (repo / ".polish" / "daemon.pid").exists()


def test_observe_watch_declines_a_non_python_directory_without_leaving_state(
    make_repo, capsys
):
    """The foreground daemon must refuse, not spin over a tree it cannot scan."""
    repo = make_repo({"index.js": "", "package.json": "{}\n"})

    code = main(["observe", "watch", str(repo)])

    assert code == 1
    assert "not a Python project" in capsys.readouterr().err
    assert not (repo / ".polish").exists()
