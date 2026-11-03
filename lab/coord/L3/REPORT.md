# L3 REPORT (new laptop, replaces TIWARI_JI)


### R1-1 - Who am I - OK
when: 2026-11-03 14:12 (local)
laptop: L3 (Windows 11 Home Single Language 10.0.22631, hostname MOHIT)
commands:
    Get-CimInstance Win32_OperatingSystem / Win32_Processor / Win32_VideoController; netsh wlan show interfaces; Get-NetIPAddress; Get-NetConnectionProfile
result: CPU 12th Gen Intel Core i5-1235U, RAM 7.7 GB visible, GPU Intel UHD Graphics only (NO NVIDIA GPU). Wi-Fi IP 192.168.31.71, SSID JARVIS, 5 GHz, ch 157, 802.11ax, signal 95%, rx 600.5 Mbps.
  Network profile was **Public**; human asked to switch to Private (result in a later entry). Same room as L1: asked, answer pending.

### R1-2 - Tools - OK
when: 2026-11-03 14:12 (local)
laptop: L3 (MOHIT)
commands:
    git --version; uv --version; uv sync; uv run python --version; docker version
result: git 2.55.0.windows.3. uv was NOT installed: installed by the agent with `winget install astral-sh.uv --scope user` -> uv 0.13.7.
  `uv sync` attempt 1 FAILED: `error: Missing expected target directory for Python minor version link at ...\uv\python\cpython-3.12.16-windows-x86_64-none` (right after downloading cpython 3.12.16).
  attempt 2 (`uv python install 3.12` printed the same error, then `uv sync`) OK. `uv run python --version` -> Python 3.12.16. System Python 3.13.14 also present.
  Docker: client 29.9.2 installed (Docker Desktop, context desktop-linux) but daemon NOT running (`dockerDesktopLinuxEngine` pipe missing). Not started, not installed by the agent.
  gh CLI 2.103.3 installed earlier today (user scope), logged in.

### R1-3 - Inventory (0.2.2) - FAIL
when: 2026-11-03 14:12 (local)
laptop: L3 (MOHIT)
commands:
    uv run python lab/inventory.py --out "lab/reports/inventory/MOHIT.json"   (run twice)
result: exit 0 both times, but probes cpu, gpus, memory, network, os, power, torch, uv FAILED: `powershell CIM query failed: powershell: timed out after 50.7 s (run deadline reached)`; later probes `skipped: run deadline exceeded`. Secure boot: enabled. nvidia-smi: not found. Docker daemon not reachable.
evidence: lab/reports/inventory/MOHIT.attempt1.json, lab/reports/inventory/MOHIT.json
BUG: lab/_inventory CIM bundle hangs only when launched by the runner. The SAME script (pw.CIM_SCRIPT written to a file) runs in 3.8 s with `powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File cim.ps1`; a single `Get-CimInstance Win32_Processor` takes 1.4 s. So PowerShell/CIM is healthy; suspect the runner's launch (`-EncodedCommand` / new process group / stdin pipe) on this laptop. Reproduce: run the command above on MOHIT. Non-blocking for later steps.

### R1-4 - GPU check - SKIPPED
when: 2026-11-03 14:16 (local)
laptop: L3 (MOHIT)
result: no NVIDIA GPU (only Intel UHD Graphics); nvidia-smi not on PATH.

### R1-5 - Hardware scores (0.2.11) - BLOCKED
when: 2026-11-03 14:16 (local)
laptop: L3 (MOHIT)
commands:
    uv run python lab/spikes/s7_hardware.py run --quick --name L3 --skip-gpu
result: exit 1, traceback before any score: `RuntimeError: powershell CIM query failed: powershell: timed out after 60 s` (environment.snapshot -> probes.probe_power -> windows_cim). Full run not attempted (same code path).
BUG: root cause isolated on MOHIT: `powershell ... -EncodedCommand <pw.CIM_SCRIPT>` (6008 chars) HANGS (>40 s, killed), with or without CREATE_NO_WINDOW|CREATE_NEW_PROCESS_GROUP; the same script via `-File cim.ps1` returns rc 0 in 3.0-3.4 s (2296 bytes) under both flag sets; runner._env() is identical to os.environ. Likely Windows Defender/AMSI holding the encoded command. Suggested fix: write the script to a temp .ps1 and use `-File` (fallback when -EncodedCommand times out). Affects lab/_inventory (R1-3) and everything using windows_cim (S7, maybe S2 env).

