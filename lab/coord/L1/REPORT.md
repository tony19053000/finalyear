# L1 REPORT

### R1-1 - Who am I - OK
when: 2026-11-01 (local)
laptop: L1 (Ubuntu 24.04.4 LTS, kernel 6.8.0-147-generic, hostname JARVISM3)
commands:
    lsb_release -ds; hostname; lscpu; free -h; lspci; ip -4 addr show wlp0s20f3; nmcli dev wifi; nvidia-smi
result: CPU 12th Gen Intel i7-12650H (16 threads); RAM 15 GiB; GPU NVIDIA GeForce RTX 3050 Mobile (GA107M) + Intel UHD.
    Wi-Fi wlp0s20f3 192.168.31.13/24, SSID JARVIS, 5 GHz (ch 157, 5785 MHz), signal 100, rate 540 Mbit/s. L1 is the reference laptop (same room n/a).
BUG: NVIDIA driver not loaded. `nvidia-smi` -> "NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver".
    `lsmod | grep nvidia` empty. Secure Boot enabled (kernel locked down). DKMS modules exist for 6.8.0-147 (dated 2026-10-14).
    Two driver packages installed: nvidia-driver-590-open 590.48.01 and nvidia-driver-595-open 595.91.07.
    Likely cause: unsigned / MOK-not-enrolled module under Secure Boot, or the two-package conflict. Not fixed (RULES: no driver/Secure Boot changes by the agent).
    Affects R1-4, R1-5 GPU part, C2.1-a, C2.11-a, C4.8-b, C9.10-d. Needs a human/Lead decision.

### R1-2 - Tools - OK
when: 2026-11-01 (local)
laptop: L1 (Ubuntu 24.04.4, JARVISM3)
commands:
    git --version; uv --version; uv sync; uv run python --version; docker version
result: git 2.43.0; uv 0.10.12; uv sync exit 0 (no errors); Python 3.12.13; Docker client 29.7.2 / server 29.7.2 (user can reach the daemon).

### R1-3 - Inventory (0.2.2) - OK
when: 2026-11-01 (local)
laptop: L1 (Ubuntu 24.04.4, JARVISM3)
commands:
    uv run python lab/inventory.py --out "lab/reports/inventory/$(hostname).json"
result: exit 0. CPU i7-12650H 10 cores / 16 threads; RAM 15.3 GiB; GPUs Intel UHD + RTX 3050 Mobile (driver NOT working, see R1-1 BUG);
    disk / 188G, 40G free (78% used); docker 29.7.2 runtimes incl. nvidia; torch not installed; eno1 down, wlp0s20f3 up. Problems: 1 (nvidia driver).
evidence: lab/reports/inventory/JARVISM3.json

### R1-4 - GPU check (0.2.1 / 0.2.3) - FAIL
when: 2026-11-01 (local)
laptop: L1 (Ubuntu 24.04.4, JARVISM3)
commands:
    nvidia-smi
    uv run python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
result: nvidia-smi fails: "couldn't communicate with the NVIDIA driver" (module not loaded, Secure Boot on; see R1-1 BUG).
    torch: not installed (ModuleNotFoundError). Docker GPU container NOT run: pointless without a loaded driver (no download asked).
BUG: see R1-1 - NVIDIA driver not loaded on L1. Non-blocking for round 1.

### R1-5 - Hardware scores (0.2.11) - OK (partial: no BLAS, no GPU)
when: 2026-11-01 (local)
laptop: L1 (Ubuntu 24.04.4, JARVISM3), AC online, power profile performance
commands:
    uv run python lab/spikes/s7_hardware.py run --quick --name L1 --skip-gpu
    uv run python lab/spikes/s7_hardware.py run --name L1 --skip-gpu
result: quick exit 3: python 1t 48.62±1.98 MFLOP/s, all 423.12±13.49 MFLOP/s, mem copy 35.54±2.44 GB/s.
    full exit 3: python 1t 51.22±0.29 MFLOP/s, all 298.93±102.27 MFLOP/s (high spread), mem copy 37.05±0.31 GB/s.
    Exit 3 = typed skips: no_blas_backend (no torch/numpy), gpu_skipped (driver not loaded, R1-1). No ON BATTERY / throttling warning.
evidence: lab/reports/raw/2026-11-01-S7-hardware/l1-dffe8596__quick.json, lab/reports/raw/2026-11-01-S7-hardware/l1-dffe8596.json

### R1-6 - Environments (0.2.10) - OK (GPU cases skipped)
when: 2026-11-01 (local)
laptop: L1 (Ubuntu 24.04.4, JARVISM3)
commands:
    uv run python lab/spikes/s6_environments.py run --quick --name L1
    uv run python lab/spikes/s6_environments.py run --name L1
result: quick exit 3 (all rows INVALID n=2, as expected). full exit 3: recommendation docker; container start 261.60±10.72 ms,
    venv start 110.29±10.98 ms, small image cold pull 10.05±0.37 s, venv cold install 6.82±2.64 s, warm 0.09 s; warm start overhead within 3 s target.
    Skipped (typed): docker-gpu-smi and docker-gpu-torch = no_gpu_on_host (driver not loaded, R1-1 BUG).
evidence: lab/reports/raw/2026-11-01-S6-environments/l1-dffe8596__quick.json, lab/reports/raw/2026-11-01-S6-environments/l1-dffe8596.json

### R1-7 - Push - OK
result: all round-1 evidence pushed to lab/L1. Open item: NVIDIA driver not loaded (R1-1 BUG) - needs human/Lead before C2.1-a.

## DONE (round 1)

# Campaign

### C0-a - Same commit, prerequisites - OK
when: 2026-11-01 (local)
laptop: L1 (Ubuntu 24.04.4, JARVISM3)
commands:
    git fetch origin && git checkout lab/L1 && git merge origin/main; git rev-parse --short HEAD; uv sync --extra agent --extra controller
result: origin/main = 3b135094 (already merged); HEAD 1e23f0d2 (lab/L1); sync exit 0.

### C0-b - Power and clock - OK
when: 2026-11-01 (local)
result: AC online, power profile performance, GNOME automatic suspend on AC = nothing (off). System clock synchronized: yes, NTP active.
    date -u +%s = 1793510392. Lid: human keeps it open.

### C0-d - L1 ready - OK
result: IP re-checked with ip -4 addr: unchanged.
SIGNAL: L1-ready ip=192.168.31.13 os=linux host=JARVISM3

### R1-4b - GPU driver fix + recheck - OK
when: 2026-11-01 11:02 (local)
laptop: L1 (Ubuntu 24.04.4, JARVISM3)
commands:
    modinfo nvidia.ko (signer "ubuntu Secure Boot Module Signature key"); mokutil --test-key /var/lib/shim-signed/mok/MOK.der -> "not enrolled"
    HUMAN: sudo mokutil --import /var/lib/shim-signed/mok/MOK.der; reboot; MOK manager Enroll MOK
    after: mokutil --test-key -> "already enrolled"; nvidia-smi; lab/inventory.py (re-run, file overwritten with the fixed state)
result: root cause of R1-1 BUG = the local DKMS signing key (MOK) was never enrolled, so Secure Boot refused the module. Fixed by the human.
    nvidia-smi: NVIDIA GeForce RTX 3050 Laptop GPU, 4096 MiB, driver 595.91.07, CUDA 13.2. Inventory problems: none. Secure Boot still enabled.
    Note: nvidia-driver-590-open package still installed beside 595 (not touched). R1-5/R1-6 GPU parts get re-run in C2.11-a / C2.10-a.
evidence: lab/reports/inventory/JARVISM3.json
