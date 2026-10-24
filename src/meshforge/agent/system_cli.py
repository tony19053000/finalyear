"""A human's ``meshforge-agent ...`` acts on the system service's state (V0.17 G1, Q15).

On a laptop with the ``.deb`` installed, the identity, config and policy belong
to the ``meshforge`` service account (``/var/lib/meshforge``, ``/etc/meshforge``).
A person typing ``meshforge-agent identity show`` or ``join`` runs as themselves,
in user mode (``MESHFORGE_INSTALL_MODE`` is only set by the unit, Q11) - without
this module that would read, or worse *mint*, a second identity in their home.

So before any command runs, :func:`decide` checks: Linux, a system install is
present (``/etc/meshforge`` **and** ``/opt/meshforge/current``), the caller is not
the service user, ``MESHFORGE_INSTALL_MODE`` is unset, and the command is not
one that is fine for anyone (``--version``, ``--help``, ``migrate-identity``,
which runs as root by design). Then:

* this CLI is the packaged one (runs from a bundle) -> re-execute it as the
  service user: ``sudo -u meshforge -- /usr/bin/env MESHFORGE_INSTALL_MODE=system
  /opt/meshforge/current/bin/meshforge-agent <args>``, saying so on stderr
  (``sudo`` asks for the admin's password; non-admins cannot, which is right -
  the G3 local console is their way in);
* this CLI is a developer checkout (``uv run``) -> it keeps its user-mode state
  but warns on stderr that the service's state is elsewhere.

``MESHFORGE_INSTALL_MODE=user`` is the explicit opt-out (a developer who wants
their own state on a machine that also has the package).
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final

from meshforge.common import system_cli as _common
from meshforge.common.buildinfo import bundle_root
from meshforge.common.system_cli import (
    INSTALL_MODE_ENV_VAR,
    SERVICE_USER,
    SYSTEM_LINUX_CONFIG_DIR,
    SYSTEM_LINUX_DATA_DIR,
    SYSTEM_PROGRAM_DIR,
    Decision,
    SystemProgram,
)

__all__ = [
    "AGENT_PROGRAM",
    "INSTALL_MODE_ENV_VAR",
    "SERVICE_USER",
    "SYSTEM_LAUNCHER",
    "SYSTEM_LINUX_CONFIG_DIR",
    "SYSTEM_LINUX_DATA_DIR",
    "SYSTEM_PROGRAM_DIR",
    "Decision",
    "apply",
    "decide",
    "system_install_present",
]

# The shared logic lives in :mod:`meshforge.common.system_cli` (V0.17 G2: the
# controller package needs the same re-execution); this module keeps the
# agent's program description and its historical entry points.
AGENT_PROGRAM: Final = SystemProgram(
    prog="meshforge-agent",
    program_dir=SYSTEM_PROGRAM_DIR,
    anyone=frozenset({"migrate-identity"}),  # runs as root by design
)
SYSTEM_LAUNCHER: Final = AGENT_PROGRAM.launcher


def system_install_present(
    config_dir: Path = SYSTEM_LINUX_CONFIG_DIR,
    program_dir: Path = SYSTEM_PROGRAM_DIR,
) -> bool:
    return config_dir.is_dir() and (program_dir / "current").exists()


def _current_user() -> str | None:
    return _common.current_user()


def decide(
    args: Sequence[str],
    *,
    environ: Mapping[str, str],
    platform: str,
    user: str | None,
    installed: bool,
    bundled: bool,
) -> Decision:
    """What to do with ``meshforge-agent <args>`` (module docstring). Pure."""
    return _common.decide(
        args,
        program=AGENT_PROGRAM,
        environ=environ,
        platform=platform,
        user=user,
        installed=installed,
        bundled=bundled,
    )


def apply(
    args: Sequence[str],
    *,
    execvp: Callable[[str, list[str]], object] = os.execvp,
    which: Callable[[str], str | None] = shutil.which,
    stderr: Callable[[str], object] | None = None,
) -> None:
    """Act on :func:`decide` for this process; returns only when the command
    should run here (``none`` / ``warn``). Exits ``CONFIG_ERROR`` when the
    re-execution is needed but ``sudo`` is missing."""
    if sys.platform != "linux" or not system_install_present():
        return  # the common case, decided with two stat calls
    # Looked up at call time (module globals), so tests can replace them.
    _common.apply(
        args,
        program=AGENT_PROGRAM,
        installed=lambda: system_install_present(),
        user=lambda: _current_user(),
        bundled=lambda: bundle_root() is not None,
        execvp=execvp,
        which=which,
        stderr=stderr,
    )
