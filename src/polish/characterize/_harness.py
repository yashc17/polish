"""The program Polish runs *inside* the sandbox. Copied there, never imported.

This module is the fifth allowlisted command shape's payload (ADR 0007). Polish
copies it into a scratch directory as `_polish_harness.py`, puts that directory
first on `PYTHONPATH`, and runs

    <venv>/bin/python -P -c "import _polish_harness as _h; _h.<task>()"

for one of three tasks. It runs under the *sandbox's* interpreter, in a
virtualenv where `polish` is not installed, so it has three hard constraints
that the rest of the package does not:

1. **It imports nothing from `polish`.** Standard library and pytest only, and
   pytest only from inside a function, so a sandbox without it fails with a
   sentence rather than a traceback.
2. **Its parameters arrive through the environment**, because the command line
   is a constant — that constancy is the entire argument for admitting the fifth
   shape, and an argument that carried a path would give it away.
3. **It always produces a result file**, including when the repository's suite
   raises or calls `sys.exit`. Polish reads the file; a repository that can make
   the file not exist can make the harness say nothing, and "said nothing" must
   not be readable as "found no flakes".

The three tasks:

    suite()     run the repository's own suite; report every test's outcome
    trace()     the same, with a tracer recording what really got called
    oracles()   run the characterization tests Polish generated, and only those

`suite()` is deliberately the same run three times over for the pre-run baseline
and again during verification (issue #5): the outcomes are compared between
runs, so the runs must be identical in every respect Polish controls.
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading

#: Where Polish left the JSON parameters, and where the result goes back.
CONFIG = "POLISH_HARNESS_CONFIG"
OUTPUT = "POLISH_HARNESS_OUTPUT"

#: Gate 2's own arguments, character for character. The baseline has to measure
#: the suite the probe promoted, not a differently-configured one.
PYTEST_ARGUMENTS = ("-q", "--tb=no", "-p", "no:cacheprovider")

#: Values whose `repr` is source that reconstructs them. Compared by exact type
#: rather than `isinstance`, so an `IntEnum` or a `str` subclass — whose reprs
#: are *not* reconstructible — is refused rather than silently mishandled.
ATOMS = (type(None), bool, int, float, str, bytes)

CONTAINERS = (tuple, list, set, frozenset)

#: `co_flags` for `*args` and `**kwargs`. A call Polish cannot replay by writing
#: its arguments out positionally is a call Polish does not record.
VARIADIC = 0x04 | 0x08


def suite() -> None:
    """Run the repository's suite and report what each test did."""
    _emit(_guard(lambda: _pytest(list(PYTEST_ARGUMENTS))))


def trace() -> None:
    """Run the suite with a tracer attached, recording real calls and returns.

    SPEC §3.4 names `sys.settrace` as Python's characterization mechanism, and
    §3.1 requires the traces be from *real execution* — which is why the tracer
    is wrapped around the repository's own suite rather than around anything
    Polish invented to exercise it.
    """
    _emit(_guard(_traced))


def oracles() -> None:
    """Run only the generated characterization tests.

    `-c` points pytest at an ini file Polish wrote, so the repository's own
    `addopts` and `testpaths` do not reach this run. Those settings are the
    repository's statement about its own suite; applying them to Polish's
    generated tests would let a `--cov` or a `-p` line decide whether an oracle
    is trustworthy.
    """
    directory = _configuration()["oracles"]
    _emit(
        _guard(
            lambda: _pytest(
                [
                    directory,
                    "-c",
                    os.path.join(directory, "pytest.ini"),
                    *PYTEST_ARGUMENTS,
                ]
            )
        )
    )


def _traced() -> dict:
    recorder = _Recorder(_configuration())
    sys.settrace(recorder.enter)
    # Threads started by the suite are traced too, or a repository that runs its
    # work on a worker would be reported as having executed nothing at all.
    threading.settrace(recorder.enter)
    try:
        result = _pytest(list(PYTEST_ARGUMENTS))
    finally:
        sys.settrace(None)
        threading.settrace(None)
    result["samples"] = recorder.samples
    result["executed"] = sorted(recorder.executed)
    return result


def _pytest(arguments: list[str]) -> dict:
    try:
        import pytest
    except ImportError as missing:  # pragma: no cover - the sandbox installs it
        return {"error": f"pytest is not importable in the sandbox: {missing}"}
    outcomes = _Outcomes()
    code = pytest.main(arguments, plugins=[outcomes])
    return {"exit": int(code), "tests": outcomes.tests, "errors": outcomes.errors}


