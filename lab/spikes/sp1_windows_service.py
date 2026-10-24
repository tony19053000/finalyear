# ruff: noqa: E402 - the platform assert must precede the pywin32 imports
"""Spike SP1 (V0.17 G0, roadmap 0.17.0): the agent as a native Windows service.

Answers D386's question - *does a pywin32 service under ``NT SERVICE\\MeshForgeAgent``
work for the agent?* - with a real service on a real Windows machine (CI runner
or a team laptop, **elevated** prompt)::

    uv sync --extra agent
    uv pip install pywin32==312
    uv run python lab/spikes/sp1_windows_service.py prepare    # bundle-like python
    uv run python lab/spikes/sp1_windows_service.py run --out sp1-results.json

``prepare`` builds a D384-like interpreter (a copy of the uv-managed CPython 3.12
with the locked ``agent`` wheels + pywin32 + meshforge installed into it, no venv)
under ``C:\\Program Files\\MeshForge\\sp1`` - the service runs from there, never
from the checkout. ``run`` then checks, each as one phase with evidence:

install · service config (account, delayed auto start, restart x3 then none,
non-crash flag off, service SID) · state-folder ACLs · named firewall rule
(Private/Domain) · RUNNING as the virtual account · graceful stop · crash ->
SCM restart · exit 4 -> no restart · probe inside the service (whoami, session 0,
GPU/Docker report, ``uv venv`` build, nested job tree) · stop kills the whole
tree · uninstall leaves no service, no MeshForge firewall rule, no process.

Writes one JSON (``--out``), prints a table (also to ``$GITHUB_STEP_SUMMARY``).
Exit codes: ``0`` every phase passed; ``3`` a phase failed (see the JSON);
``2`` usage. Changes machine state: run it on a CI runner or a laptop you may
install a service on; ``run`` always tries to uninstall at the end.
"""

from __future__ import annotations

import sys

assert sys.platform == "win32", "SP1 runs on Windows (elevated)"  # noqa: S101

import argparse
import contextlib
import json
import os
import platform
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any, Final

import psutil
import pywintypes
import win32api
import win32con
import win32file
import win32security
import win32service
import win32serviceutil

from meshforge.agent import winservice as ws

