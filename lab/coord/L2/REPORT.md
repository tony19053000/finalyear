# L2 REPORT
# L2 REPORT

### R1-1 - Who am I - OK
when: 2026-11-01 09:43 (local)
laptop: L2 (Windows 11 Home Single Language 10.0.26200, hostname AILESH)
commands:
    Get-CimInstance Win32_OperatingSystem / Win32_Processor / Win32_VideoController
    netsh wlan show interfaces
    Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias Wi-Fi
    Get-NetConnectionProfile
result: CPU 12th Gen Intel Core i5-12450H; RAM 7.6 GB; GPU NVIDIA GeForce RTX 2050 + Intel UHD; Wi-Fi IP 192.168.31.155; 5 GHz, ch 157, 802.11ax, signal 96%; same room as L1: yes.
    Network profile: Public. Human was asked to switch to Private; it is still Public (human prefers to keep Public for now). Must be Private before round 2.
evidence: this entry
QUESTIONS: Wi-Fi profile still Public - needed as Private for round 2 (S1/S2/S3 inbound connections).

### R1-2 - Tools - FAIL
when: 2026-11-01 09:50 (local)
laptop: L2 (Windows 11, hostname AILESH)
commands:
    git --version; uv --version; uv sync; uv run python --version; docker version
result: git 2.49.0.windows.1 OK. uv: not installed ("The term 'uv' is not recognized"), so uv sync / uv run were not possible. Docker: not installed (report only, not installed per TODO).
evidence: this entry
QUESTIONS: uv is needed for every remaining step. Asking the human whether to install uv (official installer) before continuing.

### R1-2 - Tools (retry after installing uv) - OK
when: 2026-11-01 10:05 (local)
laptop: L2 (Windows 11, hostname AILESH)
commands:
    (human approved) powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
    uv --version; uv sync; uv run python --version
    attempt 2: uv python install 3.12 --reinstall; uv sync; uv run python --version
result: uv 0.13.6 installed. Attempt 1: uv sync FAILED (exit 2) after downloading cpython 3.12.16.
    Attempt 2: same error from `uv python install`, but `uv sync` then completed and installed all packages; uv run python --version -> Python 3.12.16. git 2.49.0.windows.1. Docker: not installed.
evidence: this entry
BUG: (environment, not repo) uv 0.13.6 on Windows prints
    "error: Missing expected target directory for Python minor version link at `C:\Users\<user>\AppData\Roaming\uv\python\cpython-3.12.16-windows-x86_64-none`"
    when installing managed Python 3.12.16; the interpreter works anyway. Reproduce: fresh uv install, `uv python install 3.12`.

### R1-3 - Inventory (0.2.2) - OK
when: 2026-11-01 10:10 (local)
laptop: L2 (Windows 11, hostname AILESH)
commands:
    uv run python lab/inventory.py --out "lab/reports/inventory/AILESH.json"
result: exit 0. CPU i5-12450H (8 cores / 12 threads); RAM 7.6 GiB; GPU NVIDIA RTX 2050 4096 MiB (driver 581.83, CUDA 13.0) + Intel UHD; secure boot enabled; torch not installed; docker not installed; Wi-Fi 1201 Mbit/s; failed probes: none; problems: none. (Disk is not in the stderr summary; it is in the JSON.)
evidence: lab/reports/inventory/AILESH.json

NOTE (correction): the "when" times of R1-2 retry (10:05) and R1-3 (10:10) were estimated, not read from the clock; real times were about 09:40-09:46. Times from here on are read from the clock.

### R1-4 - GPU check (0.2.1 / 0.2.3) - OK (torch and docker parts not possible)
when: 2026-11-01 09:47 (local)
laptop: L2 (Windows 11, hostname AILESH)
commands:
    nvidia-smi
    uv run python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
    docker run ... -> not run, Docker not installed
result: nvidia-smi OK: NVIDIA GeForce RTX 2050, driver 581.83, CUDA 13.0, WDDM, 4096 MiB, 66C idle, 8W/50W.
    torch: not installed (ModuleNotFoundError: No module named 'torch'); not installed per TODO. Docker GPU test: SKIPPED, no Docker.
evidence: lab/reports/gpu/AILESH-nvidia-smi.txt

### R1-5 - Hardware scores (0.2.11) - FAIL (exit 3: no torch/numpy, GPU and BLAS not measured)
when: 2026-11-01 09:50 (local)
laptop: L2 (Windows 11, hostname AILESH), plugged in, Best performance, other apps closed (human confirmed)
commands:
    uv run python lab/spikes/s7_hardware.py run --quick --name L2
    uv run python lab/spikes/s7_hardware.py run --name L2
result: both runs exit 3. No "ON BATTERY" or "thermal throttling" warnings.
    quick: cpu-python-1t 19.23 +- 1.81 MFLOP/s; cpu-python-all 159.47 +- 7.06 MFLOP/s; memory copy 12.03 +- 1.05 GB/s
    full:  cpu-python-1t 22.02 +- 1.12 MFLOP/s; cpu-python-all 117.68 +- 3.90 MFLOP/s; memory copy 12.80 +- 0.53 GB/s
    Not measured: no_blas_backend (neither torch nor numpy installed), torch_not_installed (ADR-0011) -> no GPU score, no real GFLOPS.
evidence: lab/reports/raw/2026-11-01-S7-hardware/l2-d76354d8.json
BUG: the quick and full runs wrote the SAME file name (l2-d76354d8.json), so the full run overwrote the quick run's JSON. Only the full run's JSON is kept; quick numbers above are copied from stderr.
QUESTIONS: Should torch (CUDA build) be installed on L2 so the GPU can be scored? Not installed, per TODO.

### R1-6 - Environments (0.2.10) - OK for venv cases; Docker cases not run (exit 3)
when: 2026-11-01 09:51-09:52 (local)
laptop: L2 (Windows 11, hostname AILESH)
commands:
    uv run python lab/spikes/s6_environments.py run --quick --name L2
    (copied its JSON to l2-d76354d8__quick.json because the full run reuses the same file name)
    uv run python lab/spikes/s6_environments.py run --name L2
result: both exit 3. Docker not installed -> skipped: docker-start, docker-pull-cold, docker-gpu-smi, torch-cuda-in-container. Recommendation: venv-fallback (ADR-0004).
    full: venv start 1222.45 +- 87.33 ms (n=5); venv create+install cold 5.89 +- 0.30 s; warm 1.26 +- 0.14 s. Warm job start 1.22 s, within the <= 3 s target.
    quick (smoke, INVALID n=2): venv start 1349.61 ms; cold 5.99 s; warm 1.22 s.
evidence: lab/reports/raw/2026-11-01-S6-environments/l2-d76354d8.json (full), l2-d76354d8__quick.json (quick)
BUG: same as R1-5: quick and full runs write the same file name; the second run overwrites the first.

### R1-7 - Push - OK
when: 2026-11-01 09:53 (local)
result: all reports pushed to origin/lab/L2.
Open items for the Lead: Wi-Fi profile still Public (needed Private for round 2); torch not installed (no GPU score in S7); Docker not installed.

## DONE
