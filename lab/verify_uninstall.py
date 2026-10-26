# ruff: noqa: T201 - a lab tool: its output is the point
"""Check that an uninstalled MeshForge left nothing behind (V0.17 G1 first version; D393).

Criterion 2 of V0.17 ("uninstall leaves no services or firewall rules") needs a
verifier, not a promise. This script lists every trace a package install can
leave and fails on each one that should be gone:

* **always** (``apt remove`` and ``apt purge``): no ``meshforge*`` systemd unit
  (file, enablement link, drop-in, or a unit systemd still has loaded - e.g. one
  left ``failed``), no MeshForge process, no program files (``/opt/meshforge``,
  ``/usr/bin/meshforge-agent``, ``/usr/bin/compute``), no ufw rule or app profile
  named MeshForge, the dpkg package not installed (``config-files`` is the
  normal state after ``remove``);
* **with** ``--data-removed`` (after ``apt purge``): also no ``/var/lib/meshforge``,
  no ``/etc/meshforge``, no ``meshforge`` user, no ``meshforge`` /
  ``meshforge-admin`` group, and the package fully forgotten by dpkg.

Kept on purpose and only *reported*: the data after a plain ``remove`` (D393:
keep data by default) and a developer's old systemd **user** unit file in a home
directory (the migration disables it but never deletes files in a home, D387).

Everything is read by one POSIX ``sh`` probe (no Python needed on the target), run
here or inside a container (``--docker NAME``, used by the package tests and CI).

**Windows** (``--os windows``, V0.17 G4): one PowerShell probe, the same
verdict rules. Always: no ``*MeshForge*`` service, no MeshForge process, no
``*MeshForge*`` scheduled task (the development task included), no
``*MeshForge*`` firewall rule, no ``%ProgramFiles%\\MeshForge``, no Start-menu
shortcut, no uninstall entry, no MeshForge PATH entry, the service account not
left in ``docker-users``. With ``--data-removed``: no ``%ProgramData%\\MeshForge``.
Kept on purpose and only reported: the data after a default uninstall, and a
developer's migrated ``identity.migrated-*`` folders in user profiles. Needs an
elevated prompt for a complete answer (firewall rules and other users'
profiles); without it the result says "not elevated" and is never "clean".

Usage::

    python3 lab/verify_uninstall.py --os linux                  # after apt remove
    sudo python3 lab/verify_uninstall.py --os linux --data-removed   # after purge
    python3 lab/verify_uninstall.py --os linux --docker <container> --json

Run it with ``sudo`` for a complete answer: ``ufw status`` needs root. Without
root the ufw rules are reported as "not checked", never as clean.

Exit codes: 0 nothing left; 1 something left (each item printed); 2 usage error
(also ``--os windows`` off Windows); 3 is no longer returned (it meant "no
Windows checks yet" before V0.17 G4).
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Final

EXIT_CLEAN: Final = 0
EXIT_LEFTOVERS: Final = 1
EXIT_USAGE: Final = 2
EXIT_NOT_IMPLEMENTED: Final = 3

PACKAGE: Final = "meshforge-agent"
PROGRAM_PATHS: Final = (
    "/opt/meshforge",
    "/usr/bin/meshforge-agent",
    "/usr/bin/compute",
    # V0.17 G3: the local console's launcher, menu entry, icon, helper, token folder
    "/usr/bin/meshforge-console-open",
    "/usr/share/applications/meshforge.desktop",
    "/usr/share/icons/hicolor/scalable/apps/meshforge.svg",
    "/usr/lib/meshforge",
    "/usr/share/polkit-1/actions/org.meshforge.docker-access.policy",
    "/usr/lib/tmpfiles.d/meshforge-agent.conf",
    "/run/meshforge-console",
)
UNIT_PATHS: Final = (
    "/usr/lib/systemd/system/meshforge-agent.service",
    "/usr/lib/systemd/system/meshforge-agent.path",
    "/lib/systemd/system/meshforge-agent.service",
    "/lib/systemd/system/meshforge-agent.path",
    "/etc/systemd/system/meshforge-agent.service",
    "/etc/systemd/system/meshforge-agent.path",
    "/etc/systemd/system/meshforge-agent.service.d",
)
DATA_PATHS: Final = ("/var/lib/meshforge", "/etc/meshforge")

# One probe, POSIX sh + coreutils + systemctl/dpkg-query when present. Each line
# is "<kind>\t<value>"; nothing else is printed. $$ is the probe shell itself.
# The first line is a marker: the probe's own shells (and their subshells) carry
# the script in their command line, and the process scan skips that marker.
PROBE: Final = r"""# verify_uninstall probe

