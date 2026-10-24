"""Where the ``meshforge-controller`` .deb puts things (V0.17 G2, D385/D390).

The controller package can be installed next to the agent package (one machine
that is both the controller and a worker): they share the ``meshforge`` service
user and ``/etc/meshforge`` + ``/var/lib/meshforge``, but nothing else::

    /opt/meshforge/controller/<ver>/      the controller bundle (read-only)
    /opt/meshforge/controller/current     -> <ver> (the pointer an upgrade swaps)
    /etc/meshforge/controller.toml        the controller's settings (the agent
                                          keeps config.toml; MESHFORGE_CONFIG
                                          is set by the unit and the CLI re-exec)
    /var/lib/meshforge/controller/        CA, TLS, tokens, blobs, setup token
    /var/lib/meshforge/db/                the managed PostgreSQL (D390)

The agent's program files are ``/opt/meshforge/<ver>`` + ``/opt/meshforge/current``;
the agent never uses a version folder named ``controller``. Light module (no
pydantic): the console-script entry imports it before every command.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from meshforge.common.system_cli import (
    SYSTEM_LINUX_CONFIG_DIR,
    SYSTEM_PROGRAM_DIR,
    SystemProgram,
)

__all__ = [
    "CONTROLLER_CONFIG_FILE",
    "CONTROLLER_PROGRAM",
    "CONTROLLER_PROGRAM_DIR",
    "SERVICE_UNIT",
]

CONTROLLER_PROGRAM_DIR: Final = SYSTEM_PROGRAM_DIR / "controller"
CONTROLLER_CONFIG_FILE: Final = Path(SYSTEM_LINUX_CONFIG_DIR) / "controller.toml"
SERVICE_UNIT: Final = "meshforge-controller.service"

CONTROLLER_PROGRAM: Final = SystemProgram(
    prog="meshforge-controller",
    program_dir=CONTROLLER_PROGRAM_DIR,
    # The bare command starts a server: a human's `meshforge-controller` on the
    # controller machine stays a developer server in their own state (the
    # managed lock would refuse a second server over the service's database).
    run_in_place_without_args=True,
    extra_env=(f"MESHFORGE_CONFIG={CONTROLLER_CONFIG_FILE}",),
    # Admin commands act on the service's state; anything else (the server
    # itself, `--version`) runs in place.
    reexec_commands=frozenset(
        {"token", "fabric", "node", "user", "ca", "db", "doctor", "setup"}
    ),
)
