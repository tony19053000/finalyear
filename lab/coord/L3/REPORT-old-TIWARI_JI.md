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

### R1-5 - Hardware scores (attempt 2, quick + full) - FAIL
when: 2026-11-01 10:31 (local)
laptop: L3 (Windows 11, hostname TIWARI_JI)
commands:
    uv run python lab/spikes/s7_hardware.py run --quick --name L3 --skip-gpu
    uv run python lab/spikes/s7_hardware.py run --name L3 --skip-gpu
result: Before: Windows PowerLineStatus Online, charging 56%.
  quick: exit 3, no ON BATTERY warning. cpu-python-1t 20.74 ± 0.34 MFLOP/s, cpu-python-all 89.27 ± 3.11, memory copy 25.87 ± 3.82 GB/s.
  full: exit 3, printed "!! ON BATTERY" again. cpu-python-1t 22.94 ± 0.46, cpu-python-all 145.47 ± 0.69, memory copy 32.56 ± 0.15 GB/s.
  After: Windows says Online 57% Charging. So the charger likely dropped out during the full run (loose cable?) - or the battery probe is flaky. Both runs also report no_blas_backend (no torch/numpy) and gpu_skipped, which may also cause exit 3.
evidence: lab/reports/raw/2026-11-01-S7-hardware/l3-842983de__quick__run2.json, lab/reports/raw/2026-11-01-S7-hardware/l3-842983de.json
QUESTIONS: Is exit 3 expected whenever no_blas_backend is reported (no numpy/torch on L3)?

### R1-6 - Environments (no Docker) - OK
when: 2026-11-01 10:33 (local)
laptop: L3 (Windows 11, hostname TIWARI_JI)
commands:
    uv run python lab/spikes/s6_environments.py run --quick --name L3
    uv run python lab/spikes/s6_environments.py run --name L3
result: both exit 3 (expected: Docker missing). Skipped cases: docker-start, docker-pull-cold, docker-gpu-smi, docker-gpu-torch (docker_cli_missing); gpu-in-container n/a (no NVIDIA GPU). Recommendation: venv-fallback.
  full venv: process start 502.91 ± 6.30 ms, create+install cold 4.83 ± 0.51 s, warm 0.58 ± 0.03 s (n=5). Warm job start 0.50 s, within the 3 s target. Quick run rows INVALID (n=2) as designed.
evidence: lab/reports/raw/2026-11-01-S6-environments/l3-842983de__quick.json, lab/reports/raw/2026-11-01-S6-environments/l3-842983de.json

### R1-5 - Hardware scores (attempt 3, full, on AC) - OK
when: 2026-11-01 10:34 (local)
laptop: L3 (Windows 11, hostname TIWARI_JI)
commands:
    uv run python lab/spikes/s7_hardware.py run --name L3 --skip-gpu
result: AC Online before and after (61%). No ON BATTERY / no throttling warning. exit 3 (only warnings: no_blas_backend, gpu_skipped). cpu-python-1t 22.69 ± 0.45 MFLOP/s, cpu-python-all 143.35 ± 0.77 MFLOP/s, memory copy 32.22 ± 0.32 GB/s (n=5). Matches attempt 2 full within ~1.5%, so attempt 2 was likely only a brief charger drop.
  Note: no real GFLOPS number because numpy/torch are not installed (not installed per rules).
evidence: lab/reports/raw/2026-11-01-S7-hardware/l3-842983de__run2.json

### R1-7 - Push - OK
when: 2026-11-01 10:34 (local)
result: all lab/reports + REPORT.md pushed to lab/L3.
Summary for Lead: L3 = Windows 11, no NVIDIA GPU (AMD iGPU), Ryzen 7 5800HS 8c/16t, 15.4 GiB, Wi-Fi 192.168.31.236 5 GHz 87%, same room as L1, Wi-Fi profile PUBLIC (human choice), no Docker, no numpy/torch, uv installed this session.

## DONE

---
# Full campaign

### C0-a - Prep - OK
when: 2026-11-01 10:39 (local)
laptop: L3 (Windows 11, hostname TIWARI_JI)
commands:
    git fetch origin ; git merge origin/main ; git rev-parse --short HEAD ; uv sync --extra agent
result: HEAD 054763b9 (main merged into lab/L3). uv sync --extra agent exit 0.
  Pre-check for C0-b: Wi-Fi profile still Public; w32tm /query /status -> "The service has not been started. (0x80070426)" (Windows Time service stopped).
evidence: this entry

