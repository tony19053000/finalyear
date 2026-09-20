"""Guarded imports of the ``agent`` extra's untyped libraries (``psutil``, ``pynvml``).

Both are optional at import time: ``meshforge-agent --version`` and every unit test
must work on a base install (V0.1 criterion 1), and a laptop without an NVIDIA GPU
has no working NVML even when ``nvidia-ml-py`` is installed. The modules ship no
type information, so they are exposed as ``Any`` from exactly one place instead of
an ``import`` + ``type: ignore`` in every probe.

The lookups are cached per process: a missing module is a permanent fact, and
``importlib`` is not asked again on every resource sample.
"""

from __future__ import annotations

from functools import cache
from typing import Any

__all__ = ["psutil_module", "pynvml_module"]


@cache
def psutil_module() -> Any | None:
    """The ``psutil`` module, or None on a base install."""
    try:
        import psutil  # type: ignore[import-untyped]
    except ImportError:
        return None
    return psutil


@cache
def pynvml_module() -> Any | None:
    """The ``pynvml`` module (``nvidia-ml-py``), or None when it is not installed."""
    try:
        import pynvml  # type: ignore[import-untyped]
    except ImportError:
        return None
    return pynvml
