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
Windows checks come with the Windows installer (G4/G5): ``--os windows`` says so.

Usage::

    python3 lab/verify_uninstall.py --os linux                  # after apt remove
    sudo python3 lab/verify_uninstall.py --os linux --data-removed   # after purge
    python3 lab/verify_uninstall.py --os linux --docker <container> --json

Run it with ``sudo`` for a complete answer: ``ufw status`` needs root. Without
root the ufw rules are reported as "not checked", never as clean.

Exit codes: 0 nothing left; 1 something left (each item printed); 2 usage error;
3 the checks for this OS do not exist yet.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
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
        print(
            "verify_uninstall: the Windows checks come with the Windows installer "
            "(V0.17 G4/G5)",
            file=sys.stderr,
        )
        return EXIT_NOT_IMPLEMENTED
    if args.docker is None and sys.platform != "linux":
        print(
            "verify_uninstall: --os linux runs on Linux (or use --docker)",
            file=sys.stderr,
        )
        return EXIT_USAGE
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
