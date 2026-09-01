"""Test package bootstrap.

Silences AEGIS's structured logger by default so a suite run is readable.
Tests that assert on log output reconfigure it themselves with an explicit
stream, which is why the logger is configurable rather than global.
"""

from __future__ import annotations

import io

from backend.common.logging import configure_logging

configure_logging("CRITICAL", stream=io.StringIO())