### C0-b - Human batch (power, Wi-Fi, clock) - OK
when: 2026-11-01 10:49 (local)
laptop: L3 (Windows 11, hostname TIWARI_JI)
commands:
    HUMAN: Wi-Fi profile -> Private ; powercfg /change standby-timeout-ac 0 ; hibernate-timeout-ac already 0 ; net start w32time && w32tm /resync (admin cmd) ; Windows Update pause (human)
    check: Get-NetConnectionProfile ; powercfg /query ; w32tm /query /status
result: Wi-Fi now PRIVATE. AC sleep 0 (never), AC hibernate 0. On AC power. w32time Running, last successful sync 10:48:44 from time.windows.com (status still shows Leap 3 / stratum 0 right after resync). Attempt 1 at Start-Service failed in a non-admin shell ("Cannot open w32time service"); worked from admin cmd.

### C0-c - Ready signal - OK
when: 2026-11-01 10:49 (local)
SIGNAL: L3-ready ip=192.168.31.236 os=windows host=TIWARI_JI

### C2.4-a - Network facts - OK
when: 2026-11-01 10:49 (local)
commands:
    netsh wlan show interfaces ; Get-NetAdapter | ft Name,LinkSpeed ; Get-NetConnectionProfile
result: Wi-Fi 5 GHz, Private, same room as L1.
evidence: lab/reports/raw/2026-11-01-V0.2-network-facts/L3.txt

### C2.0-h - Human batch 1 (S1 firewall) - OK
when: 2026-11-01 10:53 (local)
commands:
    HUMAN (admin PowerShell): New-NetFirewallRule "MeshForge S1 TCP" / "MeshForge S1 UDP", port 47201, RemoteAddress 192.168.31.0/24, Profile Private
result: both rules created, Enabled True, Profile Private. (First try was typed in cmd -> "not recognized"; second try in admin PowerShell OK.)

### C2.5-a - S1 server - OK
when: 2026-11-01 10:53 (local)
commands:
    uv run python lab/spikes/s1_network.py serve --bind 192.168.31.236 --idle 3600 --duration 14400
result: server running in background; listening TCP and UDP on 192.168.31.236:47201.
SIGNAL: s1-serve-up L3 ip=192.168.31.236

### C2.5-b - S1 client L3 -> L4 - SKIPPED
when: 2026-11-01 10:54 (local)
result: L4 not present (no origin/lab/L4). L4's catch-up does it later.

### C2.3-a - GPU stack - SKIPPED
when: 2026-11-01 10:54 (local)
result: no NVIDIA GPU on L3.

### C17-a - Download + check (done early while waiting on S1/S2) - OK
when: 2026-11-01 10:54 (local)
commands:
    gh release download v0.17.0 --repo tony19053000/finalyear --pattern "MeshForge-Agent-Setup-*" --pattern SHA256SUMS --dir $HOME\mf-pkgs
    Get-FileHash -Algorithm SHA256 vs SHA256SUMS
result: MeshForge-Agent-Setup-0.17.0-1.exe, 57,488,826 bytes, SHA256 3e95eb45...183753d = matches SHA256SUMS.
evidence: this entry (installer not committed)

### C2.5-b / C2.6 - WAIT status - WAITING
when: 2026-11-01 11:10 (local)
result: L3 S1 server still running on 192.168.31.236:47201. Waiting for L1's and L2's s1-client-done (then stop server + human removes S1 rules) and for L1's S2 pair with L3 (s2-serve-up L1). C17-a already done. Next after that: C2.r-a raw push, V0.2 checkpoint, then C17-b WAIT controller-up.
  Signals seen so far:
    L1 | SIGNAL: L1-ready ip=192.168.31.13 os=linux host=JARVISM3
    L2 | SIGNAL: L2-ready ip=192.168.31.155 os=windows host=AILESH
    L2 | SIGNAL: s1-serve-up L2 ip=192.168.31.155

### C3.2-a - Windows agent unit tests (run early, while waiting) - FAIL
when: 2026-11-01 11:22 (local)
laptop: L3 (Windows 11, hostname TIWARI_JI)
commands:
    uv sync --extra agent ; uv run pytest tests/unit/agent -q -p no:cacheprovider
result: exit 1. 2 failed, 3427 passed, 435 skipped in 339.86 s. Both failures are the same cause: the test creates a symlink and Windows refuses it for a normal user (Developer Mode off, not admin).
BUG: tests do not skip when symlinks are not allowed on Windows.
  FAILED tests/unit/agent/test_envs_g2_tester_round3.py::test_a_symlinked_dir_named_pycache_is_counted_as_a_link
  FAILED tests/unit/agent/test_v017_g4_windows_pure.py::test_a_root_that_is_itself_a_link_is_only_unlinked
  both: OSError: [WinError 1314] A required privilege is not held by the client (pathlib symlink_to, pathlib.py:1386)
  reproduce: Windows 11 Home, non-admin shell, Developer Mode off, run the two tests above.

