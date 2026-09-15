"""Process exit codes shared by all MeshForge entry points.

Codes 0-2 follow Click/Typer conventions (2 = usage error, e.g. unknown option),
so MeshForge-specific conditions start at 3 to stay distinguishable in scripts
and tests.
"""

from enum import IntEnum


class ExitCode(IntEnum):
    """Exit codes returned by ``compute``, ``meshforge-controller`` and ``meshforge-agent``."""

    OK = 0
    """Command succeeded."""

    FAILURE = 1
    """Generic runtime failure."""

    USAGE_ERROR = 2
    """Invalid command-line usage (reserved by Click/Typer)."""

    NOT_IMPLEMENTED = 3
    """The command exists but its functionality lands in a later roadmap version."""
