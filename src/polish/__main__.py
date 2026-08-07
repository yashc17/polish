"""`python -m polish`, equivalent to the `polish` console script.

The observer spawns its detached child through this rather than through the
console script, because `sys.executable -m polish` is the one invocation that is
correct whether Polish was installed with pipx, uv, or an editable checkout with
no scripts directory on PATH.
"""

from __future__ import annotations

from polish.cli import run

run()
