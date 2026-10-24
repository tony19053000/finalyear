"""``meshforge-console-open``: the app-menu entry that opens the local console.

Runs as the **desktop user** (``/usr/share/applications/meshforge.desktop``),
never as the service:

1. read the console token (``/run/meshforge-console/token`` for the system
   service; readable only by the service and ``meshforge-admin`` - a user
   outside the group gets a plain "ask an admin" message);
2. wait (up to ``--wait`` seconds) until the agent answers ``/console/v1/health``
   - the menu entry may be clicked while the service is still starting;
3. trade the token for a one-time launch code (``POST /console/v1/launch-code``,
   ``Authorization: Bearer``) and open ``http://127.0.0.1:47130/#c=<code>`` in
   the default browser. Only that 60-second, single-use code is ever in a
   command line (``xdg-open``'s argv is visible to other users), never the
   token.

``--docker-access`` runs the Docker-access helper through ``pkexec`` instead
(the OS asks for an admin password; polkit action ``org.meshforge.docker-access``,
progress note decision 4): the service cannot do it itself.

Messages go to stderr and, when there is a desktop notifier (``notify-send``),
to a notification - a menu click has no terminal.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shlex
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Final

import httpx

from meshforge.common.exit_codes import ExitCode

__all__ = ["DOCKER_ACCESS_HELPER", "main", "read_token"]

PROG: Final = "meshforge-console-open"
DOCKER_ACCESS_HELPER: Final = Path("/usr/lib/meshforge/docker-access")
_ADMIN_GROUP: Final = "meshforge-admin"


class LaunchError(Exception):
    def __init__(self, message: str, code: int = ExitCode.FAILURE) -> None:
        super().__init__(message)
        self.code = code


def _tell(message: str) -> None:
    sys.stderr.write(f"{PROG}: {message}\n")
    notifier = shutil.which("notify-send")
    desktop = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    if notifier and desktop:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(  # noqa: S603 - fixed argv, no shell
                [notifier, "--app-name=MeshForge", "MeshForge", message],
                check=False,
                timeout=5,
                capture_output=True,
            )


def read_token(path: Path) -> str:
    """The token, or :class:`LaunchError` in plain words."""
    try:
        text = path.read_text(encoding="ascii").strip()
    except PermissionError:
        raise LaunchError(
            "your user may not open the MeshForge console on this laptop (it is "
            f"not in the {_ADMIN_GROUP} group). Ask an admin to run: sudo usermod "
            f"-aG {_ADMIN_GROUP} {os.environ.get('USER', '<your user>')} - then log "
            "out and back in.",
            ExitCode.CONFIG_ERROR,
        ) from None
    except FileNotFoundError:
        raise LaunchError(
            "MeshForge is not running on this laptop (no console token). Start it: "
            "sudo systemctl start meshforge-agent",
            ExitCode.UNAVAILABLE,
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise LaunchError(f"cannot read the console token {path}: {exc}") from None
    if not text:
        raise LaunchError(f"the console token file {path} is empty")
    return text


def _settings_paths(config: Path | None) -> tuple[Path, int]:
    """Token file + port from the agent's settings. On the system install a
    desktop user may not be able to read ``/etc/meshforge/config.toml``; the
    package defaults then apply (``/run/meshforge-console``, 47130)."""
    from meshforge.agent.settings import (
        DEFAULT_CONSOLE_PORT,
        SYSTEM_CONSOLE_TOKEN_DIR,
        AgentSettings,
    )
    from meshforge.common.config import InstallMode, install_mode
    from meshforge.common.errors import MeshForgeError

    try:
        settings = AgentSettings.load(config, agent={"discovery": {"enabled": True}})
    except (MeshForgeError, OSError) as exc:
        if config is None and install_mode() is InstallMode.SYSTEM:
            return SYSTEM_CONSOLE_TOKEN_DIR / "token", DEFAULT_CONSOLE_PORT
        message = exc.message if isinstance(exc, MeshForgeError) else str(exc)
        raise LaunchError(f"cannot read the agent settings: {message}") from None
    return settings.console_token_dir / "token", settings.agent.console.port


_SG_MARK: Final = "MESHFORGE_CONSOLE_OPEN_SG"


def _member_on_disk(group: str) -> bool:
    """Listed in ``/etc/group`` for ``group`` (the package just added us) but
    this login session predates it."""
    try:
        import grp
        import pwd

        entry = grp.getgrnam(group)
        name = pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError):
        return False
    return name in entry.gr_mem and entry.gr_gid not in os.getgroups()


def _reexec_with_group(argv: list[str]) -> None:
    """``sg meshforge-admin``: a user listed in the group gets it without a
    password and without logging out first (the package adds the installing
    user, whose desktop session started before that). Returns only if it
    cannot re-execute."""
    sg = shutil.which("sg")
    if sg is None or os.environ.get(_SG_MARK) or not _member_on_disk(_ADMIN_GROUP):
        return
    os.environ[_SG_MARK] = "1"
    command = shlex.join(
        [sys.executable, "-m", "meshforge.agent.console.launcher", *argv]
    )
    with contextlib.suppress(OSError):
        os.execv(sg, [sg, _ADMIN_GROUP, "-c", command])  # noqa: S606 - fixed argv


def _wait_healthy(client: httpx.Client, base: str, wait_s: float) -> None:
    deadline = time.monotonic() + wait_s
    last = "no answer"
    while True:
        try:
            if client.get(f"{base}/console/v1/health").status_code == 200:
                return
            last = "unexpected answer"
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        if time.monotonic() >= deadline:
            raise LaunchError(
                f"MeshForge did not answer at {base} within {wait_s:.0f} s ({last}). "
                "See: journalctl -u meshforge-agent -n 50",
                ExitCode.UNAVAILABLE,
            )
        time.sleep(0.5)


def launch_url(token_path: Path, port: int, *, wait_s: float) -> str:
    """The one-time console URL (steps 1-3 of the module docstring)."""
    token = read_token(token_path)
    base = f"http://127.0.0.1:{port}"
    with httpx.Client(timeout=5.0, trust_env=False) as client:
        _wait_healthy(client, base, wait_s)
        response = client.post(
            f"{base}/console/v1/launch-code",
            headers={"authorization": f"Bearer {token}"},
        )
        if response.status_code == 401:
            raise LaunchError(
                "the console token is out of date (MeshForge restarted while it was "
                "read); try again"
            )
        if response.status_code != 200:
            raise LaunchError(f"the console refused to open ({response.status_code})")
        url = str(response.json().get("url", ""))
    if not url.startswith(base + "/"):
        raise LaunchError("the console answered with an unexpected address")
    return url


def docker_access() -> int:
    pkexec = shutil.which("pkexec")
    if pkexec is None or not DOCKER_ACCESS_HELPER.is_file():
        _tell(
            "cannot ask for the admin password here. In a terminal run: sudo usermod "
            "-aG docker meshforge && sudo systemctl restart meshforge-agent"
        )
        return int(ExitCode.UNAVAILABLE)
    result = subprocess.run([pkexec, str(DOCKER_ACCESS_HELPER)], check=False)  # noqa: S603
    if result.returncode == 0:
        _tell("Docker access given; MeshForge restarted. Open it again from the menu.")
        return int(ExitCode.OK)
    if result.returncode in (126, 127):  # dismissed / not authorised
        _tell("Docker access was not given (the password dialog was cancelled).")
    else:
        _tell(f"giving Docker access failed (exit {result.returncode}).")
    return int(ExitCode.FAILURE)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog=PROG, description="Open the MeshForge console of this laptop."
    )
    parser.add_argument("--config", type=Path, default=None, help="agent settings file")
    parser.add_argument(
        "--print-url", action="store_true", help="print the URL instead of opening it"
    )
    parser.add_argument(
        "--wait", type=float, default=20.0, help="seconds to wait for the agent"
    )
    parser.add_argument(
        "--docker-access",
        action="store_true",
        help="give the MeshForge service Docker access (asks for an admin password)",
    )
    args = parser.parse_args(argv)
    if args.docker_access:
        raise SystemExit(docker_access())
    try:
        token_path, port = _settings_paths(args.config)
        try:
            url = launch_url(token_path, port, wait_s=max(0.0, args.wait))
        except LaunchError as exc:
            if exc.code == ExitCode.CONFIG_ERROR:  # token not readable
                _reexec_with_group(sys.argv[1:] if argv is None else argv)
            raise
    except LaunchError as exc:
        _tell(str(exc))
        raise SystemExit(int(exc.code)) from None
    if args.print_url:
        sys.stdout.write(url + "\n")
        raise SystemExit(int(ExitCode.OK))
    if not webbrowser.open(url):
        _tell(f"no browser could be opened; open this address within a minute: {url}")
        raise SystemExit(int(ExitCode.FAILURE))
    raise SystemExit(int(ExitCode.OK))


if __name__ == "__main__":
    main()