### C4.15-g - Windows part G tests (run early, while waiting) - FAIL
when: 2026-11-01 11:22 (local)
laptop: L3 (Windows 11, hostname TIWARI_JI)
commands:
    uv sync --extra agent --group dev ; uv run pytest tests/unit/common/test_bundle_case_insensitive.py tests/component/agent/test_docker_sandbox.py -q -rs
result: exit 1, no tests ran. tests/component/agent/conftest.py line 28 pytest.importorskip("sqlalchemy", reason="needs the controller extra") raises Skipped at conftest import, which aborts the whole run (also the unit test file). Docker tests: not run (no Docker on L3 anyway).
BUG: the TODO command for C4.15-g installs only --extra agent, but tests/component/agent/conftest.py needs the controller extra (sqlalchemy); a module-level importorskip in a conftest aborts collection instead of skipping. Lead: fix the TODO line (add --extra controller?) or the conftest.

### C2.5-b (tail) - Stop L3 S1 server - OK
when: 2026-11-01 11:39 (local)
result: Seen SIGNAL s1-client-done L1 and s1-client-done L2. L3 S1 server stopped (port 47201 closed). Server writes no result file on L3 (results are on the client side). Human asked to remove the S1 firewall rules.

C2.5-b addendum (2026-11-01 11:42): HUMAN ran Remove-NetFirewallRule -DisplayName "MeshForge S1 TCP","MeshForge S1 UDP" (admin). Check: no "MeshForge S1*" rules left.

### C2.r-a - Push all raw files - OK
when: 2026-11-01 11:43 (local)
result: All L3 raw files are on origin/lab/L3: inventory TIWARI_JI.json, S7 hardware (l3-842983de*.json), S6 environments (l3-842983de*.json), network facts L3.txt. L3 ran no S1 client (only L3->L4, L4 absent). S2 pair with L1 (C2.6-a, optional) not started yet - its file will be pushed later if L1 runs it.
SIGNAL: v02-raw-pushed L3

### C4.15-g - Windows part G tests (attempt 2, Lead's new command) - OK
when: 2026-11-01 12:15 (local)
commands:
    uv sync --extra agent --extra controller --group dev
    uv run pytest tests/unit/common/test_bundle_case_insensitive.py tests/component/agent/test_docker_sandbox.py -q -rs
result: exit 0. 3 passed, 9 skipped. Docker tests SKIPPED (7, no Docker daemon on L3); GPU tests SKIPPED (2, no NVIDIA GPU).
SIGNAL: v04-g-done L3

C3.2-a rerun: waiting for Lead commit "test: skip symlink tests without privilege" (not on origin/main yet).

WAIT status (2026-11-01 12:15): no new S2 signal for ~40 min. L1 last: s2-serve-up L1 (waiting for L2's S2 client). L2 last: C2.5-c. Waiting for L3's S2 turn and controller-up.

### C3.2-a - Windows agent unit tests (attempt 2, after Lead fix) - OK
when: 2026-11-01 13:11 (local)
commands:
    git merge origin/main (HEAD 27ef9685, has "test: skip symlink tests without privilege")
    uv run pytest tests/unit/agent -q -p no:cacheprovider
result: exit 0. 3467 passed, 417 skipped, 0 failed in 287 s.

### C2.6-a - S2 second pair with L1 (optional) - SKIPPED
when: 2026-11-01 15:14 (local)
result: L1 did not start an S2 server for L3 (L1 REPORT: "Optional L3 S2 pair: not done yet"; L1 moved on to S3 with L2 and V0.17a C17-b). Optional step, nothing run.

### LEAD CHECKPOINT V0.2 - OK
when: 2026-11-01 15:14 (local)
result: all L3 V0.2 parts done (S1 server served L1+L2, rules removed, S6/S7/inventory/facts pushed). merged origin/main.
SIGNAL: V0.2-done

### C17-b - WAIT controller-up - WAITING
when: 2026-11-01 15:14 (local)
result: L1 is at C17-b (stopped old dev services); no controller-up yet. Human asked to get an invite code from L1's human ahead of the timed run.

### C17-b - WAIT controller-up - OK
- time: 2026-11-01 18:55
- L1 signal seen: controller-up url=https://192.168.31.13:47110 (fp in L1 report)
- invite code: to be given verbally by L1 human, not recorded

