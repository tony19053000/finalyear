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
