# L4 REPORT

### R1-1 - Who am I - OK
when: 2026-11-03 13:50 (local)
laptop: L4 (Windows 11, hostname LAPTOP-7ORVM30G)
commands:
    Get-CimInstance Win32_OperatingSystem / Win32_Processor / Win32_VideoController; Get-NetIPAddress; netsh wlan show interfaces; Get-NetConnectionProfile
result: Windows 11 Home Single Language 10.0.26300; i5-12450H (8C/12T); 15.7 GiB RAM; NVIDIA GeForce RTX 3050 Laptop GPU 4 GB + Intel UHD.
    Wi-Fi IP 192.168.31.244, SSID JARVIS, 5 GHz ch 157, 802.11ax, 1201 Mbps, signal 97%. On AC power.
    Network profile "JARVIS 2" was **Public** -> human asked to switch to Private (result in C0-b).
    Same room as L1: to confirm with human.

### R1-2 - Tools - OK
when: 2026-11-03 13:53 (local)
laptop: L4
commands:
    git --version; winget install astral-sh.uv; uv --version; uv python install 3.12; uv sync; uv run python --version; docker version
result: git 2.56.0.windows.4 (installed today), uv 0.13.7 (installed today, user scope), Python 3.12.16. uv sync OK. Docker: not installed.
BUG: (tooling, non-blocking) first `uv run` and `uv python install 3.12` print
    "error: Missing expected target directory for Python minor version link at `...\uv\python\cpython-3.12.16-windows-x86_64-none`",
    but the interpreter is installed and `uv sync` / `uv run python --version` work.

### R1-3 - Inventory (0.2.2) - OK
when: 2026-11-03 13:56 (local)
laptop: L4
commands:
    uv run python lab/inventory.py --out "lab/reports/inventory/$env:COMPUTERNAME.json"
result: exit 0. cpu i5-12450H 8C/12T; ram 15.7 GiB; gpus Intel UHD + RTX 3050 Laptop 4096 MiB; nvidia OK driver 592.27 CUDA 13.1;
    secure boot enabled; torch/docker not installed; Wi-Fi 1201 Mbit/s; problems none.
evidence: lab/reports/inventory/LAPTOP-7ORVM30G.json

### R1-4 - GPU check (0.2.1 / 0.2.3) - OK
when: 2026-11-03 13:56 (local)
laptop: L4
commands:
    nvidia-smi; uv run python -c "import torch; ..."
result: NVIDIA-SMI 592.27, Driver 592.27, CUDA 13.1; RTX 3050 Laptop, WDDM, 52C, P0, 9W/70W, 0/4096 MiB.
    torch: not installed (ModuleNotFoundError), not installed per rules. Docker GPU test: SKIPPED (no Docker).

### R1-5 - Hardware scores (0.2.11) - run 1 - FAIL (thermal throttling flagged)
when: 2026-11-03 14:05 (local)
laptop: L4
commands:
    uv run python lab/spikes/s7_hardware.py run --quick --name L4
    uv run python lab/spikes/s7_hardware.py run --name L4
result: quick exit 3: py-1t 22.87±2.58 MFLOP/s, py-all 171.04±33.60, mem copy 17.73±1.76 GB/s.
    full exit 3: py-1t 25.66±1.55 MFLOP/s, py-all 179.09±4.49, mem copy 20.11±1.74 GB/s.
    No BLAS (torch/numpy not installed) -> no GFLOPS, GPU not measured. Full run printed
    "!! thermal throttling suspected ... memory-bandwidth". Power plan "Balanced" at the time; human asked to set Best performance + clear vents, rerun follows.
evidence: lab/reports/raw/2026-11-03-S7-hardware/l4-4a5a97c6__quick.json, l4-4a5a97c6.json (run 1)

### R1-6 - Environments (0.2.10) - OK (docker cases skipped)
when: 2026-11-03 14:00-14:10 (local)
laptop: L4
commands:
    uv run python lab/spikes/s6_environments.py run --quick --name L4
    uv run python lab/spikes/s6_environments.py run --name L4
result: quick exit 3 (smoke, INVALID n=2). full exit 3: venv start 898.16±57.08 ms; venv cold 5.68±2.84 s; warm 0.89±0.12 s (within 3 s target).
    Skipped: docker-start, docker-pull-cold, docker-gpu-smi, docker-gpu-torch (docker_cli_missing). Recommendation: venv-fallback.
evidence: lab/reports/raw/2026-11-03-S6-environments/l4-4a5a97c6__quick.json, l4-4a5a97c6.json

### R1-5 - Hardware scores (0.2.11) - run 2 - OK
when: 2026-11-03 14:20 (local)
laptop: L4
commands:
    uv run python lab/spikes/s7_hardware.py run --name L4
result: exit 3 (no BLAS/torch, expected). py-1t 28.42±0.44 MFLOP/s, py-all 166.36±6.55, mem copy 22.40±0.66 GB/s.
    No throttling / battery warning after human set Best performance + cleared vents.
evidence: lab/reports/raw/2026-11-03-S7-hardware/l4-4a5a97c6__run2.json (run 1 kept as l4-4a5a97c6__run1-throttled.json)

### R1-7 - Push - OK
result: round 1 files pushed.

## DONE (round 1)

### C0-a - sync - OK
when: 2026-11-03 14:25 (local)
commands:
    git fetch origin; git merge origin/main; git rev-parse --short HEAD; uv sync --extra agent
result: HEAD 080474cf, uv sync --extra agent exit 0.

### C0-a - re-sync after break - OK
when: 2026-11-03 (local, after the 2-day break)
commands:
    git fetch origin; git merge origin/main; read lab/coord/ANNOUNCE.md
result: HEAD f41d451f. L4 TODO unchanged. Wi-Fi IP re-checked: 192.168.31.244 (unchanged).

### C0-b - HUMAN BATCH - PARTIAL
result: Wi-Fi profile JARVIS 2 = Private (OK, the blocking part). Pending human (Administrator): sleep off on AC
    (powercfg), Windows Update pause, clock resync (w32tm /query /status: Leap Indicator 3, not synchronized; last sync 13:10, source time.windows.com).

### C0-c - ready - OK
SIGNAL: L4-ready ip=192.168.31.244 os=windows host=LAPTOP-7ORVM30G

### CU-a - Network facts (0.2.4) - OK
commands:
    netsh wlan show interfaces; Get-NetAdapter | ft Name,LinkSpeed; Get-NetConnectionProfile
result: Intel Wi-Fi 6E AX211, SSID JARVIS, 5 GHz ch 157, 802.11ax, 1.2 Gbps, signal 97%, profile Private.
evidence: lab/reports/raw/2026-11-03-V0.2-network-facts/L4.txt