PYWIN32: Final = "312"
BUNDLE_ROOT: Final = Path(r"C:\Program Files\MeshForge\sp1")
BUNDLE_PY: Final = BUNDLE_ROOT / "python" / "python.exe"
PROBE_DIR: Final = BUNDLE_ROOT / "probe"
LAYOUT: Final = ws.machine_layout(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
GRACE_S: Final = 20.0
ACCOUNT: Final = ws.SERVICE_ACCOUNT.lower()
_INHERITED_ACE: Final = 0x10
_LAB_DIR: Final = Path(__file__).resolve().parents[1]
_REPO: Final = _LAB_DIR.parent

GOOD_CONFIG: Final = f"""\
data_dir = "{Path(LAYOUT.root).as_posix()}"
[security]
mode = "insecure-dev"
[agent]
controller_address = "127.0.0.1:47100"
[agent.scores]
enabled = false
[agent.envs]
dir = "{Path(LAYOUT.cache_dir, "envs").as_posix()}"
"""
# insecure-dev with a non-loopback controller: the agent refuses it -> exit 4.
BAD_CONFIG: Final = GOOD_CONFIG.replace("127.0.0.1:47100", "10.255.255.1:47100")


# ------------------------------------------------------------------- utils
def _run(
    argv: list[str], *, timeout: float = 300, cwd: Path | None = None
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        r = subprocess.run(
            argv, capture_output=True, timeout=timeout, check=False, cwd=cwd
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"argv": argv, "rc": None, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "argv": argv,
        "rc": r.returncode,
        "s": round(time.monotonic() - started, 2),
        "out": r.stdout.decode("utf-8", "replace")[-6000:],
        "err": r.stderr.decode("utf-8", "replace")[-3000:],
    }


def _must(res: dict[str, Any]) -> dict[str, Any]:
    if res.get("rc") != 0:
        raise RuntimeError(f"command failed: {json.dumps(res)[:3000]}")
    return res


def _ps(script: str) -> dict[str, Any]:
    return _run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=120
    )


@dataclass
class Phase:
    name: str
    ok: bool | None = None
    detail: str = ""
    seconds: float = 0.0
    evidence: dict[str, Any] = field(default_factory=dict)


PHASES: list[Phase] = []


@contextlib.contextmanager
def phase(name: str) -> Iterator[Phase]:
    p = Phase(name)
    PHASES.append(p)
    started = time.monotonic()
    print(f"--- {name}", flush=True)  # noqa: T201
    try:
        yield p
    except Exception as exc:  # noqa: BLE001 - a failed phase is a result, not a crash
        p.ok = False
        p.detail = f"{type(exc).__name__}: {exc}"[:2000]
    finally:
        p.seconds = round(time.monotonic() - started, 1)
        if p.ok is None:
            p.ok = False
            p.detail = p.detail or "phase did not decide"
        print(f"    -> {'PASS' if p.ok else 'FAIL'} {p.detail[:300]}", flush=True)  # noqa: T201


def _decide(p: Phase, checks: dict[str, bool]) -> None:
    p.evidence["checks"] = checks
    failed = [k for k, v in checks.items() if not v]
    p.ok = not failed
    p.detail = "all checks passed" if not failed else "failed: " + ", ".join(failed)


def _wait(cond: Callable[[], bool], timeout_s: float, step: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(step)
    return cond()


# --------------------------------------------------------------------- SCM
def _status() -> dict[str, Any] | None:
    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    try:
        try:
            h = win32service.OpenService(
                scm, ws.SERVICE_NAME, win32service.SERVICE_QUERY_STATUS
            )
        except pywintypes.error as exc:
            if exc.winerror == 1060:  # does not exist
                return None
            raise
        try:
            status: Any = win32service.QueryServiceStatusEx(h)  # a dict at runtime
            return dict(status)
        finally:
            win32service.CloseServiceHandle(h)
    finally:
        win32service.CloseServiceHandle(scm)


def _state() -> int | None:
    st = _status()
    return None if st is None else int(st["CurrentState"])


def _wait_state(state: int, timeout_s: float) -> bool:
    return _wait(lambda: _state() == state, timeout_s)


def _start() -> None:
    win32serviceutil.StartService(ws.SERVICE_NAME)
    if not _wait_state(win32service.SERVICE_RUNNING, 60):
        raise RuntimeError(f"not RUNNING after start: {_status()}")


def _stop(timeout_s: float = GRACE_S + 40) -> None:
    if _state() not in (None, win32service.SERVICE_STOPPED):
        with contextlib.suppress(pywintypes.error):
            win32serviceutil.StopService(ws.SERVICE_NAME)
    if (
        not _wait_state(win32service.SERVICE_STOPPED, timeout_s)
        and _state() is not None
    ):
        raise RuntimeError(f"not STOPPED after stop: {_status()}")


def _set_bin_path(bin_path: str) -> None:
    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)
    try:
        h = win32service.OpenService(
            scm, ws.SERVICE_NAME, win32service.SERVICE_CHANGE_CONFIG
        )
        nc = win32service.SERVICE_NO_CHANGE
        try:
            win32service.ChangeServiceConfig(
                h, nc, nc, nc, bin_path, None, False, None, None, None, None
            )
        finally:
            win32service.CloseServiceHandle(h)
    finally:
        win32service.CloseServiceHandle(scm)


def _service_procs() -> list[dict[str, Any]]:
    found = []
    for proc in psutil.process_iter(["pid", "name", "username"]):
        user = (proc.info.get("username") or "").lower()
        if user == ACCOUNT:
            found.append({"pid": proc.info["pid"], "name": proc.info["name"]})
    return found


def _tree(pid: int) -> list[dict[str, Any]]:
    try:
        root = psutil.Process(pid)
        procs = [root, *root.children(recursive=True)]
    except psutil.NoSuchProcess:
        return []
    out = []
    for p in procs:
        with contextlib.suppress(psutil.Error):
            out.append({"pid": p.pid, "name": p.name(), "user": p.username(),
                        "cmd": " ".join(p.cmdline())[:300]})  # fmt: skip
    return out


def _alive(pids: list[int]) -> list[int]:
    return [p for p in pids if psutil.pid_exists(p)]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"<unreadable: {exc}>"


def _log_tail(name: str, n: int = 40) -> list[str]:
    return _read(Path(LAYOUT.logs_dir, name)).splitlines()[-n:]


def _last(text: str, marker: str) -> str:
    hits = [line for line in text.splitlines() if marker in line]
    return hits[-1] if hits else ""


def _write_config(text: str) -> None:
    Path(LAYOUT.config_file).write_text(text, encoding="utf-8")


_INFO: Final = (
    win32security.DACL_SECURITY_INFORMATION | win32security.OWNER_SECURITY_INFORMATION
)


def _backup_privilege() -> None:
    """Enable SeBackupPrivilege: node.key's DACL grants only the service SID, so
    even an elevated admin gets ERROR_ACCESS_DENIED for READ_CONTROL without it
    (SP1 run 40097330471 - which is the point of an owner-only key)."""
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(),
        win32security.TOKEN_ADJUST_PRIVILEGES | win32security.TOKEN_QUERY,
    )
    luid = win32security.LookupPrivilegeValue(None, win32security.SE_BACKUP_NAME)
    win32security.AdjustTokenPrivileges(
        token, False, [(luid, win32security.SE_PRIVILEGE_ENABLED)]
    )


