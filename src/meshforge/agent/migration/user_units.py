"""The old per-user agent: its systemd user unit and hand-started processes."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from meshforge.agent.migration.model import USER_UNIT_NAME

UnitRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed systemctl argv, a user name from pwd
        list(argv), capture_output=True, text=True, check=False, timeout=30
    )


def stop_user_unit(
    user: str, home: Path | None, runner: UnitRunner = _run
) -> tuple[bool, str] | None:
    """Stop + disable ``user``'s old ``meshforge-agent`` user unit. ``None`` when
    the user has no such unit file; else ``(ok, what happened)``."""
    if home is None:
        return None
    unit_dir = home / ".config" / "systemd" / "user"
    unit = unit_dir / USER_UNIT_NAME
    wanted = unit_dir / "default.target.wants" / USER_UNIT_NAME
    if not (unit.exists() or wanted.is_symlink()):
        return None
    systemctl = shutil.which("systemctl") or "systemctl"
    argv = [systemctl, "--user", "-M", f"{user}@", "disable", "--now", USER_UNIT_NAME]
    try:
        done = runner(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        done = subprocess.CompletedProcess(argv, 1, "", str(exc))
    if done.returncode == 0:
        return True, (
            f"stopped and disabled {user}'s user unit {USER_UNIT_NAME}; its file "
            f"{unit} is left in place (development only from now on)"
        )
    return False, (
        f"could not stop {user}'s user unit {USER_UNIT_NAME} "
        f"({(done.stderr or done.stdout).strip()[:200]}); as {user} run: "
        f"systemctl --user disable --now {USER_UNIT_NAME} - otherwise the two "
        "agents share one identity and the controller keeps only one of them"
    )


def running_user_agents(user_uid: int | None, proc: Path = Path("/proc")) -> list[int]:
    """PIDs of ``meshforge-agent run`` processes of ``user_uid`` (Linux /proc)."""
    if user_uid is None or not proc.is_dir():
        return []
    found: list[int] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != user_uid:
                continue
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        text = b" ".join(argv)
        if b"meshforge-agent" in text and b"run" in argv:
            found.append(int(entry.name))
    return sorted(found)
