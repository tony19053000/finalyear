# L3 REPORT

### R1-1 - Who am I - OK
when: 2026-11-01 10:20 (local)
laptop: L3 (Windows 11 Home Single Language 10.0.26300, hostname TIWARI_JI)
commands:
    Get-CimInstance Win32_OperatingSystem / Win32_Processor / Win32_VideoController
    netsh wlan show interfaces
    Get-NetIPAddress -AddressFamily IPv4 ; Get-NetConnectionProfile
result: CPU AMD Ryzen 7 5800HS (8c/16t), RAM 15.4 GB, GPU AMD Radeon integrated only - NO NVIDIA GPU (this is the no-GPU laptop). Wi-Fi IP 192.168.31.236, MediaTek MT7921, 5 GHz ch 157, 802.11ac, signal 87% (RSSI -37), link 866.7 Mbps.
  Wi-Fi profile: Public. Human was asked to switch to Private and chose to keep it Public (may block inbound connections in round 2).
  Same room as L1: not answered yet.
evidence: this entry
QUESTIONS: Network profile left Public by human decision - Lead please note for round 2.

R1-1 addendum (2026-11-01 10:21): human confirmed L3 IS in the same room as L1.

### R1-2 - Tools - FAIL
when: 2026-11-01 10:21 (local)
laptop: L3 (Windows 11, hostname TIWARI_JI)
commands:
    git --version ; uv --version ; uv sync ; uv run python --version ; docker version
result: git 2.52.0.windows.1 OK. uv NOT INSTALLED ("The term 'uv' is not recognized"), so uv sync / uv run did not run. Docker NOT INSTALLED. System Python 3.12 exists (AppData\Local\Programs\Python\Python312).
evidence: this entry
BUG: not a code bug - uv missing on L3 blocks R1-2..R1-6. Asking human whether to install uv (official installer).

### R1-2 - Tools (attempt 2, after installing uv) - OK
when: 2026-11-01 10:26 (local)
laptop: L3 (Windows 11, hostname TIWARI_JI)
commands:
    winget install --id astral-sh.uv -e   (human approved)
    uv --version ; uv sync ; uv run python --version ; docker version
result: uv 0.13.6 installed. uv sync exit 0 (all packages installed, no errors). uv run python = 3.12.10. git 2.52.0. Docker still NOT INSTALLED (not installed, per TODO).
evidence: this entry

### R1-3 - Inventory - OK
when: 2026-11-01 10:27 (local)
laptop: L3 (Windows 11, hostname TIWARI_JI)
commands:
    uv run python lab/inventory.py --out "lab/reports/inventory/$env:COMPUTERNAME.json"
result: exit 0, failed probes none, problems none. CPU AMD Ryzen 7 5800HS 8c/16t; RAM 15.4 GiB; GPU AMD Radeon integrated 512 MiB, nvidia-smi not found; disk C: 475 GiB total / 38.7 GiB free. Secure Boot enabled, torch not installed, docker not installed. Note: on battery (58%) at time of run.
evidence: lab/reports/inventory/TIWARI_JI.json

### R1-4 - GPU check - SKIPPED
when: 2026-11-01 10:27 (local)
laptop: L3 (Windows 11, hostname TIWARI_JI)
commands:
    (none - per TODO)
result: SKIPPED - no NVIDIA GPU on L3 (only AMD Radeon integrated; nvidia-smi absent). torch not installed, Docker not installed.
evidence: lab/reports/inventory/TIWARI_JI.json

### R1-5 - Hardware scores (attempt 1, quick) - FAIL
when: 2026-11-01 10:29 (local)
laptop: L3 (Windows 11, hostname TIWARI_JI)
commands:
    uv run python lab/spikes/s7_hardware.py run --quick --name L3 --skip-gpu
result: exit 3. Printed "!! ON BATTERY" - human said charger plugged in, but Windows reports PowerLineStatus Offline, battery 57% discharging. Quick scores (not comparable): cpu-python-1t 8.59 MFLOP/s, cpu-python-all 91.18 MFLOP/s, memory copy 24.42 GB/s. Also no_blas_backend (no torch/numpy).
evidence: lab/reports/raw/2026-11-01-S7-hardware/l3-842983de__quick.json