p() { printf '%s\t%s\n' "$1" "$2"; }
for path in __PROGRAM__; do [ -e "$path" ] || [ -L "$path" ] && p program "$path"; done
for path in __UNITS__; do
	# merged /usr (/lib -> usr/lib): report each file once
	case "$path" in /lib/*) [ -L /lib ] && continue ;; esac
	[ -e "$path" ] || [ -L "$path" ] && p unit_file "$path"
done
for path in /etc/systemd/system/*.wants/meshforge* /usr/lib/systemd/system/*.wants/meshforge*; do
	[ -e "$path" ] || [ -L "$path" ] && p unit_file "$path"
done
for path in __DATA__; do [ -e "$path" ] && p data "$path"; done
for path in /etc/ufw/applications.d/*; do
	[ -f "$path" ] && grep -qi meshforge "$path" 2>/dev/null && p ufw_profile "$path"
done
for path in /home/*/.config/systemd/user/meshforge-agent.service \
	/root/.config/systemd/user/meshforge-agent.service; do
	[ -e "$path" ] && p user_unit "$path"
done
if command -v getent >/dev/null 2>&1; then
	getent passwd meshforge >/dev/null 2>&1 && p account passwd:meshforge
	getent group meshforge >/dev/null 2>&1 && p account group:meshforge
	getent group meshforge-admin >/dev/null 2>&1 && p account group:meshforge-admin
fi
if command -v dpkg-query >/dev/null 2>&1; then
	status=$(dpkg-query -W -f='${db:Status-Status}' __PACKAGE__ 2>/dev/null || true)
	p dpkg "${status:-absent}"
else
	p dpkg no-dpkg
fi
if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
	systemctl list-units --all --no-legend --plain 'meshforge*' 2>/dev/null |
		while read -r unit rest; do [ -n "$unit" ] && p unit_loaded "$unit $rest"; done
	systemctl list-unit-files --no-legend --plain 'meshforge*' 2>/dev/null |
		while read -r unit rest; do [ -n "$unit" ] && p unit_known "$unit $rest"; done
else
	p systemd absent