class _Outcomes:
    """A pytest plugin that remembers what happened to each test.

    Per-test outcomes are the reason the harness exists. `pytest -q` prints a
    row of dots, and a row of dots cannot answer "which test was it" — which is
    the only question a quarantine list is made of.

    A test's outcome is the worst thing that happened to it across setup, call
    and teardown, so a fixture that fails intermittently quarantines the tests it
    feeds rather than being invisible.
    """

    def __init__(self) -> None:
        self.tests: dict[str, str] = {}
        self.errors: list[str] = []

    def pytest_runtest_logreport(self, report) -> None:
        if report.outcome == "failed":
            self.tests[report.nodeid] = "failed"
        elif report.when == "call":
            self.tests.setdefault(report.nodeid, report.outcome)
        elif report.when == "setup" and report.outcome == "skipped":
            self.tests.setdefault(report.nodeid, "skipped")

    def pytest_collectreport(self, report) -> None:
        if report.outcome == "failed":
            self.errors.append(report.nodeid or "<rootdir>")


class _Recorder:
    """Turns real calls into replayable ones, and refuses everything else.

    The bar is deliberately high and the rejections are the interesting part. A
    call is recorded only when every argument *and* the return value can be
    written back out as source that reconstructs them — the Tier 1 counterpart
    of ADR 0005's provably-inert rule. Everything else is reported as executed
    with no oracle, which is a true statement about the code, rather than
    guessed at, which would not be.

    Four things are refused, each for its own reason:

    * **Anything not literal-reconstructible.** An oracle that cannot rebuild
      its own inputs is not an oracle. This is also what excludes methods for
      free: `self` is an instance, and an instance is not a literal.
    * **`*args` / `**kwargs`.** The recorded call would have to guess how the
      arguments were spelled at the call site.
    * **Calls that raised *and* returned `None`.** When an exception propagates
      out of a frame the `return` event still fires, with `None` — so recording
      it would assert that a function returns `None` when in fact it blew up.
      Note how narrow this is: a call that raised internally and *handled* it is
      an ordinary recordable call, and `try/except` is far too common in real
      Python to refuse wholesale. Only the genuinely ambiguous shape is refused.
    * **Calls that mutated their arguments.** Not because the recorded answer is
      wrong — the arguments are captured at call time, so a replay gets exactly
      the inputs the recording had. Because a function that rewrites what it was
      handed is observably side-effecting, and an oracle for one runs those
      effects again on every replay and every verification for the rest of the
      night.
    """

    def __init__(self, config: dict) -> None:
        self.modules: dict[str, str] = dict(config.get("modules", {}))
        self.per_symbol = int(config.get("samples", 3))
        self.chars = int(config.get("chars", 200))
        self.limit = int(config.get("depth", 4))
        self.total = int(config.get("total", 2000))
        self.samples: list[dict] = []
        self.executed: set[str] = set()
        self._resolved: dict[str, str | None] = {}
        self._seen: set[tuple] = set()
        self._counts: dict[str, int] = {}

    def enter(self, frame, event, arg):
        """The global trace function: one decision per call, as cheap as it can be.

        This runs on every function call in the process, pytest's own included,
        so the first thing it does is the filename lookup that rejects almost
        all of them.
        """
        if event != "call":
            return None
        code = frame.f_code
        module = self._module(code.co_filename)
        if module is None:
            return None
        # `<locals>`, `<lambda>`, `<listcomp>`, `<module>` — none of them is a
        # name an oracle could call, so none of them is worth tracing.
        qualname = code.co_qualname
        if "<" in qualname:
            return None

        symbol = f"{module}.{qualname}"
        self.executed.add(symbol)

        # Line events are most of the cost of tracing and none of the value:
        # everything here happens on `call` and `return`.
        frame.f_trace_lines = False

        call = self._begin(frame, module, qualname, symbol)
        return None if call is None else call.watch

    def _begin(self, frame, module: str, qualname: str, symbol: str):
        if len(self.samples) >= self.total:
            return None
        if self._counts.get(symbol, 0) >= self.per_symbol:
            return None

        code = frame.f_code
        if code.co_flags & VARIADIC:
            return None
        positional = code.co_argcount
        names = code.co_varnames[: positional + code.co_kwonlyargcount]
        local = frame.f_locals

        rendered = []
        for name in names:
            if name not in local:
                return None
            text = self._source(local[name], 0)
            if text is None:
                return None
            rendered.append(text)

        key = (symbol, tuple(rendered))
        if key in self._seen:
            return None
        return _Call(
            self, module, qualname, tuple(names), tuple(rendered), positional, key
        )

    def finish(self, call, frame, value) -> None:
        local = frame.f_locals
        for name, before in zip(call.names, call.rendered, strict=True):
            if name not in local or self._source(local[name], 0) != before:
                return
        expected = self._source(value, 0)
        if expected is None or call.key in self._seen:
            return
        self._seen.add(call.key)
        self._counts[call.symbol] = self._counts.get(call.symbol, 0) + 1
        self.samples.append(
            {
                "module": call.module,
                "qualname": call.qualname,
                "arguments": list(call.arguments()),
                "expected": expected,
            }
        )

    def _source(self, value, depth: int) -> str | None:
        """`repr(value)`, if that repr is source that rebuilds an equal value.

        Note what this does *not* do: pin anything. Issue #5 rejected freezing
        clocks and seeding RNGs because every pin asserts something about the
        code's behaviour, and a wrong pin produces a confident oracle for
        behaviour that never happens. So a value whose repr varies between runs
        is not stabilised here — it is recorded honestly and thrown away by the
        replay gate, which is the only thing entitled to that judgement.
        """
        kind = type(value)
        if kind is float and not math.isfinite(value):
            # `inf` and `nan` are names, not literals; written into a generated
            # test they would be a NameError rather than a value.
            return None
        if kind not in ATOMS:
            if depth >= self.limit:
                return None
            if kind in CONTAINERS:
                if any(self._source(item, depth + 1) is None for item in value):
                    return None
            elif kind is dict:
                for entry, item in value.items():
                    if self._source(entry, depth + 1) is None:
                        return None
                    if self._source(item, depth + 1) is None:
                        return None
            else:
                return None
        try:
            text = repr(value)
        except Exception:
            # A repr that raises is a fact about the value, not an error worth
            # ending a night's tracing over.
            return None
        return text if len(text) <= self.chars else None

    def _module(self, filename: str) -> str | None:
        """The dotted module a traced frame belongs to, or `None` to ignore it.

        Memoised per filename because it is on the hot path — `realpath` once
        per file loaded, rather than once per call in the process.
        """
        try:
            return self._resolved[filename]
        except KeyError:
            pass
        found = self.modules.get(filename)
        if found is None:
            found = self.modules.get(os.path.realpath(filename))
        self._resolved[filename] = found
        return found


