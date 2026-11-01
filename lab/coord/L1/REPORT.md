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
