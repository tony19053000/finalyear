# Spike SP1 — the agent as a native Windows service (task 0.17.0, D386 / D388 / D389)

**Question:** can the bundled Python + pywin32 host the agent as the service
`MeshForgeAgent` under the virtual account `NT SERVICE\MeshForgeAgent` — install,
start, stop gracefully, restart after a crash, *not* restart on exit 4/5, build a
`uv` environment and run a job tree that is fully killed on stop, report GPU/Docker
honestly from session 0, and uninstall without leaving a service, a process or a
firewall rule? Or do we need the D386 fallback (WinSW)?

Script: `lab/spikes/sp1_windows_service.py` · probe run as the service's child:
`lab/spikes/_sp1/probe.py` · prototype (not wired into an installer yet):
`src/meshforge/agent/winservice.py` (pure parts, Linux-testable),
`src/meshforge/agent/winservice_host.py` (pywin32), `src/meshforge/agent/winservice_supervisor.py`
(child supervision, cross-platform) · tests: `tests/unit/agent/test_winservice.py`,
`tests/component/agent/test_winservice_supervisor.py` · workflow:
`.github/workflows/spike-sp1.yml` (manual / push to `spike/sp1-windows-service`).

```powershell
# elevated prompt, repo checkout
uv sync --extra agent
uv pip install pywin32==312
uv run python lab/spikes/sp1_windows_service.py prepare   # bundle-like python under C:\Program Files\MeshForge\sp1
uv run python lab/spikes/sp1_windows_service.py run --out reports/sp1-results.json
```

Measured on GitHub-hosted `windows-latest`: **Windows Server 2025, build 10.0.26100**
(the same NT build as Windows 11 24H2), CPython 3.12.13 (uv-managed, copied as a
bundle — no venv), pywin32 312, elevated runner account, no GPU, Docker Engine present.

## 1. Answer in one table (green run 40097983450)

https://github.com/tony19053000/finalyear/actions/runs/40097983450 — 14/14 phases PASS,
artifact `sp1-results` holds the full JSON.