class _Call:
    """One call being watched between its `call` and `return` events."""

    def __init__(
        self, recorder, module, qualname, names, rendered, positional, key
    ) -> None:
        self.recorder = recorder
        self.module = module
        self.qualname = qualname
        self.symbol = f"{module}.{qualname}"
        self.names = names
        self.rendered = rendered
        self.positional = positional
        self.key = key
        self.raised = False
        self.done = False

    def arguments(self) -> list[str]:
        """The call as source: positionals in order, keyword-only by name."""
        return [
            *self.rendered[: self.positional],
            *(
                f"{name}={text}"
                for name, text in zip(
                    self.names[self.positional :],
                    self.rendered[self.positional :],
                    strict=True,
                )
            ),
        ]

    def watch(self, frame, event, arg):
        if event == "exception":
            self.raised = True
        elif event == "return" and not self.done and not (self.raised and arg is None):
            # `raised and arg is None` is the ambiguous shape: an exception
            # propagating out looks exactly like a function returning `None`.
            # Everything else is a real return, including one from a frame that
            # raised and dealt with it.
            self.done = True
            self.recorder.finish(self, frame, arg)
        # Always the same function back: a local trace that returns `None` is
        # documented to stop tracing the frame, and this one has to see the
        # `return` that follows an `exception`.
        return self.watch


def _configuration() -> dict:
    with open(os.environ[CONFIG], encoding="utf-8") as handle:
        return json.load(handle)


def _guard(work) -> dict:
    """Run `work`, turning anything it does into a result rather than a crash."""
    try:
        return work()
    except SystemExit as stopped:
        # A `sys.exit` inside a conftest, or a plugin that gives up. Real, and
        # not a reason for Polish to have no answer.
        return {"error": f"the suite exited: {stopped.code}"}
    except Exception as failure:
        return {"error": f"{type(failure).__name__}: {failure}"}


def _emit(result: dict) -> None:
    result.setdefault("tests", {})
    result.setdefault("errors", [])
    with open(os.environ[OUTPUT], "w", encoding="utf-8") as handle:
        json.dump(result, handle)
