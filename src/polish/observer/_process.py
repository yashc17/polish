"""Starting, finding and stopping the daemon with tools that already exist.

SPEC §3.7 accepts the daemon's cost on one condition: "it must be trivially
inspectable and killable". That is a constraint on this module more than on the
store. So the daemon is an ordinary child process holding an ordinary pidfile —
`ps`, `kill` and `cat` are enough to run it, and `polish observe` is a
convenience over them rather than the only way in.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import _store

#: How long to wait for a spawned daemon to announce itself, and for a stopped
#: one to go away, before giving up on the polite path.
HANDSHAKE_SECONDS = 5.0
TERM_SECONDS = 5.0


def running_pid(root: Path) -> int | None:
    """The live daemon's pid, or None. Clears a pidfile left by a crash."""
    path = _store.pid_path(root)
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if _alive(pid):
        return pid
    path.unlink(missing_ok=True)
    return None


def claim(root: Path) -> None:
    """Record this process as the daemon for `root`."""
    _store.store_dir(root).mkdir(parents=True, exist_ok=True)
    path = _store.pid_path(root)
    temporary = path.with_suffix(".pid.tmp")
    temporary.write_text(f"{os.getpid()}\n", encoding="utf-8")
    os.replace(temporary, path)


def release(root: Path) -> None:
    if running_pid(root) == os.getpid():
        _store.pid_path(root).unlink(missing_ok=True)


def start(root: Path) -> int:
    """Spawn a detached daemon and wait until it has claimed the pidfile.

    Returning only once the child is up is what makes `polish observe start`
    honest: a command that reports success and leaves nothing running is exactly
    the kind of thing that erodes trust in an always-on process.
    """
    existing = running_pid(root)
    if existing is not None:
        return existing

    # Fixed argv, no shell. `-m polish` rather than the console script because
    # it is the one invocation that works under pipx, uv, and an editable
    # checkout with no scripts directory on PATH.
    subprocess.Popen(
        [sys.executable, "-m", "polish", "observe", "watch", str(root)],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + HANDSHAKE_SECONDS
    while time.monotonic() < deadline:
        pid = running_pid(root)
        if pid is not None:
            return pid
        time.sleep(0.05)
    raise ObserverFailedToStart(
        f"the observer for {root} did not start within {HANDSHAKE_SECONDS:.0f}s.\n"
        f"Run it in the foreground to see why: polish observe watch {root}"
    )


def stop(root: Path) -> int | None:
    """Ask the daemon to stop; insist if it will not. Returns the pid it killed."""
    pid = running_pid(root)
    if pid is None:
        return None
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + TERM_SECONDS
    while time.monotonic() < deadline:
        if not _alive(pid):
            _store.pid_path(root).unlink(missing_ok=True)
            return pid
        time.sleep(0.05)
    os.kill(pid, signal.SIGKILL)
    _store.pid_path(root).unlink(missing_ok=True)
    return pid


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Someone else's process holds the pid. Not our daemon, but not ours to
        # signal either — treat it as alive so we never kill a stranger.
        return True
    return True


class ObserverFailedToStart(Exception):
    """Raised when a spawned daemon never announced itself."""