fi
for dir in /proc/[0-9]*; do
	pid=${dir#/proc/}
	[ "$pid" = "$$" ] && continue
	cmd=$(tr '\0' ' ' < "$dir/cmdline" 2>/dev/null) || continue
	case "$cmd" in
	*verify_uninstall*) ;;
	*/opt/meshforge/* | *meshforge-agent* | *meshforge-controller*) p process "$pid $cmd" ;;
	esac
done
if command -v ufw >/dev/null 2>&1; then
	if [ "$(id -u)" = 0 ]; then
		ufw status 2>/dev/null | grep -i meshforge | while read -r line; do p ufw_rule "$line"; done
		p ufw checked
	else
		p ufw not-root
	fi
else
	p ufw absent
fi
"""


WINDOWS_APP_ID: Final = "{A8774C05-C367-491D-833B-4979498B0BEE}"
"""packaging/windows/meshforge-agent.iss AppId (its uninstall key is <id>_is1)."""
WINDOWS_SERVICE: Final = "MeshForgeAgent"


def windows_service_sid(name: str = WINDOWS_SERVICE) -> str:
    """``NT SERVICE\\<name>``'s SID (same derivation as
    ``meshforge.common.windows_service``; stdlib only here)."""
    import hashlib
    import struct

    digest = hashlib.sha1(name.upper().encode("utf-16-le")).digest()  # noqa: S324
    return "S-1-5-80-" + "-".join(str(s) for s in struct.unpack("<5I", digest))


WINDOWS_PROBE_FILE: Final = Path(__file__).with_name("verify_uninstall_windows.ps1")
"""The PowerShell probe (a file: PowerShell lines are long); placeholders
``__APPID__`` / ``__SERVICE__`` / ``__SID__`` are filled in here."""


def windows_probe_script() -> str:
    return (
        WINDOWS_PROBE_FILE.read_text(encoding="utf-8")
        .replace("__APPID__", WINDOWS_APP_ID)
        .replace("__SERVICE__", WINDOWS_SERVICE)
        .replace("__SID__", windows_service_sid())
    )


def evaluate_windows(
    facts: Sequence[tuple[str, str]], *, data_removed: bool
) -> Verdict:
    """The Windows verdict on a probe's facts (pure; unit-tested)."""
    verdict = Verdict(os="windows", data_removed=data_removed)
    seen = {kind for kind, _ in facts}
    if "done" not in seen:
        verdict.leftovers.append("the probe did not finish: nothing was checked")
    labels = {
        "service": "service still installed",
        "process": "process still running",
        "task": "scheduled task left",
        "firewall": "firewall rule left",
        "program": "program file left",
        "uninstall_entry": "uninstall entry left",
        "path": "PATH entry left",
        "docker_member": "service account still in docker-users",
    }
    for kind, value in facts:
        if kind in labels:
            verdict.leftovers.append(f"{labels[kind]}: {value[:200]}")
        elif kind == "elevated" and value.strip().lower() != "true":
            verdict.leftovers.append(
                "not elevated: firewall rules were not checked (run it as "
                "administrator)"
            )
        elif kind == "data":
            if data_removed:
                verdict.leftovers.append(f"data left after remove-all-data: {value}")
            else:
                verdict.kept.append(
                    f"data kept (the default uninstall keeps it): {value}"
                )
        elif kind == "user_identity":
            verdict.kept.append(
                f"migrated developer identity (kept on purpose, never used): {value}"
            )
    return verdict


def run_windows_probe() -> str:
    try:
        done = subprocess.run(
            [  # noqa: S607 - Windows built-in
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                windows_probe_script(),
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"verify_uninstall: the probe could not run: {exc}") from None
    return done.stdout


def probe_script() -> str:
    return (
        PROBE.replace("__PROGRAM__", " ".join(PROGRAM_PATHS))
        .replace("__UNITS__", " ".join(UNIT_PATHS))
        .replace("__DATA__", " ".join(DATA_PATHS))
        .replace("__PACKAGE__", PACKAGE)
    )


@dataclass
class Verdict:
    os: str
    data_removed: bool
    leftovers: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.leftovers


def parse_probe(text: str) -> list[tuple[str, str]]:
    facts: list[tuple[str, str]] = []
    for line in text.splitlines():
        kind, sep, value = line.partition("\t")
        if sep:
            facts.append((kind, value.strip()))
    return facts


def evaluate(facts: Sequence[tuple[str, str]], *, data_removed: bool) -> Verdict:
    """The verdict on a probe's facts (pure; unit-tested)."""
    verdict = Verdict(os="linux", data_removed=data_removed)
    for kind, value in facts:
        if kind == "program":
            verdict.leftovers.append(f"program file left: {value}")
        elif kind == "unit_file":
            verdict.leftovers.append(f"systemd unit file left: {value}")
        elif kind in ("unit_loaded", "unit_known"):
            verdict.leftovers.append(f"systemd still knows the unit: {value}")
        elif kind == "process":
            verdict.leftovers.append(f"process still running: {value[:200]}")
        elif kind == "ufw_profile":
            verdict.leftovers.append(f"ufw app profile left: {value}")
        elif kind == "ufw_rule":
            verdict.leftovers.append(f"ufw rule left: {value}")
        elif kind == "ufw" and value == "not-root":
            verdict.notes.append("ufw rules not checked (run with sudo)")
        elif kind == "systemd" and value == "absent":
            verdict.notes.append(
                "systemd is not running here: loaded units not checked"
            )
        elif kind == "user_unit":
            verdict.kept.append(
                f"old development user unit file (kept on purpose, disabled by the "
                f"migration): {value}"
            )
        elif kind == "data":
            if data_removed:
                verdict.leftovers.append(f"data left after purge: {value}")
            else:
                verdict.kept.append(f"data kept (remove keeps data): {value}")
        elif kind == "account":
            if data_removed:
                verdict.leftovers.append(f"account left after purge: {value}")
            else:
                verdict.kept.append(f"service account kept: {value}")
        elif kind == "dpkg":
            if value in ("absent", "not-installed", "no-dpkg"):
                continue
            if value == "config-files" and not data_removed:
                verdict.kept.append("dpkg: config-files (purge removes it)")
            else:
                verdict.leftovers.append(f"dpkg package state: {value}")
    return verdict


def run_probe(docker: str | None) -> str:
    script = probe_script()
    argv = (
        ["docker", "exec", docker, "sh", "-c", script]
        if docker
        else ["sh", "-c", script]
    )
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"verify_uninstall: the probe could not run: {exc}") from None
    if done.returncode != 0:
        raise SystemExit(
            f"verify_uninstall: the probe failed (exit {done.returncode}): "
            f"{done.stderr.strip()[-500:]}"
        )
    return done.stdout


def render(verdict: Verdict) -> str:
    lines = [
        f"MeshForge uninstall check ({verdict.os}, "
        f"{'purge: data must be gone' if verdict.data_removed else 'remove: data may stay'})"
    ]
    lines += [f"  LEFT   {item}" for item in verdict.leftovers]
    lines += [f"  kept   {item}" for item in verdict.kept]
    lines += [f"  note   {item}" for item in verdict.notes]
    lines.append(
        "CLEAN: nothing left behind"
        if verdict.clean
        else f"NOT CLEAN: {len(verdict.leftovers)} item(s) left behind"
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--os", dest="os_name", required=True, choices=["linux", "windows"]
    )
    parser.add_argument(
        "--data-removed",
        action="store_true",
        help="the uninstall removed data too (apt purge): data and accounts must be gone",
    )
    parser.add_argument(
        "--docker", metavar="CONTAINER", help="check inside this container"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_CLEAN
    if args.os_name == "windows":
        if platform.system() != "Windows":  # not sys.platform: mypy reachability
            print("verify_uninstall: --os windows runs on Windows", file=sys.stderr)
            return EXIT_USAGE
        verdict = evaluate_windows(
            parse_probe(run_windows_probe()), data_removed=args.data_removed
        )
    elif args.docker is None and sys.platform != "linux":
        print(
            "verify_uninstall: --os linux runs on Linux (or use --docker)",
            file=sys.stderr,
        )
        return EXIT_USAGE
    else:
        verdict = evaluate(
            parse_probe(run_probe(args.docker)), data_removed=args.data_removed
        )
    if args.json:
        print(json.dumps({**asdict(verdict), "clean": verdict.clean}, indent=1))
    else:
        print(render(verdict))
    return EXIT_CLEAN if verdict.clean else EXIT_LEFTOVERS


if __name__ == "__main__":
    sys.exit(main())
