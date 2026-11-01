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

### C0-c - Firewall state - OK
result: human ran `sudo ufw status`: inactive. All later L1 "open port" steps are not needed.

### C2.0-h - HUMAN BATCH - OK
result: downloads agreed (CUDA torch ~2.5 GB, S6 images incl. PyTorch image, CUDA base test image). ufw inactive -> no rules. Router DHCP reservations: no.

### C2.1-a - GPU stack on L1 (0.2.1, 0.2.3) - OK
when: 2026-11-01 11:09 (local)
commands:
    nvidia-smi; uv sync --extra agent --extra controller --extra gpu
    uv run python -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
    docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
result: nvidia-smi RTX 3050 Laptop 4 GiB, driver 595.91.07; sync exit 0; torch 2.14.0+cu130 True "NVIDIA GeForce RTX 3050 Laptop GPU";
    container printed the GPU table (docker exit 0).
evidence: lab/reports/raw/2026-11-01-V0.2-gpu/L1.txt

### C2.4-a - Network facts (0.2.4) - OK
commands:
    nmcli dev wifi; iw dev wlp0s20f3 link (iw NOT installed -> used nmcli rate + /proc/net/wireless); ip -4 addr
result: 5 GHz ch 157 (5785 MHz), signal 100, link rate 540 Mbit/s, IP 192.168.31.13/24 (DHCP, lease dynamic). DHCP reservations: no (router does not allow / human said no) -> IPs may change; re-check before each phase.
evidence: lab/reports/raw/2026-11-01-V0.2-network-facts/L1.txt

### C2.10-a - S6 environments with GPU (0.2.10) - OK
commands:
    uv run python lab/spikes/s6_environments.py run --quick --name L1   (exit 0)
    uv run python lab/spikes/s6_environments.py run --name L1           (exit 0; pulled pytorch/pytorch:2.8.0-cuda12.6-cudnn9-runtime ~3.7 GB)
result: recommendation docker. container start 157.26±6.69 ms, venv start 99.62±1.98 ms, small image cold pull 10.36±0.57 s,
    venv cold install 5.46±1.43 s, warm 0.06 s, PyTorch-CUDA container to CUDA ready 1122.44±44.22 ms. No skipped cases. All within the 3 s target.
evidence: lab/reports/raw/2026-11-01-S6-environments/l1-dffe8596__quick__run2.json, lab/reports/raw/2026-11-01-S6-environments/l1-dffe8596__run2.json

### C2.5-a - S1 server (0.2.5) - OK
result: other laptops: L2 192.168.31.155 (windows, AILESH), L3 192.168.31.236 (windows, TIWARI_JI), L4 not present. ufw inactive (no rule).
    uv run python lab/spikes/s1_network.py serve --bind 192.168.31.13 --idle 3600 --duration 14400  (running in background)
SIGNAL: s1-serve-up L1 ip=192.168.31.13

### C2.5-b - S1 client L1 -> L2, L3 (0.2.5) - OK
commands:
    uv run python lab/spikes/s1_network.py client --link wifi --server 192.168.31.155 --server 192.168.31.236
result: exit 0. L4 not present.
    L1<->L2 (AILESH): TCP x1 L2->L1 194.7±7.6, L1->L2 212.1±35.7; x4 L2->L1 206.5±41.8, L1->L2 238.5±25.4 Mbit/s; UDP rtt 2.70±0.55 ms, loss 0 %.
    L1<->L3 (Tiwari_ji): TCP x1 L3->L1 224.1±10.3, L1->L3 201.0±32.2; x4 L3->L1 275.9±46.9, L1->L3 182.4±21.2 Mbit/s; UDP rtt 2.07±0.21 ms, jitter 0.67 ms, loss 0 %.
    L1 server then stopped (SIGINT; no run targets L1). No ufw rule to remove (inactive).
evidence: lab/reports/raw/2026-11-01-S1-network/jarvism3-249e3a41__to-ailesh_wifi-e92a7f9a.json, lab/reports/raw/2026-11-01-S1-network/jarvism3-249e3a41__to-tiwari_ji_wifi-3f7a2a51.json
SIGNAL: s1-client-done L1

### C2.11-a - S7 again with CUDA torch (0.2.11) - OK
commands:
    cp l1-dffe8596.json l1-dffe8596__nonumpy.json (copy aside; the tool also wrote a new __run2 file, nothing overwritten)
    uv run python lab/spikes/s7_hardware.py run --name L1
result: exit 0, AC online, no ON BATTERY / throttling warning.
    GPU RTX 3050 Laptop: fp32 2.42±0.02 TFLOP/s, fp16 13.89±0.34, bf16 14.00±0.81 TFLOP/s, device copy 179.66 GB/s.
    CPU float64 1t 65.64±0.21, all 269.11±63.61 GFLOP/s (high spread); python 1t 49.6, all 349.9 MFLOP/s; host mem copy 25.05, triad 27.32 GB/s.
    Note: S2 L2->L3 S1 may have been running on L2/L3 at the same time (not involving L1).
evidence: lab/reports/raw/2026-11-01-S7-hardware/l1-dffe8596__run2.json, lab/reports/raw/2026-11-01-S7-hardware/l1-dffe8596__nonumpy.json

### C2.6-b - S2 L1 as server (0.2.6) - started
commands:
    uv run python lab/spikes/s2_crossos.py serve --bind 192.168.31.13 --name L1   (background; ufw inactive, no rule)
SIGNAL: s2-serve-up L1

### C2.6-b - S2 L1 as server, L2 as client (0.2.6) - OK
result: L2 reported SIGNAL: s2-done L2-to-L1 (L2's REPORT has the numbers). L1 server served 24 sessions from 192.168.31.155, then stopped (SIGINT).
    ufw inactive: no 47202:47203 rule to remove. Optional L3 S2 pair: not done yet (after C2.6-a).

### C2.6-a - S2 L1 as client, L2 as server (0.2.6) - OK
commands:
    uv run python lab/spikes/s2_crossos.py client --server 192.168.31.155 --link wifi --name L1 --no-inbound-rule --note "..."
result: exit 0. Linux client -> Windows 11 server: every case yes (http-ping, http-blob, grpc-unary, grpc-stream, bidi idle 30 s/120 s keepalive on/off), outbound-only yes.
    HTTP first byte 44.25 ms, HTTP stream 154.0 Mbit/s, gRPC unary RTT 5.66 ms, gRPC stream 179.4 Mbit/s.
evidence: lab/reports/raw/2026-11-01-S2-crossos/l1-dffe8596__to-l2_wifi-e7716ae1.json
SIGNAL: s2-done L1-to-L2

### C2.7-a - S3 L1 server (0.2.7) - started
commands:
    uv run python lab/spikes/s3_transport.py serve --bind 192.168.31.13   (background; ufw inactive)
SIGNAL: s3-serve-up L1

### C2.7-a - S3 L1 server, L2 client (0.2.7) - OK
result: L2 reported SIGNAL: s3-done L2 (its client run measures both directions; numbers in L2's REPORT/raw JSON). L1 server stopped (SIGINT).
    Ethernet run: NOT RUN (D425).

### C2.x-a - Remove spike firewall rules - OK
result: ufw inactive the whole time: no rules were added, none to remove.
