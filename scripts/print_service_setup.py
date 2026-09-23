#!/usr/bin/env python3
"""Print the commands that install MeshForge as a dev background service.

**This script only prints** (roadmap 0.3.13, decision D17). It never copies a
file, never edits a service manager, never starts a process, and it does not need
a controller or an agent to be running - so it is safe to run anywhere, including
on a laptop that has nothing set up yet. Read the output, then run the parts you
want yourself.

Usage::

    uv run python scripts/print_service_setup.py            # this platform
    uv run python scripts/print_service_setup.py linux
    uv run python scripts/print_service_setup.py windows
    uv run python scripts/print_service_setup.py uninstall

The unit files and the task XML it refers to live in ``deploy/``; the prose, the
trust boundary and the troubleshooting live in ``docs/OPERATIONS.md``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UNIT_DIR = "~/.config/systemd/user"
WINDOWS_PLACEHOLDER = r"C:\path\to\meshforge"
"""Printed instead of this checkout when the Windows commands are read on Linux -
a POSIX path in a ``cmd.exe`` command line would be quietly wrong."""


def _posix_repo() -> str:
    """This checkout as a POSIX path. Templates quote it - laptop paths contain
    spaces (this repository's own does)."""
    return str(REPO)


def _windows_repo() -> str:
    return str(REPO) if sys.platform.startswith("win") else WINDOWS_PLACEHOLDER


LINUX_TEMPLATE = """
# ---------------------------------------------------------------- Linux (systemd user)
# 1. Copy the unit(s) you want. Edit WorkingDirectory in each to this checkout:
#      {repo}
mkdir -p {unit_dir}
cp "{repo}/deploy/systemd/meshforge-agent.service" {unit_dir}/
cp "{repo}/deploy/systemd/meshforge-controller.service" {unit_dir}/   # controller laptop only
$EDITOR {unit_dir}/meshforge-agent.service

# 2. Register and start it.
systemctl --user daemon-reload
systemctl --user enable --now meshforge-agent

# 3. Make it survive logout and start at boot (this is the step people forget):
loginctl enable-linger "$USER"

# 4. Check it.
systemctl --user status meshforge-agent
journalctl --user -u meshforge-agent -f
curl -sS http://127.0.0.1:47110/healthz && echo
uv run compute status
""".strip()

WINDOWS_TEMPLATE = r"""
# ------------------------------------------------------- Windows (Task Scheduler)
# Run these in cmd.exe. `where uv` gives the uv.exe path used below.
# One line, quoted, runs the agent at every logon of the current user:

set MF_LOG=%LOCALAPPDATA%\MeshForge\agent.log
if not exist "%LOCALAPPDATA%\MeshForge" mkdir "%LOCALAPPDATA%\MeshForge"
schtasks /create /tn MeshForgeAgent /sc onlogon /rl LIMITED ^
  /tr "cmd /c cd /d \"{repo}\" && uv run meshforge-agent run >> \"%MF_LOG%\" 2>&1"

# Start it now without logging off, then inspect it:
schtasks /run   /tn MeshForgeAgent
schtasks /query /tn MeshForgeAgent /v /fo LIST

# Output goes to the file named in the command line above:
#   %LOCALAPPDATA%\MeshForge\agent.log
# Check the fabric:
curl http://127.0.0.1:47110/healthz
uv run compute status

# The XML template (more settings: restart on failure, no time limit) is
#   {repo}\deploy\windows\meshforge-agent-task.xml
# Edit the three marked fields, then:
schtasks /create /tn MeshForgeAgent ^
  /xml "{repo}\deploy\windows\meshforge-agent-task.xml"

# NOTE: whether an /sc onlogon task survives *logoff* is UNVERIFIED for this
# project - see docs/OPERATIONS.md. If it does not and you need it to, NSSM is the
# documented fallback; the agent stays a native Windows process either way
# (ADR-0009).
""".strip()

UNINSTALL_TEMPLATE = """
# ------------------------------------------------------------------------ uninstall
# Linux:
systemctl --user disable --now meshforge-agent
systemctl --user disable --now meshforge-controller
rm -f {unit_dir}/meshforge-agent.service {unit_dir}/meshforge-controller.service
systemctl --user daemon-reload
loginctl disable-linger "$USER"        # only if nothing else of yours needs linger

# Windows (cmd.exe):
schtasks /end    /tn MeshForgeAgent
schtasks /delete /tn MeshForgeAgent /f

# Neither removes your data: the node identity stays in
#   Linux   ~/.local/share/meshforge/identity
#   Windows %LOCALAPPDATA%\\MeshForge\\identity
# Delete it only if you want this laptop to enrol as a brand new node
# (`meshforge-agent identity reset` is the supported way).
""".strip()

HEADER = """
# MeshForge dev background service - COMMANDS ONLY, NOTHING WAS INSTALLED.
# Read them, then run what you want. Full notes: docs/OPERATIONS.md
# Trust boundary: the REST surface (port 47110) is read-only and UNAUTHENTICATED
# in V0.3 - keep it on loopback or a trusted LAN. Authentication lands in V0.7.
""".strip()


def sections() -> dict[str, str]:
    """Render the three blocks; every interpolated path is quoted."""
    return {
        "linux": LINUX_TEMPLATE.format(repo=_posix_repo(), unit_dir=UNIT_DIR),
        "windows": WINDOWS_TEMPLATE.format(repo=_windows_repo()),
        "uninstall": UNINSTALL_TEMPLATE.format(repo=_posix_repo(), unit_dir=UNIT_DIR),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "platform",
        nargs="?",
        choices=("linux", "windows", "uninstall", "all"),
        help="Which commands to print (default: this platform).",
    )
    args = parser.parse_args(argv)
    choice = args.platform or ("windows" if sys.platform.startswith("win") else "linux")

    blocks = sections()
    print(HEADER)
    print()
    for name in blocks if choice == "all" else (choice,):
        print(blocks[name])
        print()
    if choice != "uninstall":
        print(
            "# Removing it again: "
            "uv run python scripts/print_service_setup.py uninstall"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
