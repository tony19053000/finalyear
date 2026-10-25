# ruff: noqa: T201 - a lab/CI driver: fixed Windows tools, its output is the point
"""Check an installed MeshForge agent on Windows, step by step (V0.17 G4, CI + lab).

Run by the ``package-windows`` CI job (``.github/workflows/ci.yml``) from an
elevated prompt on ``windows-latest``, and usable as-is on a lab laptop. Stdlib
only; every probe is a Windows tool with a fixed argument list. Each sub-command
prints what it checked and exits 1 on the first failed check (``FAIL: ...``):

* ``seed``             - a developer agent's state, as a V0.16 laptop has it: a
                         user-mode identity (``meshforge-agent identity show``
                         from the checkout) and the dev scheduled task
                         ``MeshForgeAgent``; writes the node_id to ``--state``;
* ``installed``        - after the installer: setup result ``ok``, service
                         config (virtual account, delayed auto start, recovery,
                         binPath = this version), RUNNING as the virtual account,
                         same node_id, dev task disabled, source renamed, shims +
                         PATH, ``--version`` / ``hardware show`` / ``identity
                         show`` through the shim, console on loopback only, the
                         console token's and the identity's ACLs exactly;
* ``crash``            - the host killed -> the SCM starts a new one;
* ``exit4``            - a configuration the agent refuses -> STOPPED with
                         service exit code 4 and NOT restarted; then repaired;
* ``stop``             - a requested stop: STOPPED, exit 0, no process of the
                         service account left, then started again;
* ``upgraded``         - after the revision-2 installer: binPath and
                         ``current.txt`` name revision 2, same node_id, RUNNING.

``lab/verify_uninstall.py --os windows`` checks the uninstall.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path

SERVICE = "MeshForgeAgent"
ACCOUNT = rf"NT SERVICE\{SERVICE}"
SYSTEM_SID = "S-1-5-18"
ADMINS_SID = "S-1-5-32-544"
DATA = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "MeshForge"
PROGRAM = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "MeshForge"
CONSOLE = "http://127.0.0.1:47130/console/v1/health"


class CheckError(Exception):
    pass


def service_sid(name: str = SERVICE) -> str:
    digest = hashlib.sha1(name.upper().encode("utf-16-le")).digest()  # noqa: S324
    return "S-1-5-80-" + "-".join(str(s) for s in struct.unpack("<5I", digest))


def run(argv: Sequence[str], *, check: bool = True, timeout: float = 300) -> str:
    done = subprocess.run(
        list(argv), capture_output=True, text=True, errors="replace", timeout=timeout
    )
    if check and done.returncode != 0:
        raise CheckError(
            f"{' '.join(argv)} exited {done.returncode}: "
            f"{(done.stdout + done.stderr).strip()[-800:]}"
        )
    return done.stdout


def ps(script: str) -> str:
    return run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])


def ok(text: str) -> None:
    print(f"  ok   {text}")


def expect(condition: bool, text: str) -> None:
    if not condition:
        raise CheckError(text)
    ok(text)


# ------------------------------------------------------------------- service
def query() -> dict[str, str]:
    """``sc.exe queryex`` as a dict (STATE -> RUNNING, PID, exit codes)."""
    out = run(["sc.exe", "queryex", SERVICE], check=False)
    facts: dict[str, str] = {}
    for line in out.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            facts[key.strip()] = value.strip()
    state = facts.get("STATE", "")
    facts["state"] = state.split()[-1] if state else "ABSENT"
    facts["pid"] = facts.get("PID", "0")
    facts["service_exit"] = facts.get("SERVICE_EXIT_CODE", "0").split()[0]
    return facts


def wait_state(state: str, timeout_s: float) -> dict[str, str]:
    deadline = time.monotonic() + timeout_s
    facts = query()
    while facts["state"] != state and time.monotonic() < deadline:
        time.sleep(1)
        facts = query()
    if facts["state"] != state:
        raise CheckError(
            f"{SERVICE} is {facts['state']}, not {state}, after {timeout_s:.0f} s"
        )
    return facts


def wait_console(timeout_s: float = 90) -> None:
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(CONSOLE, timeout=3) as response:
                if response.status == 200:
                    ok(f"console answers on {CONSOLE}")
                    return
        except (urllib.error.URLError, OSError) as exc:
            last = str(exc)
        time.sleep(1)
    raise CheckError(f"the console does not answer on {CONSOLE}: {last}")


def lan_ip() -> str | None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("192.0.2.1", 9))  # no packet is sent (UDP connect)
            ip = str(sock.getsockname()[0])
        except OSError:
            return None
    return None if ip.startswith("127.") else ip


def process_owner(pid: str) -> str:
    out = ps(
        f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}'; "
        "$o = Invoke-CimMethod -InputObject $p -MethodName GetOwner; "
        '"$($o.Domain)\\$($o.User)"'
    )
    return out.strip()


def acl_sids(path: Path) -> dict[str, str]:
    """SID -> rights of every ACE of ``path`` (explicit and inherited)."""
    out = ps(
        f"(Get-Acl -LiteralPath '{path}').Access | ForEach-Object {{ "
        "$_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value "
        '+ "`t" + $_.FileSystemRights + "`t" + $_.AccessControlType }'
    )
    found: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.strip().split("\t")
        if len(parts) == 3:
            if parts[2] != "Allow":
                raise CheckError(f"{path} has a {parts[2]} ACE for {parts[0]}")
            found[parts[0]] = parts[1]
    return found


def current_sid() -> str:
    rows = list(csv.reader(io.StringIO(run(["whoami", "/user", "/fo", "csv", "/nh"]))))
    return rows[0][1]


def node_id_of(identity_dir: Path) -> str:
    return str(
        json.loads((identity_dir / "node.json").read_text(encoding="utf-8"))["node_id"]
    )


def shim(*args: str) -> str:
    return run([str(PROGRAM / "bin" / "meshforge-agent.cmd"), *args])


# ------------------------------------------------------------------ commands
def cmd_seed(state: Path) -> None:
    local = Path(os.environ["LOCALAPPDATA"]) / "MeshForge" / "identity"
    expect((local / "node.json").is_file(), f"a user-mode identity exists in {local}")
    node = node_id_of(local)
    run(
        [
            "schtasks",
            "/Create",
            "/TN",
            SERVICE,
            "/TR",
            "cmd.exe /c exit 0",
            "/SC",
            "ONLOGON",
            "/F",
        ]
    )
    ok(f"dev scheduled task \\{SERVICE} registered")
    state.write_text(json.dumps({"node_id": node}), encoding="utf-8")
    ok(f"seeded node_id {node}")


def _setup_result() -> dict[str, object]:
    path = DATA / "logs" / "setup-result.json"
    result = json.loads(path.read_text(encoding="utf-8"))
    print("  setup said:\n    " + "\n    ".join(map(str, result.get("lines", []))))
    return dict(result)


def cmd_installed(state: Path, version_dir: str) -> None:
    expected = json.loads(state.read_text(encoding="utf-8"))["node_id"]
    result = _setup_result()
    expect(
        result.get("outcome") == "ok", f"setup outcome is ok ({result.get('outcome')})"
    )
    qc = run(["sc.exe", "qc", SERVICE])
    expect(
        re.search(r"AUTO_START\s+\(DELAYED\)", qc) is not None,
        "start type: Automatic (Delayed)",
    )
    expect(ACCOUNT.lower() in qc.lower(), f"runs as {ACCOUNT}")
    expect(
        f"\\{version_dir}\\python\\python.exe".lower() in qc.lower(),
        f"binPath = {version_dir}",
    )
    failure = run(["sc.exe", "qfailure", SERVICE])
    expect(failure.count("RESTART -- Delay = 5000") == 3, "recovery: restart x3 / 5 s")
    facts = wait_state("RUNNING", 60)
    owner = process_owner(facts["pid"])
    expect(owner.lower() == ACCOUNT.lower(), f"host process owner is {owner}")
    expect(
        node_id_of(DATA / "identity") == expected,
        f"same node_id after migration ({expected})",
    )
    migrated = list(
        (Path(os.environ["LOCALAPPDATA"]) / "MeshForge").glob("identity.migrated-*")
    )
    expect(bool(migrated), f"source renamed ({[m.name for m in migrated]})")
    task = run(["schtasks", "/Query", "/TN", SERVICE, "/FO", "CSV", "/V", "/NH"])
    expect("disabled" in task.lower(), "dev scheduled task is disabled")
    expect(
        (PROGRAM / "current.txt").read_text().strip() == version_dir,
        "current.txt names this version",
    )
    machine_path = ps("[Environment]::GetEnvironmentVariable('Path', 'Machine')")
    expect(
        str(PROGRAM / "bin").lower() in machine_path.lower(),
        "machine PATH has the shims",
    )
    version = shim("--version")
    expect("bundle" in version, f"meshforge-agent --version: {version.strip()}")
    run([str(PROGRAM / "bin" / "meshforge-agent.cmd"), "hardware", "show"])
    ok("hardware show (elevated, system mode) exit 0")
    shown = json.loads(shim("identity", "show", "--json"))
    expect(
        shown["node_id"] == expected, "identity show (system mode) = the service's node"
    )
    expect(
        node_id_of(DATA / "identity") == expected
        and wait_state("RUNNING", 5)["pid"] == facts["pid"],
        "the admin command did not disturb the service",
    )
    wait_console()
    ip = lan_ip()
    if ip is not None:
        try:
            socket.create_connection((ip, 47130), timeout=3).close()
            raise CheckError(f"the console answers on the LAN address {ip}:47130")
        except OSError:
            ok(f"console NOT reachable on the LAN address {ip}:47130")
    token = DATA / "console" / "token"
    sids = acl_sids(token)
    svc = service_sid()
    me = current_sid()
    expect(
        set(sids) == {SYSTEM_SID, ADMINS_SID, svc, me},
        f"token ACL SIDs exactly {sorted(sids)}",
    )
    expect(
        "Modify" in sids[svc] or "FullControl" in sids[svc],
        "service may write the token",
    )
    expect(
        "Write" not in sids[me] and "Modify" not in sids[me],
        f"installing user reads only ({sids[me]})",
    )
    key = DATA / "identity" / "node.key"
    expect(
        set(acl_sids(key)) <= {SYSTEM_SID, ADMINS_SID, svc},
        "node.key: service + SYSTEM + Administrators only",
    )


def cmd_crash() -> None:
    before = wait_state("RUNNING", 60)["pid"]
    run(["taskkill", "/F", "/PID", before])
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        facts = query()
        if facts["state"] == "RUNNING" and facts["pid"] not in ("0", before):
            ok(f"crash: SCM started a new host ({before} -> {facts['pid']})")
            wait_console()
            return
        time.sleep(1)
    raise CheckError("the killed service was not restarted within 90 s")


def cmd_exit4() -> None:
    config = DATA / "config" / "config.toml"
    backup = config.read_bytes() if config.exists() else None
    config.write_text("[agent]\nno_such_key = 1\n", encoding="utf-8")
    try:
        run(["sc.exe", "stop", SERVICE], check=False)
        wait_state("STOPPED", 90)
        run(["sc.exe", "start", SERVICE])
        facts = wait_state("STOPPED", 120)
        expect(
            facts["service_exit"] == "4",
            f"stopped with service exit code 4 ({facts['service_exit']})",
        )
        time.sleep(25)
        expect(query()["state"] == "STOPPED", "still STOPPED 25 s later: not restarted")
    finally:
        if backup is None:
            config.unlink(missing_ok=True)
        else:
            config.write_bytes(backup)
    run(["sc.exe", "start", SERVICE])
    wait_state("RUNNING", 60)
    wait_console()
    ok("repaired: RUNNING again")


def cmd_stop() -> None:
    wait_state("RUNNING", 60)
    run(["sc.exe", "stop", SERVICE], check=False)
    facts = wait_state("STOPPED", 90)
    expect(
        facts.get("WIN32_EXIT_CODE", "0").split()[0] == "0",
        "requested stop: exit code 0",
    )
    left = ps(
        "Get-CimInstance Win32_Process | ForEach-Object { $o = Invoke-CimMethod "
        "-InputObject $_ -MethodName GetOwner; if ($o.User -eq 'MeshForgeAgent') "
        "{ $_.ProcessId } }"
    ).split()
    expect(not left, f"no process of {ACCOUNT} left ({left})")
    run(["sc.exe", "start", SERVICE])
    wait_state("RUNNING", 60)
    wait_console()


def cmd_upgraded(state: Path, version_dir: str) -> None:
    expected = json.loads(state.read_text(encoding="utf-8"))["node_id"]
    result = _setup_result()
    expect(
        result.get("outcome") == "ok",
        f"upgrade outcome is ok ({result.get('outcome')})",
    )
    expect(bool(result.get("previous_python")), "the upgrade knew the previous version")
    qc = run(["sc.exe", "qc", SERVICE])
    expect(
        f"\\{version_dir}\\python\\python.exe".lower() in qc.lower(),
        f"binPath = {version_dir}",
    )
    expect(
        (PROGRAM / "current.txt").read_text().strip() == version_dir,
        "current.txt = new version",
    )
    wait_state("RUNNING", 60)
    expect(node_id_of(DATA / "identity") == expected, "same node_id after the upgrade")
    wait_console()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "command", choices=["seed", "installed", "crash", "exit4", "stop", "upgraded"]
    )
    parser.add_argument(
        "--state", type=Path, default=Path("reports/windows-state.json")
    )
    parser.add_argument("--version-dir", default="")
    args = parser.parse_args(argv)
    print(f"package_check {args.command}")
    try:
        if args.command == "seed":
            cmd_seed(args.state)
        elif args.command == "installed":
            cmd_installed(args.state, args.version_dir)
        elif args.command == "crash":
            cmd_crash()
        elif args.command == "exit4":
            cmd_exit4()
        elif args.command == "stop":
            cmd_stop()
        else:
            cmd_upgraded(args.state, args.version_dir)
    except (
        CheckError,
        OSError,
        ValueError,
        KeyError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"  FAIL {exc}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