### R1-1b - Wi-Fi profile + room - OK
when: 2026-11-03 14:28 (local)
laptop: L3 (MOHIT)
commands:
    Get-NetConnectionProfile
result: human switched JARVIS to Private -> `JARVIS Private` confirmed. Same room as L1: yes (human).

### R1-6 - Environments (0.2.10) - OK (docker cases skipped)
when: 2026-11-03 14:28 (local)
laptop: L3 (MOHIT)
commands:
    uv run python lab/spikes/s6_environments.py run --quick --name L3   (exit 3)
    uv run python lab/spikes/s6_environments.py run --name L3           (exit 3)
result: venv process start 0.85 s (within the <= 3 s target); venv create+install cold 5.09 +- 0.42 s, warm 1.13 +- 0.04 s (n=5).
  Docker cases skipped: quick run = `docker_daemon_unreachable` (Docker Desktop client present, engine not running); by the full run the docker CLI was no longer on PATH (`docker_cli_missing`; C:\Program Files\Docker absent). Treat L3 as **no Docker**. GPU cases n/a (no NVIDIA).
evidence: lab/reports/raw/2026-11-03-S6-environments/l3-842983de__quick.json, lab/reports/raw/2026-11-03-S6-environments/l3-842983de.json

### R1b - round 1b items - OK
when: 2026-11-03 14:28 (local)
laptop: L3 (MOHIT)
commands:
    uv sync --extra agent
result: exit 0. Wi-Fi Private: done (R1-1b). CUDA torch: not applicable (no NVIDIA GPU).

### R1-7 - Push - OK
result: round 1 pushed. Open: R1-5 BLOCKED by the -EncodedCommand hang (see R1-5 BUG); R1-3 inventory incomplete for the same reason.

### C0-a - P0 sync - OK
when: 2026-11-03 14:28 (local)
laptop: L3 (MOHIT)
commands:
    git fetch origin; git checkout lab/L3; git merge origin/main; git rev-parse --short HEAD; uv sync --extra agent
result: HEAD dc257850, uv sync --extra agent exit 0.

### C0-c - ready - OK
SIGNAL: L3-ready ip=192.168.31.71 os=windows host=MOHIT

### NL-1 - old files removed - OK
when: 2026-11-03 14:59 (local)
result: 9 TIWARI_JI files (2026-11-01) removed from lab/L3 after checking each exists on origin/lab/L3-old-TIWARI_JI. MOHIT files (2026-11-03) kept.

### CU-a - Network facts (0.2.4) - OK
when: 2026-11-03 14:59 (local)
laptop: L3 (MOHIT)
commands:
    netsh wlan show interfaces; Get-NetAdapter | ft Name,LinkSpeed; Get-NetConnectionProfile
result: Wi-Fi 5 GHz 802.11ax, Private, same room as L1. MAC/BSSID lines left out.
evidence: lab/reports/raw/2026-11-03-V0.2-network-facts/L3.txt

### C0-b - human batch - OK (clock: FAIL, non-blocking)
when: 2026-11-03 15:09 (local)
laptop: L3 (MOHIT)
result: sleep on AC = 0 (off), on charger (battery status 2), Wi-Fi profile Private, S1 firewall rules TCP/UDP 47201 Private enabled (CU-0). Windows Update pause: asked of human.
  `w32tm /query /status` -> `The service has not been started. (0x80070426)`: Windows Time service stopped. Matters for C4.0 clock offset later.

### CU-0 - S1 firewall - OK
result: `MeshForge S1 TCP True Private`, `MeshForge S1 UDP True Private`.

### CU-b - S1 server - running
when: 2026-11-03 15:09 (local)
commands:
    uv run python lab/spikes/s1_network.py serve --bind 192.168.31.71 --idle 3600 --duration 14400
result: listening on 192.168.31.71:47201. Waiting for L1, L2, L4 clients.
SIGNAL: s1-serve-up L3 ip=192.168.31.71
