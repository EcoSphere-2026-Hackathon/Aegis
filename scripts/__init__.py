"""Harnesses and operational tools.

A package rather than loose files because ``run.py --demo`` imports the
golden-demo replay, so the same module has to resolve identically whether it
is run directly or imported. Without this, a type checker sees the file under
two module names and refuses to check either.
"""

from __future__ import annotations

import sys


def use_utf8_stdout() -> None:
    """Stop a Windows console from killing a harness over an arrow.

    These scripts print box drawing, arrows and em-dashes. A Windows console
    defaults to cp1252, which cannot encode any of them, and ``print`` raises
    ``UnicodeEncodeError`` -- so ``scripts/run_golden_demo.py`` died on its
    first claim line, and the demo gate the README tells you to run was
    unrunnable on the machine this project is developed on.

    Reconfiguring is better than replacing the characters with ASCII: the
    output stays readable everywhere that can render it, and degrades to
    replacement characters instead of a traceback where it cannot. Guarded
    because ``reconfigure`` needs a real text stream, and stdout may have
    been replaced by a test harness or a pipe wrapper.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # already detached, or not a text stream
            pass
