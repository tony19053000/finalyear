"""Shared types for the ``compute`` unit tests (V0.3 G5).

Lives in ``tests/_support`` (on ``sys.path`` via ``tests/conftest.py``) because a
test module cannot import from a ``conftest`` - ``tests`` is not a package under
pytest's prepend import mode, and the bare ``conftest`` name belongs to whichever
directory was loaded last.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

__all__ = ["Handler", "TransportFactory"]

type Handler = Callable[[httpx.Request], httpx.Response]
"""What ``httpx.MockTransport`` calls: one request in, one canned response out."""

type TransportFactory = Callable[[Handler], None]
"""The ``controller`` fixture: install ``handler`` for this test's CLI commands."""
