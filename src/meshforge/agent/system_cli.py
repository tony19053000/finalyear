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
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from meshforge.common.buildinfo import bundle_root
from meshforge.common.exit_codes import ExitCode

__all__ = [
    "SYSTEM_LAUNCHER",
    "Decision",
    "apply",
    "decide",
    "system_install_present",
]

# Light on purpose: this module runs before every command, ``--version``
# included, so it does not import ``common.config`` (pydantic). The values are
# pinned equal to config's by tests/unit/agent/test_v017_g1_system_cli.py.
INSTALL_MODE_ENV_VAR: Final = "MESHFORGE_INSTALL_MODE"
SYSTEM_LINUX_CONFIG_DIR: Final = Path("/etc/meshforge")
SYSTEM_LINUX_DATA_DIR: Final = Path("/var/lib/meshforge")
SERVICE_USER: Final = "meshforge"
SYSTEM_PROGRAM_DIR: Final = Path("/opt/meshforge")
SYSTEM_LAUNCHER: Final = SYSTEM_PROGRAM_DIR / "current" / "bin" / "meshforge-agent"
_ANYONE: Final = frozenset({"--version", "--help", "-h", "migrate-identity"})


@dataclass(frozen=True, slots=True)
class Decision:
    action: Literal["none", "reexec", "warn"]
    argv: tuple[str, ...] = ()
    message: str = ""


def system_install_present(
    config_dir: Path = SYSTEM_LINUX_CONFIG_DIR,
    program_dir: Path = SYSTEM_PROGRAM_DIR,
) -> bool:
    return config_dir.is_dir() and (program_dir / "current").exists()


def _current_user() -> str | None:
    if sys.platform == "win32":  # pragma: no cover - Linux package only
        return None
    import pwd

    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return None


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
    if platform != "linux" or not installed or user == SERVICE_USER:
        return Decision("none")
    if environ.get(INSTALL_MODE_ENV_VAR, "").strip():
        return Decision("none")  # system (the unit) or an explicit user opt-out
    if not args or args[0] in _ANYONE or any(a in ("--help", "-h") for a in args):
        return Decision("none")
    if not bundled:
        return Decision(
            "warn",
            message=(
                "meshforge-agent: note: a system install exists; this development "
                "command uses YOUR user-mode state, not the service's "
                f"({SYSTEM_LINUX_DATA_DIR}). Use {SYSTEM_LAUNCHER.name} from the "
                f"package for the service, or set {INSTALL_MODE_ENV_VAR}=user to "
                "silence this."
            ),
        )
    argv = (
        "sudo",
        "-u",
        SERVICE_USER,
        "--",
        "/usr/bin/env",
        f"{INSTALL_MODE_ENV_VAR}=system",
        str(SYSTEM_LAUNCHER),
        *args,
    )
    return Decision(
        "reexec",
        argv=argv,
        message=(
            f"meshforge-agent: acting on the system service's state "
            f"({SYSTEM_LINUX_DATA_DIR}) as user {SERVICE_USER!r} via sudo"
        ),
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
    say = stderr or (lambda text: sys.stderr.write(text + "\n"))
    if sys.platform != "linux" or not system_install_present():
        return  # the common case, decided with two stat calls
    decision = decide(
        args,
        environ=os.environ,
        platform=sys.platform,
        user=_current_user(),
        installed=system_install_present(),
        bundled=bundle_root() is not None,
    )
    if decision.action == "none":
        return
    if decision.action == "warn":
        say(decision.message)
        return
    if which("sudo") is None:
        say(
            "meshforge-agent: this laptop's agent runs as the system user "
            f"{SERVICE_USER!r}; run this command as that user (no sudo found): "
            f"su -s /bin/sh {SERVICE_USER} -c '{INSTALL_MODE_ENV_VAR}=system "
            f"{SYSTEM_LAUNCHER} ...'"
        )
        raise SystemExit(int(ExitCode.CONFIG_ERROR))
    say(decision.message)
    sys.stdout.flush()
    sys.stderr.flush()
    execvp("sudo", list(decision.argv))