| Check | Result | Evidence (from the run's JSON) |
|---|---|---|
| install | **PASS**, 1.3 s, idempotent `install` CLI | `installed MeshForgeAgent as NT SERVICE\MeshForgeAgent` |
| service config | **PASS** | `sc qc`: `AUTO_START (DELAYED)`, `SERVICE_START_NAME: NT SERVICE\MeshForgeAgent`; `sc qfailure`: RESTART 5000 ms ×3 then none, reset 3600 s; non-crash flag **off**; SID type unrestricted; our computed service SID = `sc showsid` = `LookupAccountName` (`S-1-5-80-2154090528-…-1387896820`) |
| state-folder ACLs | **PASS** | `ProgramData\MeshForge\{.,config,identity,cache,logs}`: only SYSTEM + Administrators + the service SID, nothing inherited; `identity\` owned by the service SID; config is read-only for the service (`0x1200a9`) |
| `node.key` | **PASS** | single ACE = service SID, owner = service SID. Even the elevated admin gets `ERROR_ACCESS_DENIED` reading its DACL without `SeBackupPrivilege` (first run's failure, §3) |
| firewall rule | **PASS** | `MeshForge-Agent-Peer`, TCP 47120 in, profiles `Domain,Private` (never Public) |
| running as the virtual account | **PASS**, `agent.started` in 24 s | host + agent child (+conhost) all `NT SERVICE\MeshForgeAgent`; identity created in `ProgramData\MeshForge\identity` (`identity.lock`, `node.json`, `node.key`) |
| graceful stop | **PASS**, **2.0 s** | `Win32ExitCode 0`; agent log has `agent.session.stop_requested` and `agent.exited`; `child_exited killed=false` (stdin-EOF stop, not the grace kill); no process left |
| crash → SCM restart | **PASS**, new PID **5.5 s** after `taskkill /F` of the host | the agent child + its conhost died with the host (job object `KILL_ON_JOB_CLOSE`, survivors `[]`); SCM started a new host (PID 7388 → 7988) |
| exit 4 → not restarted | **PASS** | `sc query`: `STOPPED`, `WIN32_EXIT_CODE 1066`, `SERVICE_EXIT_CODE 4`; still stopped 25 s later; host started exactly once |
| probe identity / session | **PASS** | `whoami /user` in the child: `nt service\meshforgeagent`, SID = service SID; session **0**; profile `C:\Windows\ServiceProfiles\MeshForgeAgent` |
| GPU / Docker report from the service | **PASS (report, not gate — D388)** | `meshforge-agent hardware show` exit 0, `gpus: []`, `degraded: gpu unavailable: NVMLError_LibraryNotFound; nvidia-smi is not on PATH`. `docker version` as the service: `permission denied … npipe:////./pipe/docker_engine` — exactly the D388 "not in docker-users" case |
| `uv venv` env build as the service | **PASS**, 0.14 s | venv under `ProgramData\MeshForge\cache` from the bundled interpreter |
| stop kills the whole job tree | **PASS**, 3.5 s (3 s grace + kill) | 7 PIDs (host, probe, a job started with the agent's own `process.spawn` in a **nested** job object, its 2 grandchildren, conhosts): survivors `[]`, no process of the service account left |
| uninstall | **PASS**, 2.3 s | service gone; `netsh … show rule name=all` mentions no MeshForge; `Get-NetFirewallRule *MeshForge*` count 0; no service-account process; data under `ProgramData\MeshForge` kept (D393 default) |

## 2. Verdict: **pywin32 (D386 as written). WinSW stays an unused fallback.**

Every check SP1 can make on a runner passed with the pywin32 host. Nothing found
needs WinSW, and WinSW would not remove any of our code:

| | pywin32 host (tested) | WinSW (not run — no failure to fall back from) |
|---|---|---|
| extra artifact | none beyond pywin32 wheels already in the bundle (ADR-0009: one Python) | a separate `.exe` (+ XML) to pin, verify and ship; v2 needs .NET Framework, v3 is still pre-release |
| exit 4/5 → no restart | done: STOPPED + `ERROR_SERVICE_SPECIFIC_ERROR` + code 4, non-crash actions off (`sc query` shows the agent's reason) | WinSW applies its `onfailure` actions to any failure exit; an exit-code-specific "do not restart" would still need our own wrapper logic |
| graceful stop | stdin-EOF → `SIGBREAK` to the agent's own stop handler; 2.0 s, Goodbye sent | console Ctrl+C injection into the child, then kill after `stoptimeout` |
| whole-tree kill | job object (reuses `agent.process`), proven incl. nested job + grandchildren and host crash | its own process-tree kill; job trees started by the agent would still rely on our nested jobs |
| virtual account, delayed start, failure actions, SID type | set directly via the SCM API, verified with `sc qc` / `qfailure` / `showsid` | configurable, same SCM underneath |
| in-service probes (GPU, Docker, session 0) | runs the same agent code paths | identical (it would run the same Python child) |

The one real difference is the extra wrapper; pywin32 keeps one runtime and one
tested code path (`meshforge-agent run` is the child in both the service and the
console case). **G4 should build on `winservice*.py`.**

## 3. What the runs found (and fixed)

| Run | Result | Finding → fix (commit) |
|---|---|---|
| 40097151170 | fail (prepare) | the bundle copied `.venv`'s interpreter → `prepare` uses `uv python find --system` from outside the checkout (e554d13e) |
| 40097330471 | 11/14 | **real bug:** on exit 4 the agent child died at interpreter shutdown with `Fatal Python error: _enter_buffered_busy: could not acquire lock for <_io.BufferedReader name='<stdin>'>` (exit `0xC0000005`), so the host saw a crash and the SCM restarted it 3×. The stop-watcher daemon thread was blocked in a *buffered* `sys.stdin.buffer.read`, holding the reader lock the shutdown flush needs. Reproduced on Linux too (SIGABRT). Fix: the watcher reads fd 0 **unbuffered**; new component test `test_the_real_agent_refusing_its_config_exits_4_not_a_shutdown_crash` fails before, passes after. Two driver bugs: `node.key` DACL read needs `SeBackupPrivilege` (the key is owner-only by design), and the probe's `whoami` was read from the wrong key (e0c74308) |
| 40097983450 | **14/14 PASS** | — |
| 40098434410 | **14/14 PASS** | repeat on the verdict commit (fresh runner) — same result |

## 4. Not provable on a CI runner → 🧑 lab step (before G4 is closed)

A runner has no interactive user, no NVIDIA GPU, Docker Engine instead of Docker
Desktop, and is never rebooted. These D386/D388 claims stay **open**:

1. **Starts at boot** (delayed auto start) and **survives logoff.**
2. **GPU from session 0:** `nvidia-smi` and a CUDA tensor in a job env, run *by the service*.
3. **Docker Desktop via `docker-users`:** unreachable before adding the account, reachable after.

On one team Windows 11 laptop with an NVIDIA GPU and Docker Desktop, elevated PowerShell,
repo checkout on this branch:

```powershell
uv sync --extra agent; uv pip install pywin32==312
uv run python lab/spikes/sp1_windows_service.py prepare
uv run python lab/spikes/sp1_windows_service.py run --out sp1-laptop.json   # expect 14/14 PASS, gpu reported present
# boot + logoff
& "C:\Program Files\MeshForge\sp1\python\python.exe" -I -m meshforge.agent.winservice install --python-exe "C:\Program Files\MeshForge\sp1\python\python.exe"
# write C:\ProgramData\MeshForge\config\config.toml = GOOD_CONFIG from the spike script, then reboot, log in, wait 2 min:
sc.exe query MeshForgeAgent                                    # expect RUNNING
Get-Content C:\ProgramData\MeshForge\logs\agent.log -Tail 5    # expect agent.started after the boot time
# sign out, sign back in:
sc.exe query MeshForgeAgent                                    # expect RUNNING, same PID as before sign-out
# docker-users
net localgroup docker-users "NT SERVICE\MeshForgeAgent" /add; Restart-Service MeshForgeAgent
& "C:\Program Files\MeshForge\sp1\python\python.exe" -I -m meshforge.agent.winservice uninstall
```

Paste back: the `run` table, `gpus` / `nvidia_smi` / `docker` from `sp1-laptop.json`,
the three `sc query` outputs, and the `agent.log` lines. The GPU+CUDA tensor-in-a-job
check needs a real job env with torch and is best done once G4 wires the service
into the full agent (the probe currently checks `nvidia-smi` and the agent's own NVML probe).

## 5. Notes for G4

- pywin32 must become a locked, Windows-only dependency of the `agent` extra
  (`pywin32; sys_platform == 'win32'`); the workflow installs it ad hoc today.
- The child exit code is the contract (`classify_exit`): any new "do not restart"
  exit must be added there and to systemd's `RestartPreventExitStatus`.
- Anything in the agent that reads stdin in a daemon thread must use an unbuffered
  fd (§3) — otherwise an own exit can turn into a crash and a restart loop.
- `crash_restarts_via_scm` took 126 s because the driver's "agent started" wait
  only looks at the last 5 log lines and times out (120 s) before killing; the
  restart itself is 5.5 s. Cosmetic, driver only.