def _security_descriptor(path: Path) -> tuple[Any, bool]:
    """(descriptor, needed_backup_privilege)."""
    try:
        return win32security.GetNamedSecurityInfo(
            str(path), win32security.SE_FILE_OBJECT, _INFO
        ), False
    except pywintypes.error as exc:
        if exc.winerror != 5:  # ERROR_ACCESS_DENIED
            raise
    _backup_privilege()
    handle = win32file.CreateFile(
        str(path),
        win32con.READ_CONTROL,
        win32file.FILE_SHARE_READ
        | win32file.FILE_SHARE_WRITE
        | win32file.FILE_SHARE_DELETE,
        None,
        win32file.OPEN_EXISTING,
        win32file.FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    try:
        return win32security.GetSecurityInfo(
            handle, win32security.SE_FILE_OBJECT, _INFO
        ), True
    finally:
        handle.Close()


def _aces(path: Path) -> dict[str, Any]:
    sd, via_backup = _security_descriptor(path)
    dacl = sd.GetSecurityDescriptorDacl()
    aces = []
    for i in range(dacl.GetAceCount()):
        (_type, flags), mask, sid = dacl.GetAce(i)[:3]
        aces.append({
            "sid": win32security.ConvertSidToStringSid(sid),
            "mask": hex(mask),
            "inherited": bool(flags & _INHERITED_ACE),
        })  # fmt: skip
    owner = win32security.ConvertSidToStringSid(sd.GetSecurityDescriptorOwner())
    return {"owner": owner, "aces": aces, "admin_denied_without_backup": via_backup}


def _firewall_mentions() -> dict[str, Any]:
    netsh = _run(
        ["netsh", "advfirewall", "firewall", "show", "rule", "name=all"], timeout=120
    )
    ps = _ps(
        "@(Get-NetFirewallRule | Where-Object { $_.DisplayName -like '*MeshForge*' -or "
        "$_.Name -like '*MeshForge*' }).Count"
    )
    return {
        "netsh_rc": netsh.get("rc"),
        "netsh_mentions": "meshforge" in (netsh.get("out") or "").lower(),
        "ps_count": (ps.get("out") or "").strip(),
        "ps_rc": ps.get("rc"),
    }


# ----------------------------------------------------------------- prepare
def prepare() -> int:
    """Bundle-like interpreter: copy the uv-managed CPython, install the locked
    agent wheels + pywin32 + meshforge into it (no venv), copy the probe."""
    # --system + a cwd outside the checkout: the managed interpreter, never .venv
    found = _must(
        _run(["uv", "python", "find", "--system", "3.12"], cwd=Path(os.environ["TEMP"]))
    )
    src = Path(found["out"].strip().splitlines()[-1]).parent
    if (src / "pyvenv.cfg").exists() or (src.parent / "pyvenv.cfg").exists():
        raise RuntimeError(f"uv python find returned a virtual environment: {src}")
    if BUNDLE_ROOT.exists():
        shutil.rmtree(BUNDLE_ROOT)
    shutil.copytree(src, BUNDLE_PY.parent)
    req = BUNDLE_ROOT / "requirements.txt"
    _must(_run(["uv", "export", "--frozen", "--no-dev", "--extra", "agent",
                "--no-emit-project", "-o", str(req)], cwd=_REPO))  # fmt: skip
    pip = [
        "uv",
        "pip",
        "install",
        "--python",
        str(BUNDLE_PY),
        "--break-system-packages",
    ]
    _must(_run([*pip, "--require-hashes", "-r", str(req)], timeout=900))
    _must(_run([*pip, "--no-deps", f"pywin32=={PYWIN32}", str(_REPO)], timeout=900))
    shutil.copytree(_LAB_DIR / "spikes" / "_sp1", PROBE_DIR)
    check = _must(_run([str(BUNDLE_PY), "-I", "-c",
                        "import servicemanager, win32service, meshforge.agent.winservice_host,"
                        " sys; print(sys.executable, sys.prefix)"]))  # fmt: skip
    print(f"bundle ready: {check['out'].strip()}")  # noqa: T201
    return 0


# --------------------------------------------------------------------- run
def _install(p: Phase) -> None:
    res = _run([str(BUNDLE_PY), "-I", "-m", "meshforge.agent.winservice", "install",
                "--python-exe", str(BUNDLE_PY), "--grace-s", f"{GRACE_S:g}",
                "--peer-firewall"])  # fmt: skip
    p.evidence["install"] = res
    if res.get("rc") == 0:
        _write_config(GOOD_CONFIG)
    _decide(
        p,
        {"install_exit_0": res.get("rc") == 0, "service_exists": _status() is not None},
    )


def _service_config(p: Phase) -> None:
    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    h = win32service.OpenService(
        scm, ws.SERVICE_NAME, win32service.SERVICE_QUERY_CONFIG
    )
    try:
        cfg = win32service.QueryServiceConfig(h)
        delayed = win32service.QueryServiceConfig2(
            h, win32service.SERVICE_CONFIG_DELAYED_AUTO_START_INFO
        )
        fa = win32service.QueryServiceConfig2(
            h, win32service.SERVICE_CONFIG_FAILURE_ACTIONS
        )
        flag = win32service.QueryServiceConfig2(
            h, win32service.SERVICE_CONFIG_FAILURE_ACTIONS_FLAG
        )
        sid_type = win32service.QueryServiceConfig2(
            h, win32service.SERVICE_CONFIG_SERVICE_SID_INFO
        )
    finally:
        win32service.CloseServiceHandle(h)
        win32service.CloseServiceHandle(scm)
    actions = [tuple(a) for a in (fa.get("Actions") or ())]
    showsid = _run(["sc.exe", "showsid", ws.SERVICE_NAME])
    m = re.search(r"S-1-5-80-[\d-]+", showsid.get("out") or "")
    looked_up = win32security.ConvertSidToStringSid(
        win32security.LookupAccountName(None, ws.SERVICE_ACCOUNT)[0]
    )
    p.evidence.update({
        "bin_path": cfg[3], "start_type": cfg[1], "account": cfg[7],
        "delayed_auto_start": delayed, "failure_actions": actions,
        "reset_period_s": fa.get("ResetPeriod"), "non_crash_flag": flag,
        "sid_type": sid_type, "sc_showsid_sid": m.group(0) if m else None,
        "lookup_sid": looked_up, "computed_sid": ws.service_sid(),
        "sc_qc": _run(["sc.exe", "qc", ws.SERVICE_NAME]).get("out"),
        "sc_qfailure": _run(["sc.exe", "qfailure", ws.SERVICE_NAME]).get("out"),
    })  # fmt: skip
    restart, none = win32service.SC_ACTION_RESTART, win32service.SC_ACTION_NONE
    _decide(p, {
        "account_is_virtual": str(cfg[7]).lower() == ACCOUNT,
        "start_auto": cfg[1] == win32service.SERVICE_AUTO_START,
        "delayed": bool(delayed),
        "restart_x3_then_none": actions == [(restart, 5000)] * 3 + [(none, 0)],
        "reset_period": fa.get("ResetPeriod") == ws.FAILURE_RESET_S,
        "non_crash_flag_off": not flag,
        "sid_unrestricted": sid_type == win32service.SERVICE_SID_TYPE_UNRESTRICTED,
        "computed_sid_matches_scm": ws.service_sid() == looked_up == p.evidence["sc_showsid_sid"],
    })  # fmt: skip


def _acl(p: Phase) -> None:
    sid = ws.service_sid()
    checks: dict[str, bool] = {}
    for acl in ws.acl_plan(LAYOUT, sid):
        got = _aces(Path(acl.path))
        name = Path(acl.path).name
        p.evidence[name] = got
        want = {g.sid for g in acl.grants}
        checks[f"{name}_only_system_admins_service"] = {
            a["sid"] for a in got["aces"]
        } == want
        checks[f"{name}_nothing_inherited"] = not any(
            a["inherited"] for a in got["aces"]
        )
        if acl.owner_sid is not None:
            checks[f"{name}_owned_by_service"] = got["owner"] == acl.owner_sid
    p.evidence["icacls_identity"] = _run(["icacls", str(LAYOUT.identity_dir)]).get(
        "out"
    )
    _decide(p, checks)


def _firewall_created(p: Phase) -> None:
    rule = ws.PEER_FIREWALL_RULE
    show = _run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={rule}"])
    prof = _ps(f"(Get-NetFirewallRule -DisplayName '{rule}').Profile.ToString()")
    port = _ps(
        f"(Get-NetFirewallRule -DisplayName '{rule}' | Get-NetFirewallPortFilter).LocalPort"
    )
    profile = (prof.get("out") or "").strip()
    p.evidence.update({"show": show.get("out"), "profile": profile,
                       "port": (port.get("out") or "").strip()})  # fmt: skip
    _decide(p, {
        "rule_exists": show.get("rc") == 0,
        "private_and_domain": "Private" in profile and "Domain" in profile,
        "never_public": "Public" not in profile,
        "port_47120": p.evidence["port"] == str(ws.PEER_PORT),
    })  # fmt: skip


def _running(p: Phase) -> None:
    _start()
    st = _status() or {}
    pid = int(st.get("ProcessId") or 0)
    agent_log = Path(LAYOUT.logs_dir, ws.AGENT_LOG_NAME)
    started = _wait(lambda: '"agent.started"' in _read(agent_log), 180, 1.0)
    tree = _tree(pid)
    ident = (
        sorted(os.listdir(LAYOUT.identity_dir))
        if Path(LAYOUT.identity_dir).exists()
        else []
    )
    key = Path(LAYOUT.identity_dir, "node.key")
    key_acl = _aces(key) if key.exists() else None
    p.evidence.update(
        {
            "status": st,
            "tree": tree,
            "identity_files": ident,
            "node_key_acl": key_acl,
            "agent_log": _log_tail(ws.AGENT_LOG_NAME, 15),
        }
    )
    users = {str(t["user"]).lower() for t in tree}
    _decide(p, {
        "running": st.get("CurrentState") == win32service.SERVICE_RUNNING,
        "host_and_agent_present": len(tree) >= 2,
        "all_run_as_virtual_account": users == {ACCOUNT},
        "agent_started_log": started,
        "identity_created_in_programdata": "node.key" in ident,
        "node_key_owner_only": key_acl is not None
        and {a["sid"] for a in key_acl["aces"]} == {ws.service_sid()},
    })  # fmt: skip


def _graceful_stop(p: Phase) -> None:
    t0 = time.monotonic()
    _stop()
    elapsed = time.monotonic() - t0
    st = _status() or {}
    agent = "\n".join(_log_tail(ws.AGENT_LOG_NAME, 60))
    svc = "\n".join(_log_tail(ws.SERVICE_LOG_NAME, 20))
    left = _service_procs()
    p.evidence.update({"status": st, "stop_s": round(elapsed, 1), "leftover": left,
                       "service_log": svc.splitlines()[-6:]})  # fmt: skip
    _decide(p, {
        "stopped_exit_0": st.get("Win32ExitCode") == 0,
        "agent_saw_stop": "agent.session.stop_requested" in agent,
        "agent_exited_cleanly": '"agent.exited"' in agent,
        "not_killed_by_grace": '"killed": false' in _last(svc, "agent.service.child_exited"),
        "within_grace": elapsed < GRACE_S + 10,
        "no_leftover_process": not left,
    })  # fmt: skip


def _crash_restart(p: Phase) -> None:
    _start()
    _wait(
        lambda: '"agent.started"' in "\n".join(_log_tail(ws.AGENT_LOG_NAME, 5)),
        120,
        1.0,
    )
    pid1 = int((_status() or {}).get("ProcessId") or 0)
    tree1 = [t["pid"] for t in _tree(pid1)]
    kill = _run(["taskkill", "/F", "/PID", str(pid1)])
    t0 = time.monotonic()
    tree_gone = _wait(lambda: not _alive(tree1), 15)

    def restarted() -> bool:
        st = _status() or {}
        return st.get("CurrentState") == win32service.SERVICE_RUNNING and int(
            st.get("ProcessId") or 0
        ) not in (0, pid1)

    back = _wait(restarted, 90, 0.5)
    pid2 = int((_status() or {}).get("ProcessId") or 0)
    p.evidence.update({"pid_before": pid1, "tree_before": tree1, "taskkill": kill,
                       "pid_after": pid2, "restart_s": round(time.monotonic() - t0, 1),
                       "survivors": _alive(tree1)})  # fmt: skip
    _decide(p, {
        "taskkill_ok": kill.get("rc") == 0,
        "job_close_killed_agent_child": tree_gone,
        "scm_restarted_new_pid": back,
    })  # fmt: skip


def _exit4(p: Phase) -> None:
    _stop()
    n0 = _read(Path(LAYOUT.logs_dir, ws.SERVICE_LOG_NAME)).count(
        "agent.service.host_starting"
    )
    _write_config(BAD_CONFIG)
    try:
        with contextlib.suppress(pywintypes.error):
            win32serviceutil.StartService(ws.SERVICE_NAME)
        stopped = _wait_state(win32service.SERVICE_STOPPED, 120)
        st1 = _status() or {}
        time.sleep(25)  # > restart delay: a restart would have happened by now
        st2 = _status() or {}
        n1 = _read(Path(LAYOUT.logs_dir, ws.SERVICE_LOG_NAME)).count(
            "agent.service.host_starting"
        )
    finally:
        _write_config(GOOD_CONFIG)
    p.evidence.update(
        {
            "status_after_exit": st1,
            "status_25s_later": st2,
            "host_starts": n1 - n0,
            "agent_log": _log_tail(ws.AGENT_LOG_NAME, 8),
            "sc_query": _run(["sc.exe", "query", ws.SERVICE_NAME]).get("out"),
        }
    )
    _decide(p, {
        "stopped": stopped,
        "win32_exit_1066": st1.get("Win32ExitCode") == ws.ERROR_SERVICE_SPECIFIC_ERROR,
        "service_exit_4": st1.get("ServiceSpecificExitCode") == 4,
        "still_stopped_later": st2.get("CurrentState") == win32service.SERVICE_STOPPED,
        "started_exactly_once": n1 - n0 == 1,
    })  # fmt: skip


def _probe_phases() -> None:
    out = Path(LAYOUT.cache_dir, "sp1-probe.json")
    with contextlib.suppress(OSError):
        out.unlink()
    child = [str(BUNDLE_PY), "-I", str(PROBE_DIR / "probe.py"), "--out", str(out),
             "--config", str(LAYOUT.config_file), "--cache", str(LAYOUT.cache_dir)]  # fmt: skip
    normal = ws.build_service_spec(str(BUNDLE_PY), LAYOUT, grace_s=GRACE_S).bin_path
    probe_bin = ws.build_service_spec(
        str(BUNDLE_PY), LAYOUT, grace_s=3, child_argv=child
    ).bin_path
    report: dict[str, Any] = {}
    pids: list[int] = []
    try:
        with phase("probe_started_in_service") as p:
            _stop()
            _set_bin_path(probe_bin)
            _start()

            def ready() -> bool:
                with contextlib.suppress(OSError, ValueError):
                    return bool(json.loads(out.read_text("utf-8")).get("ready"))
                return False

            ok = _wait(ready, 420, 2.0)
            if out.exists():
                report = json.loads(out.read_text("utf-8"))
            pid = int((_status() or {}).get("ProcessId") or 0)
            tree = _tree(pid)
            pids = [t["pid"] for t in tree]
            jt = report.get("job_tree") or {}
            pids += [
                int(x)
                for x in [jt.get("child_pid"), *(jt.get("grandchild_pids") or [])]
                if x
            ]
            p.evidence.update({"tree": tree, "report_keys": sorted(report)})
            _decide(
                p, {"probe_ready": ok, "tree_has_grandchildren": len(set(pids)) >= 5}
            )
        with phase("probe_identity_session0") as p:
            who = (report.get("whoami") or {}).get("stdout", "")
            p.evidence.update({"whoami": who, "session_id": report.get("session_id"),
                               "env": report.get("env")})  # fmt: skip
            _decide(p, {
                "whoami_is_virtual_account": "nt service\\meshforgeagent" in who.lower(),
                "whoami_sid_is_service_sid": ws.service_sid() in who,
                "session_0": report.get("session_id") == 0,
            })  # fmt: skip
        with phase("probe_gpu_docker_report") as p:
            hw = report.get("hardware_show") or {}
            p.evidence.update({"hardware_show": hw, "nvidia_smi": report.get("nvidia_smi"),
                               "docker": report.get("docker")})  # fmt: skip
            caps = hw.get("capabilities")
            _decide(p, {
                "hardware_show_exit_0": (hw.get("command") or {}).get("ok") is True,
                "reported_capabilities": isinstance(caps, list),
                "gpu_reported_absent_not_crashed": hw.get("gpus") == []
                or bool(report.get("nvidia_smi", {}).get("on_path")),
            })  # fmt: skip
        with phase("probe_uv_env_build") as p:
            uvb = report.get("uv_venv") or {}
            p.evidence["uv_venv"] = uvb
            _decide(p, {"uv_venv_exit_0": uvb.get("ok") is True,
                        "venv_python_exists": uvb.get("venv_python_exists") is True})  # fmt: skip
        with phase("stop_kills_whole_job_tree") as p:
            jt = report.get("job_tree") or {}
            t0 = time.monotonic()
            _stop()
            gone = _wait(lambda: not _alive(pids), 15)
            p.evidence.update({"pids": sorted(set(pids)), "survivors": _alive(pids),
                               "stop_s": round(time.monotonic() - t0, 1), "job_tree": jt,
                               "service_log": _log_tail(ws.SERVICE_LOG_NAME, 4)})  # fmt: skip
            _decide(p, {
                "nested_job_object_in_service_job": jt.get("nested_job_object") is True,
                "no_descendant_survives": gone,
                "no_service_account_process": not _service_procs(),
            })  # fmt: skip
    finally:
        with contextlib.suppress(Exception):
            _stop()
        with contextlib.suppress(Exception):
            _set_bin_path(normal)


def _uninstall(p: Phase) -> None:
    res = _run([str(BUNDLE_PY), "-I", "-m", "meshforge.agent.winservice", "uninstall"])
    gone = _wait(lambda: _status() is None, 30)
    fw = _firewall_mentions()
    p.evidence.update(
        {"uninstall": res, "firewall": fw, "data_kept": Path(LAYOUT.root).exists()}
    )
    _decide(p, {
        "uninstall_exit_0": res.get("rc") == 0,
        "service_gone": gone,
        "netsh_lists_no_meshforge_rule": fw["netsh_rc"] == 0 and not fw["netsh_mentions"],
        "powershell_counts_zero": fw["ps_count"] == "0",
        "no_service_account_process": not _service_procs(),
    })  # fmt: skip


def run(out: Path) -> int:
    if not BUNDLE_PY.exists():
        print(f"no bundle at {BUNDLE_PY}: run `prepare` first", file=sys.stderr)  # noqa: T201
        return 2
    try:
        for name, fn in (
            ("install", _install),
            ("service_config", _service_config),
            ("state_folder_acls", _acl),
            ("firewall_rule_named_private_domain", _firewall_created),
            ("running_as_virtual_account", _running),
            ("graceful_stop", _graceful_stop),
            ("crash_restarts_via_scm", _crash_restart),
            ("exit_4_not_restarted", _exit4),
        ):
            with phase(name) as p:
                fn(p)
        _probe_phases()
        with phase("uninstall_leaves_nothing") as p:
            _uninstall(p)
    finally:
        if _status() is not None:
            _run(
                [str(BUNDLE_PY), "-I", "-m", "meshforge.agent.winservice", "uninstall"]
            )
        _write_results(out)
    return 0 if all(p.ok for p in PHASES) else 3


def _write_results(out: Path) -> None:
    env_keys = ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")
    server, repo, run_id = (os.environ.get(k) for k in env_keys)
    meta = {
        "spike": "SP1",
        "when": datetime.now(UTC).isoformat(timespec="seconds"),
        "run_url": f"{server}/{repo}/actions/runs/{run_id}" if run_id else None,
        "windows": platform.platform(),
        "windows_version": platform.version(),
        "python_driver": sys.version,
        "pywin32": version("pywin32"),
        "bundle_python": str(BUNDLE_PY),
        "logs": {
            "service": _log_tail(ws.SERVICE_LOG_NAME, 60),
            "agent": _log_tail(ws.AGENT_LOG_NAME, 60),
        },
    }
    payload = {"meta": meta, "phases": [asdict(p) for p in PHASES],
               "passed": all(p.ok for p in PHASES)}  # fmt: skip
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    lines = ["| phase | result | s | detail |", "|---|---|---|---|"]
    lines += [f"| {p.name} | {'PASS' if p.ok else 'FAIL'} | {p.seconds} | {p.detail[:160]} |"
              for p in PHASES]  # fmt: skip
    table = "\n".join(lines)
    print("\n" + table)  # noqa: T201
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"## SP1 Windows service\n\n{table}\n")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0] if __doc__ else None
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prepare")
    r = sub.add_parser("run")
    r.add_argument("--out", type=Path, default=Path("sp1-results.json"))
    a = ap.parse_args()
    return prepare() if a.cmd == "prepare" else run(a.out)


if __name__ == "__main__":
    sys.exit(main())
