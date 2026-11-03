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

### C17-a - Download and check the packages - OK (done early while waiting for L2 v02-raw-pushed)
commands:
    gh release download v0.17.0 --repo tony19053000/finalyear --dir ~/mf-pkgs; sha256sum -c SHA256SUMS --ignore-missing
result: 3 files, all OK: meshforge-agent_0.17.0-1_amd64.deb, meshforge-controller_0.17.0-1_amd64.deb, MeshForge-Agent-Setup-0.17.0-1.exe.
evidence: lab/reports/raw/2026-11-01-V0.17-install/L1-download.txt

### C17-b - Stop the old dev services - OK
result: no `uv run meshforge-*` process running, no systemd --user meshforge units loaded, dev Compose stack has no running containers. Q21 dev-DB import: NOT done (default).

### C17-c - Controller install (0.17.3) - OK
commands:
    HUMAN: cd ~/mf-pkgs && sudo apt install ./meshforge-controller_0.17.0-1_amd64.deb
result: installed 0.17.0-1 (657 MB). Preflight "all checks passed" (3 expected warns: DB passwords/files/CA created later). "is running version 0.17.0" and "Create your fabric" shown.
    Harmless apt note: "_apt couldn't access ~/mf-pkgs" (home dir permissions; download done as root).
evidence: lab/reports/raw/2026-11-01-V0.17-install/L1-controller-install.txt

### C17-d - Create the fabric (0.17.4) - OK
commands:
    HUMAN: sudo meshforge-controller setup url; opened link in Firefox on L1; created the fabric. Admin user is `admin` (not `labadmin`, Lead decision); password kept by the human.
    check: curl -k https://192.168.31.13:47110/healthz; ss -ltn; journalctl -u meshforge-controller
result: page showed "Laptops join at 192.168.31.13:47101", dashboard https://192.168.31.13:47110/ (not 127.0.0.1 -> no controller.toml edit).
    journal: controller.fabric.created 13:22:06Z, setup.completed, restarting, fabric.ready. healthz 200 {"status":"ok"}, /v1/nodes 401 (auth required).
    Listening on 192.168.31.13:47100, :47101, :47110. Fabric id fab_01M6BMCGQBHTN059R0R5MY3WYW.
    CA SHA-256 fingerprint (not secret): 05:6B:7B:A3:97:89:CC:FB:A2:CC:A0:18:3F:2B:36:65:A3:99:7F:1D:4A:06:3A:51:98:72:8C:FF:45:72:45:85 (read from the TLS chain).
    Screenshot SKIPPED (Lead): the human's screenshot showed the password, so it is not saved.

### C17-e - Firewall - SKIPPED
result: ufw inactive, no rule needed.

### C17-f - compute login + doctor - OK
commands:
    HUMAN: compute login --controller https://192.168.31.13:47110 --fingerprint <CA fp above> -u admin  (password typed by the human)
    HUMAN: sudo meshforge-controller doctor
result: "logged in to https://192.168.31.13:47110 as admin (admin)", login lasts until 2027-01-30. doctor: 8x ok (DB, passwords 0600, CA present, dashboard), "all checks passed".

### C17-g - controller up - OK
SIGNAL: controller-up url=https://192.168.31.13:47110 fp=05:6B:7B:A3:97:89:CC:FB:A2:CC:A0:18:3F:2B:36:65:A3:99:7F:1D:4A:06:3A:51:98:72:8C:FF:45:72:45:85

### C17-h (part 1) - L1 agent package install - OK (join pending)
when: 2026-11-01 (install) / 2026-11-03 (recheck after 2-day break)
commands:
    before: uv run meshforge-agent identity show -> dev node_id node_01M6BMFVHJVZXC7AET3CFRE3AV, enrolled False
    HUMAN: cd ~/mf-pkgs && sudo apt install ./meshforge-agent_0.17.0-1_amd64.deb
result: installed 0.17.0-1 (291 MB). Package migrated the dev identity: "same node_id" node_01M6BMFVHJVZXC7AET3CFRE3AV,
    key fingerprint 2058534e9ab79eda... verified, old dir renamed identity.migrated-20261101.
    Install note: docker not given to the service by default (Give Docker access step). Same harmless _apt note as C17-c.
    2026-11-03 recheck: merged origin/main (da64df11), read ANNOUNCE.md + LEAD NOTE. IP still 192.168.31.13. meshforge-controller active, meshforge-agent active.
    `compute nodes` = 0 nodes: L1 agent not joined yet (invite + console join = HUMAN, next).

### C17-h / C17-i - ON HOLD (Lead/team priority change, ANNOUNCE 2026-11-03 (2))
result: join steps paused until ANNOUNCE says "joining may start". Full S1 Wi-Fi matrix L1-L4 first.

### S1-ALL-a - S1 server again (0.2.5 matrix L1-L4) - OK
commands:
    uv run python lab/spikes/s1_network.py serve --bind 192.168.31.13 --idle 3600 --duration 14400  (background; ufw inactive)
SIGNAL: s1-serve-up L1 ip=192.168.31.13

S1 client RUNNING L1-to-L3

S1 client RUNNING L1-to-L4

### S1-ALL-b - S1 client L1 -> L3 (MOHIT), L1 -> L4 (LAPTOP-7ORVM30G) - OK
when: 2026-11-03 (local)
commands:
    uv run python lab/spikes/s1_network.py client --link wifi --server 192.168.31.71
    uv run python lab/spikes/s1_network.py client --link wifi --server 192.168.31.244
result: both exit 0. Run one after the other (L1-to-L3 RUNNING line stayed open during the L4 run; no overlap with other clients).
    L1<->L3: TCP x1 L3->L1 171.0±14.8, L1->L3 191.2±11.9; x4 L3->L1 226.3±5.7, L1->L3 181.4±5.3 Mbit/s; UDP rtt 3.35±0.32 ms, jitter 0.73 ms, loss 0 %.
    L1<->L4: TCP x1 L4->L1 215.2±11.3, L1->L4 214.6±24.2; x4 L4->L1 218.0±42.1, L1->L4 238.4±15.5 Mbit/s; UDP rtt 2.61±0.11 ms, jitter 0.57 ms, loss 0 %.
evidence: lab/reports/raw/2026-11-03-S1-network/jarvism3-249e3a41__to-mohit_wifi-930c4d45.json, lab/reports/raw/2026-11-03-S1-network/jarvism3-249e3a41__to-laptop-7orvm30g_wifi-305efdfb.json
SIGNAL: s1-client-done L1-to-L3
SIGNAL: s1-client-done L1-to-L4

### S1-ALL-c - S1 matrix report L1-L4 (Wi-Fi) - OK
commands:
    stopped L1 S1 server; git checkout origin/lab/{L2,L3,L4} -- lab/reports/raw   (peers' raw files, not edited)
    uv run python lab/spikes/s1_network.py matrix --results <tmp dir: L1-L2 2026-11-01 file + all 5 2026-11-03 files> --out lab/reports/2026-11-03-S1-network.md --labels L1=JARVISM3,L2=AILESH,L3=Mohit,L4=LAPTOP-7ORVM30G --date 2026-11-03 --force
result: 6 result files, all 6 pairs filled, every cell n=5. Old TIWARI_JI files left out (retired L3).
    TCP x1 range 171-248 Mbit/s, x4 181-361 Mbit/s; RTT 2.61-3.54 ms; loss 0 % except L2-L3 0.10 %.
    TBD cells: Ethernet tables (NOT RUN, D425); host table link speed + power mode (hand sections, Lead). No INVALID cells.
    First run used L3=MOHIT (hostname case is "Mohit") -> regenerated with --force (no hand text existed yet).
evidence: lab/reports/2026-11-03-S1-network.md, lab/reports/raw/2026-11-03-S1-network/
SIGNAL: s1-matrix-done L1

### C17-h (part 2) - L1 agent installed, not joined - OK
when: 2026-11-03 (local)
result: install already done 2026-11-01 (part 1 above): dpkg meshforge-agent 0.17.0-1 "install ok installed", service active.
    Dev node_id before install: node_01M6BMFVHJVZXC7AET3CFRE3AV (the dev identity dir is now renamed .migrated-20261101, so `uv run ... identity show` no longer shows it; recorded from part 1).
    Package migrated the same node_id into /var/lib/meshforge/identity. Not joined yet (joint join of L1-L4 per Lead).
    Agent journal (last 50 lines, only console-listening/start/stop events, no secrets) saved.
    `meshforge-agent identity show` / `doctor` need sudo (system state): HUMAN runs them, output added in the next entry.
evidence: lab/reports/raw/2026-11-01-V0.17-install/L1-agent-journal.txt
SIGNAL: installed L1

### C17-h (part 3) - identity show + doctor after install (HUMAN ran with sudo) - OK
result: identity show: node_id node_01M6BMFVHJVZXC7AET3CFRE3AV (= dev node_id, migration kept it), session_epoch 0,
    key_fingerprint 2058534e9ab79eda..., path /var/lib/meshforge/identity, enrolled False ("not joined yet").
    doctor 0.17.0 (git 3fca750858ea): OK version/upgrade/gpu (RTX 3050 Laptop)/cuda 13.2/disk 27 GiB/firewall (ufw off)/peer off;
    WARN joined (expected: joint join pending), WARN docker (Give Docker access not done yet). "doctor: nothing needs attention".
    Waiting for the joint join (Lead). Human makes 4 invites on the dashboard (codes never written down).

### C17-x - admin password reset (HUMAN) - OK
result: human forgot the admin password. HUMAN ran `sudo meshforge-controller user reset-password admin` -> "password of admin reset (1 API token(s) revoked)".
    The CLI login token of C17-f is revoked -> `compute login` must be redone. Password not recorded anywhere.
    Browser: human reached the dashboard login page with Chrome "Not secure" (CA not yet imported; ~/meshforge-fabric-ca.crt exported for import, C8.10-a).

### C17-i (part 1) - invites for the joint join - in progress
result: HUMAN made invite inv_01M6GG62NQVEV7TTJ6D3BM7E1T (`fabric invite --ttl 2h`) but pasted the code into the agent chat -> treated as exposed:
    revoked with `compute fabric invites --revoke inv_01M6GG62NQVEV7TTJ6D3BM7E1T` (status revoked). Code not written anywhere in the repo.
    Setup-page invite inv_01M6BMCGQSRKVKVCTB... expired unused. HUMAN makes 4 fresh invites (L1-L4), codes on paper only.
    compute login redone after the password reset: OK as admin, valid until 2027-02-01.

### C17-i (part 2) - 4 invites ready - OK
result: HUMAN ran `sudo meshforge-controller fabric invite --ttl 2h` 4 times (L1-L4). `compute fabric invites`: 4 pending, 1 revoked, 1 expired.
    Codes kept by the human (paper/screen), never in a file. Valid ~2 h. Waiting for the Lead's go for the joint join.
SIGNAL: invites-ready L1 count=4

### C17-h (part 4) - L1 joined - OK
when: 2026-11-03 ~16:45 (local)
commands:
    sudo meshforge-agent join <code>   (code given by the human; typed into the human's terminal, never written to a file)
    HUMAN sudo: usermod -aG docker meshforge && systemctl restart meshforge-agent   (= "Give Docker access")
result: "joined fabric fab_01M6BMCGQBHTN059R0R5MY3WYW via 192.168.31.13:47101", node_id node_01M6BMFVHJVZXC7AET3CFRE3AV (same as dev: migration kept it),
    cert valid until 2026-12-03. `id meshforge` now includes docker. compute nodes: JARVISM3 online, CONN yes, 0.17.0.
    Console policy (Run jobs / GPU) and console screenshots: not done via console (joined from terminal) - the Lead may ask for it later. QUESTION: are Run jobs / GPU on by default after a CLI join?
evidence: lab/reports/raw/2026-11-01-V0.17-install/L1-agent-journal.txt
SIGNAL: joined L1 node=node_01M6BMFVHJVZXC7AET3CFRE3AV

### C17-j - Fabric up - OK
result: compute nodes: 4 nodes, all online, CONN yes, version 0.17.0:
    L1 JARVISM3 node_01M6BMFVHJVZXC7AET3CFRE3AV, L2 AILESH node_01M6GCKN7PYT2J297BT241P436,
    L3 Mohit node_01M6GGZ7VNCSRKFAEDH3AX7QQY, L4 LAPTOP-7ORVM30G node_01M6GGJP3FW5VHBDFKYQ6SF318 (was stuck "enrolling", now online).
evidence: lab/reports/raw/2026-11-01-V0.17-install/fabric.txt
SIGNAL: fabric-up nodes=4

### C17-k - hello job from L1 to each agent - FAIL (3 of 4: owner policy off), run 1
commands:
    compute run ./hello --node <id> -f   (docs/user/run-a-workload.md hello project), for L1, L2, L3, L4
result: L4 (LAPTOP-7ORVM30G): job_01M6GK4AA35542XWHPYZ971R1G ran (venv on Windows), printed "hello from the fabric".
    L1, L2, L3: refused 409 "the node does not accept jobs (its owner has not enabled [agent.policy] accept_jobs)".
    Answer to the C17-h QUESTION: Run jobs is OFF by default after a join; each owner must turn it on in the console (expected product behaviour, not a bug).
    Next: HUMAN turns on Run jobs (+ GPU) in the MeshForge console on L1, L2, L3; then rerun.
evidence: lab/reports/raw/2026-11-03-V0.17-install/C17-k-hello-run1.txt

### C17-k - run 2 - FAIL (same 3 of 4)
result: L4 ok (job_01M6GKSNJ8Q37V00VD3MB11PRD "hello from the fabric"); L1, L2, L3 refused 409 accept_jobs, although L2's console shows Run jobs + GPU ticked.
    Lead also ran it 17:35: L4 ok (job_01M6GMS6NQ6NA243PRNZ4J166C), L1/L2/L3 409.
BUG (Lead: F5, fix in progress): exec capability comes only from the agent Hello; "Run jobs" switched on after connect is logged
    (session.policy_updated accept_jobs=true) but not applied; L1's own agent never sent an update. Workaround: policy on, then restart the agent.
evidence: lab/reports/raw/2026-11-03-V0.17-install/C17-k-hello-run2.txt

### C17-k - run 3 (after F5 workaround: policy on + agent restart) - 3 of 4 OK
result: L2 job_01M6GN3WTTF3NF1P645FDN4YN1, L3 job_01M6GN3Y1XJJV4M79788F3YXBB, L4 job_01M6GN40SQF0JK30KFD6Q0VC23: "hello from the fabric".
    L1: still 409 accept_jobs. L1 agent was restarted 17:40:53 IST, but its journal shows no policy save from the console
    -> Run jobs on L1 most likely not saved yet. HUMAN: console Settings, tick + Save, restart agent; then rerun L1.
evidence: lab/reports/raw/2026-11-03-V0.17-install/C17-k-hello-run3.txt

### C17-k - run 4 (L1 after policy save + restart 18:05) - OK, all 4 laptops done
result: L1 agent restarted 18:05:04 IST -> journal agent.execution.ready + cpu_cgroup_ready (before: execution.disabled).
    L1 job_01M6GPJ0N0SJRNPR3S19Q5H8EP "hello from the fabric". With run 3 (L2, L3, L4): hello succeeded on every laptop.
    F5 workaround confirmed on Linux too: policy saved in console, then restart = jobs accepted.
evidence: lab/reports/raw/2026-11-03-V0.17-install/C17-k-hello-run4-L1.txt, ...-run3.txt
SIGNAL: V0.17a-done

## Phase V0.3

### C3.15-a - Inventory, checklist part C (0.3.15 c1, 0.3.3) - OK
commands:
    export MESHFORGE_CLI__CONTROLLER_URL=https://192.168.31.13:47110; uv run python lab/fabric_check.py --name L1 inventory
result: exit 0. 4 nodes online/connected, agent 0.17.0. Expected vs actual written: all 4 rows match their inventory JSON (CPU, threads, RAM, NVIDIA GPU/VRAM).
    L1 caps include docker, exec-docker, exec-gpu, nvidia-container-toolkit; L2/L4 exec-gpu without docker; L3 no GPU.
evidence: lab/reports/2026-11-03-V0.3-fabric-inventory.md, lab/reports/raw/2026-11-03-V0.3-fabric-inventory/l1-dffe8596.json, lab/reports/inventory/*.json (copied from L2-L4)

### C3.7-a - Wi-Fi drop, L3 (Mohit) - RUNNING (LIVE)
SIGNAL: C3.7-a GO L3 - L1 is at ">>> PULL" (5 reps): on L1's spoken "now" L3's human turns Wi-Fi OFF; at ">>> PLUG" Wi-Fi ON.

### C3.7-a - Wi-Fi drop, L3 (Mohit) - FAIL (exit 3; recorded, non-blocking)
commands:
    uv run python lab/fabric_check.py --name L1 watch --node Mohit --link wifi   (LIVE: L3 Wi-Fi off/on, Enter on L1)
result: 5 reps, rep 1 not counted (Enter pressed ~20 s after Wi-Fi off: operator timing). 4 valid reps -> rows INVALID (n=4 < 5).
    OFFLINE after Wi-Fi off: 4.86 / 3.31 / 4.72 / 5.64 s (mean 4.63) - all within 8 s.
    ONLINE after Wi-Fi on: 7.01 / 15.98 / 17.54 / 14.49 s (mean 13.75) - 3 of 4 EXCEED 10 s. Agent process restarted: no (every rep).
    Reading: the ONLINE time includes Windows Wi-Fi re-association + DHCP (Wi-Fi reconnect is slower than a cable plug, as the TODO says);
    the fabric part (reconnect once the network is back) is not separated in this measurement. Lead to judge.
evidence: lab/reports/2026-11-03-V0.3-fabric-watch.md, lab/reports/raw/2026-11-03-V0.3-fabric-watch/l1-dffe8596__mohit-cable-124530-a2ec565d.json
SIGNAL: C3.7-a done L3 (L3 human: Wi-Fi stays ON now)

### C3.7-a - Wi-Fi drop, L2 (AILESH) - FAIL (exit 3; recorded, non-blocking)
commands:
    uv run python lab/fabric_check.py --name L1 watch --node AILESH --link wifi   (LIVE)
result: rep 1 not counted (Enter 11.26 s after Wi-Fi off: operator timing). 4 valid reps -> INVALID (n=4).
    OFFLINE: 5.70 / 5.93 / 4.93 / 4.30 s (mean 5.22) - all within 8 s.
    ONLINE: 7.47 / 13.97 / 8.06 / 7.99 s (mean 9.37) - 3 of 4 within 10 s, rep 3 exceeded. Agent restarted: no (every rep).
    Note for the Lead: the tool counts the first rep as late every time on Wi-Fi (operator learning); a 6-rep run would give n=5.
evidence: lab/reports/2026-11-03-V0.3-fabric-watch.md, lab/reports/raw/2026-11-03-V0.3-fabric-watch/l1-dffe8596__ailesh-cable-125301-e8a1e463.json
SIGNAL: C3.7-a done L2

### C3.7-a - Wi-Fi drop, L4 (LAPTOP-7ORVM30G) - FAIL (1 of 6 reps over 10 s; recorded, non-blocking)
commands:
    uv run python lab/fabric_check.py --name L1 watch --node LAPTOP-7ORVM30G --link wifi --reps 6   (LIVE; 6 reps so a late first rep would still leave n=5 - none was late)
result: n=6, valid row. OFFLINE 5.33/5.03/4.78/5.51/4.62/4.71 s (mean 5.00) - all within 8 s.
    ONLINE 14.52/8.21/8.46/7.85/8.13/6.96 s (mean 9.02) - rep 1 exceeded 10 s, reps 2-6 within. Agent restarted: no (every rep).
    Summary C3.7-a, all 3 agents: OFFLINE always <= 6 s (pass); ONLINE on Wi-Fi often 7-8 s but 14-18 s in 7 of 14 valid reps
    (L3 3/4, L2 1/4, L4 1/6) - Wi-Fi re-association time is inside the number. Lead decides how the 10 s bound applies on Wi-Fi.
evidence: lab/reports/2026-11-03-V0.3-fabric-watch.md, lab/reports/raw/2026-11-03-V0.3-fabric-watch/l1-dffe8596__laptop-7orvm30g-cable-125717-503906e8.json
SIGNAL: C3.7-a done L4

### C3.7-b - Agent restart keeps node_id, L2 (AILESH) - OK
commands:
    uv run python lab/fabric_check.py --name L1 watch --node AILESH --action agent --link wifi --reps 6   (LIVE: Stop-/Start-Service MeshForgeAgent on L2)
result: 6 reps, all came back under the same node_id node_01M6GCKN7PYT2J297BT241P436, agent process restarted = yes every rep, end reason agent_closed.
    OFFLINE after stop 0.05-0.42 s. ONLINE after start 14.45 / 11.28 / 7.97 / 45.77 / 8.46 / 8.33 s (rep 4 slow: likely Start typed late; Windows service start ~8 s).
evidence: lab/reports/2026-11-03-V0.3-fabric-watch.md, lab/reports/raw/2026-11-03-V0.3-fabric-watch/l1-dffe8596__ailesh-agent-143345-64a9c64e.json

### C3.7-b - Agent restart keeps node_id, L3 (Mohit) - OK
commands:
    uv run python lab/fabric_check.py --name L1 watch --node Mohit --action agent --link wifi --reps 6   (LIVE: Stop-/Start-Service on L3)
result: 6 reps, same node_id node_01M6GGZ7VNCSRKFAEDH3AX7QQY every time, agent restarted = yes, end reason agent_closed.
    OFFLINE after stop 0.03-0.20 s. ONLINE after start 15.79 / 9.05 / 9.21 / 8.42 / 8.21 / 8.53 s.
evidence: lab/reports/2026-11-03-V0.3-fabric-watch.md, lab/reports/raw/2026-11-03-V0.3-fabric-watch/l1-dffe8596__mohit-agent-151409-0299391f.json

### C3.7-b - Agent restart keeps node_id, L4 (LAPTOP-7ORVM30G) - OK
commands:
    uv run python lab/fabric_check.py --name L1 watch --node LAPTOP-7ORVM30G --action agent --link wifi --reps 6   (LIVE: Stop-/Start-Service on L4)
result: 6 reps, same node_id node_01M6GGJP3FW5VHBDFKYQ6SF318 every time, agent restarted = yes, end reason agent_closed.
    OFFLINE after stop 0.06-0.18 s. ONLINE after start 7.25 / 5.25 / 4.98 / 6.40 / 5.49 / 6.48 s.
evidence: lab/reports/2026-11-03-V0.3-fabric-watch.md, lab/reports/raw/2026-11-03-V0.3-fabric-watch/l1-dffe8596__laptop-7orvm30g-agent-151945-1c8b5fb9.json

### C3.7-b - Agent restart keeps node_id, L1 (JARVISM3) - OK
commands:
    lab/reports/raw/2026-11-03-V0.3-fabric-watch/tools/l1_agent_test.sh: runs `fabric_check.py --name L1 watch --node JARVISM3 --action agent --link wifi --reps 6`
    and, at each >>> STOP / >>> START, sends Enter then `sudo systemctl stop|start meshforge-agent` (one person cannot press Enter and type sudo at once on the same laptop).
result: attempt 1 (Enter sent AFTER the stop): rep 1 "offline was reached 0.24 s BEFORE Enter" -> not counted; stuck waiting, stopped by the agent (no JSON). Fixed order (Enter first).
    attempt 2: exit 0. 6 reps, same node_id node_01M6BMFVHJVZXC7AET3CFRE3AV, agent restarted = yes, agent_closed.
    OFFLINE 0.03-0.11 s, ONLINE 1.06-1.18 s (local controller, no Wi-Fi hop). Agent active afterwards.
    Summary C3.7-b: all 4 laptops x 6 reps came back under the same node_id (criterion 4 passes).
evidence: lab/reports/2026-11-03-V0.3-fabric-watch.md, lab/reports/raw/2026-11-03-V0.3-fabric-watch/l1-dffe8596__jarvism3-agent-152726-ac0daa35.json

### C3.15-b - Controller kill, part F (0.3.15 c3) - OK
commands:
    tools/l1_controller_kill.sh: runs `fabric_check.py --name L1 restart-check --link wifi` and at each ">>> KILL" runs `sudo systemctl kill -s KILL meshforge-controller` (systemd restarts it after 5 s)
result: exit 0. 5 reps, 4/4 nodes back every rep, node_id kept, agent restarted = none, all within 15 s.
    Controller outage 6.44-6.87 s (systemd restart delay). All reconnected after 5.66 / 10.93 / 6.24 / 5.48 / 6.37 s.
    Harmless systemd message each kill: "Failed to send signal SIGKILL to auxiliary processes: Invalid argument" (main process killed; outage measured).
evidence: lab/reports/2026-11-03-V0.3-fabric-restart.md, lab/reports/raw/2026-11-03-V0.3-fabric-restart/l1-dffe8596__153155.json

### C3.15-c - 24 h soak - SKIPPED
result: superseded by the 72 h soak of phase V1.0 (V1.0-campaign.md L3.2).

### C3.x-a - Evidence - OK
result: lab/reports/2026-11-03-V0.3-fabric-{inventory,watch,restart}.md + raw folders committed.

### LEAD CHECKPOINT V0.3
result: C3.15-a OK, C3.7-a FAIL (OFFLINE always <= 6 s; ONLINE over 10 s in 7 of 14 valid Wi-Fi reps), C3.7-b OK (4 laptops x 6), C3.15-b OK.
SIGNAL: V0.3-done

## Phase V0.4 (2026-11-04)

### C4.0 - HUMAN BATCH - OK
result: human agreed to the ~4 GB image pull. IP still 192.168.31.13; controller + agent active; 4 nodes online/connected; 27 GB free.

### C4.15-a - Clock offset (A4) - OK (L1 part)
commands: timedatectl; timedatectl timesync-status (chrony not installed)
result: synchronized yes, NTP ntp.ubuntu.com, L1 offset +128.954 ms (vs NTP, poll 34 min). Waiting for SIGNAL clock-offset from L2/L3/L4.
SIGNAL: clock-offset L1 ms=+129

### C4.8-a - Pull the pinned images (A3) - OK
commands: docker pull python:3.12-slim@sha256:f77ac9e4...; docker pull pytorch/pytorch:2.14.0-cuda12.6-cudnn9-runtime@sha256:a77983eb...
result: both exit 0. Disk now 17 GB free (91 % used) - watch it for V0.5 (needs >= 20 GB free: C5.11-a may fail).
evidence: lab/reports/raw/2026-11-04-V0.4-exec/A3-pull.txt

### C4.15-b - CPU example on every laptop, part C (c1) - OK
commands: compute run workloads/examples/cpu-deterministic --node <L1|L2|L3|L4> -f; compute get <J>; sha256sum outputs/<J>/results/result.json
    (L1 via --node <L1-ID> instead of --local: same agent, same result)
result: all 4 exit 0 "done: 148933 primes, pi ~ 3.14728" (1.23 / 1.58 / 1.55 / 1.51 s).
    result.json sha256 = d85fca4c255d086a52b98aa22bc5aaa1ec00eb62f27b40cd1c0986e29b01c598 on all 4 (= golden digest).
    attempt output_digest identical on all 4: sha256:0562889f3aab9811...
    jobs: L1 job_01M6HBFZ4W0XJYE7JZWM8GP2ND, L2 job_01M6HBG1HZGJHH361BFZB9Y8CY, L3 job_01M6HBG6NAMMAK5QAEFCF9VAQD, L4 job_01M6HBGCP5Q0SHMERM5CYNCFJZ
evidence: lab/reports/raw/2026-11-04-V0.4-exec/C.txt

### C4.15-c - PyTorch on the CPU, part D1 (c1) - FAIL on L2/L3/L4 (expected per TODO; QUESTION), OK on L1
commands: copy of pytorch-train with compute.yaml {env base cuda-pytorch, gpu none, --device cpu}; compute run ./pytorch-cpu --node <Lx> -f
result: L1 job_01M6HBH8B3VDS2D1BG0XPM1S8E succeeded "done on cpu: final loss 0.018201" (Docker cuda-pytorch image).
    L2 job_01M6HBHBHB8XFK84R1TH9CXQZF, L3 job_01M6HBHDHT6WBGH60TZA00T89Q, L4 job_01M6HBHE8Q1CCX3MECASNZTXH0: failed (user_error), exit 1,
    "ModuleNotFoundError: No module named 'torch'" (no Docker on Windows -> subprocess/venv runtime without torch).
    No cross-laptop compare.py possible (only L1 has a result).
QUESTION (Lead): (1) the job asks env base cuda-pytorch; on a node without Docker the agent runs it in a venv WITHOUT torch and the failure is typed user_error,
    not env_error - is that intended? Should the planner skip nodes that cannot provide base cuda-pytorch? (2) How should the Windows laptops get torch for this test?

### C4.8-b - PyTorch on the GPU, part D2 (0.4.8) - OK on L1, FAIL on L2 (same cause)
commands: compute run workloads/examples/pytorch-train --node <L1-ID> -f (x2); compare.py; same with --node <L2-ID>
result: L1 job_01M6HBHQHWHQDEWAERM8GXECDR + job_01M6HBHV2YAHK73J3QE3X1NAC9: "done on cuda: final loss 0.018201" (0.27 / 0.25 s);
    compare.py: "IDENTICAL: metrics.json and weights.bin are byte-identical"; run.json cuda_device "NVIDIA GeForce RTX 3050 Laptop GPU". L1 OK.
    L2 (RTX 2050) job_01M6HBHYKSTNDHA1GZQ5Z6K10E: failed (user_error) "No module named 'torch'" (no Docker, subprocess runtime has no torch) - as the TODO predicted.
evidence: lab/reports/raw/2026-11-04-V0.4-exec/D.txt

### C4.15-d - Log latency, part E (c2) - L4 OK (L2, L3 waiting for their clock-offset)
commands: J=$(compute run lab/workloads/log-ticker --node <L4-ID>); uv run python lab/exec_check.py latency $J --name L4 --clock-offset-ms -304
    (offset = L4 -175 ms - L1 +129 ms; exec_check needs SSL_CERT_FILE=~/meshforge-fabric-ca.crt - it does not use the compute login CA. Lab-tool note for the Lead.)
result: attempt 1 job_01M6HBJZH2EEFMVZB0K4K67EFH: CERTIFICATE_VERIFY_FAILED; follower attached ~1 min late -> p50 2408 / max 7592 ms "within false" - INVALID (follower not connected before the ticks).
    attempt 2 job_01M6HBKM7V5MKR3FB01Q4941MV: 100 lines, p50 365.3, p95 450.3, max 527.4 ms, within true (bound 1000 ms).
evidence: lab/reports/raw/2026-11-04-V0.4-exec-check.jsonl (tool output)

### C4.15-e - Cancel + timeout on L2, part F (c3) - cancel OK, timeout FAIL (overshoot 3.47 s > 2 s)
commands: F1 sleeper (sleep 300) --node <L2-ID>, waited for running, uv run python lab/exec_check.py cancel $J --name L2
    F2 compute.yaml limits {timeout: 20s}; compute run . --node <L2-ID> -f; transitions from `compute jobs <J> --json`
result: F1 job_01M6HBMPPBVRV9NH30256888X2: state cancelled, 3.091 s (bound 5 s) - OK.
    F2 job_01M6HBN8SWHS9CZMQ3J8JJ58AZ: failed (deadline_exceeded), exit 126 - OK; running 18:45:03.063Z -> failed 18:45:26.531Z = 23.47 s,
    overshoot 3.47 s > 2 s bound - FAIL. Possible cause: the 20 s are counted from the process start on L2 (after venv/env prepare that happens
    after the controller marks "running"), plus Wi-Fi report delay. Lead to judge (BUG candidate: timeout reference point).
    LIVE leftover check (Task Manager on L2 for a leftover python after the cancel): asked of L2's human, see L2 REPORT.

### C4.15-d - Log latency, L2 - FAIL (max 1119.8 ms > 1000 ms)
commands: J=$(compute run lab/workloads/log-ticker --node <L2-ID>); exec_check.py latency $J --name L2 --clock-offset-ms -89  (L2 +40 - L1 +129)
result: job_01M6HDA6NYAGKTQ2TM13ADQAH4: 100 lines, p50 344.6, p95 874.6, max 1119.8 ms -> within false (bound 1000 ms). Single run, Wi-Fi.
    Note: the offsets come from NTP status (L1 ±poll 34 min, L2 w32tm), each with ~100 ms uncertainty; the max is 12 % over the bound.
    L2 signals seen: clock-offset L2 ms=40, v04-g-done L2. Leftover-python check after the F1 cancel: not yet in L2's REPORT.

### C4.15-d - Log latency, L3 - OK (with measured offset; L3 clock not synced)
commands: J=$(compute run lab/workloads/log-ticker --node <L3-ID>); exec_check.py latency $J --name L3 --clock-offset-ms 488
    (L3 has no clock-offset SIGNAL: its C4.15-a FAILED - w32time stopped, offset ~+617 ms vs time.windows.com; 617 - 129 = +488 vs L1)
result: job_01M6HER9G1XSY1ZFDJP6WS7BRF: 100 lines, p50 229.6, p95 342.6, max 380.7 ms, within true.
    Uncertain: offset from L3's stripchart (3 samples) vs L1's NTP status. Re-run after L3's human starts w32time + resync if the Lead wants it.
    C4.15-d summary: L4 max 527 ms OK, L3 max 381 ms OK, L2 max 1120 ms FAIL.

### C4.x-a - Evidence - OK
result: lab/reports/raw/2026-11-04-V0.4-exec/{A3-pull,C,D}.txt + lab/reports/raw/2026-11-03-V0.4-exec-check.jsonl. Part G: v04-g-done L2 + L3 (L4 Windows: see its REPORT).
    Open: L2 leftover-python check after the F1 cancel not yet reported by L2.

### LEAD CHECKPOINT V0.4
result: C OK (4/4 identical golden digest); D1/D2 OK on L1 only (Windows no torch - QUESTION); E: 2/3 OK; F cancel OK, timeout overshoot 3.47 s FAIL.
SIGNAL: V0.4-done

## Phase V0.5 (2026-11-04)

### C5.11-a (part) - disk - OK after cleanup
result: 17 GB free after V0.4 pulls (< 20 GB). Human approved removing ONLY pytorch/pytorch:2.8.0 (10.8 GB, S6 spike image) + unused build cache (3.3 GB);
    other projects' images left alone. Then 30 GB free; 25 GB after the 5 GB dataset. ufw inactive (no 5201/47120 rules). iperf3: not installed (HUMAN apt install pending).

### C5.11-b - Part C dataset + reader - OK
result: ~/mf-lab/ds5g = 5 x 1 GiB random part*.bin + labels.csv (5.1 G); ~/mf-lab/reader/{main.py,compute.yaml} (input ds5g@1 -> data/ds) as written.
    Part B check: `compute nodes` peer_endpoint = None on ALL 4 agents (peer serving is Off by default, doctor "peer Off").
QUESTION: parts G/H need a peer endpoint. Per TODO, skipped unless owners turn on peer serving in the console (+ agent restart, bug F5?). Asked the human.

### C5.11-c - Part D2 iperf3 server - started
result: HUMAN installed iperf3 (/usr/bin/iperf3). `iperf3 -s -B 192.168.31.13` running in background. L2: run the client and record the receiver Bitrate.
SIGNAL: iperf3-server-up ip=192.168.31.13
    correction: the manual `iperf3 -s` failed "Address already in use" - the apt install started the iperf3 systemd service (active, *:5201). That service is the server; same port, L2 unaffected.

### C5.11-d - Part E upload once (c1) - OK
commands: compute data push ds5g ~/mf-lab/ds5g (x2); append "c,d" to labels.csv; push again
result: push 1: "uploaded 6 files / 5368752863 bytes, skipped 0", "registered ds5g@1 (6 files, 5.0 GiB)", 37.1 s (local controller, no Wi-Fi hop).
    push 2: "uploaded 0 files / 0 bytes, skipped 6", "already registered as ds5g@1", 7.0 s (hashing only).
    push 3: "uploaded 1 files / 43748 bytes, skipped 5", "registered ds5g@2". (43748 bytes for a ~12-byte csv: QUESTION - is that the chunk/manifest framing? not a fail.)
evidence: lab/reports/raw/2026-11-04-V0.5-data/E.txt

### C5.11-e - Part F 5 GB fetch to L2 (c2, Wi-Fi) - attempt 1 FAIL (node_lost)
commands: cd ~/mf-lab/reader; compute run . --node <L2-ID> -f   (input ds5g@1; L2 iperf3: up 237 / down 169 Mbit/s, 192 GB free)
result: job_01M6HFPX055QCKC2B6HFSWN7JE failed (node_lost) after 17 s, exit 126. Controller journal: dispatched to L2, then within ~10 s
    node.lease_expired for L2, L3 AND L4 (all three Windows agents at once), session.closed L2, retry_scheduled node_offline, attempt lost; pinned job -> failed.
    All 4 nodes back online a few seconds later.
BUG candidate (Lead): starting a 5 GB fetch over Wi-Fi made every Wi-Fi agent miss its 6 s lease - bulk data starves the control-plane heartbeats
    on a shared Wi-Fi (or the fetch shares the agent's connection/event loop with the session). Reproduce: run part F on Wi-Fi. Rerunning once to see if it repeats.
evidence: lab/reports/raw/2026-11-04-V0.5-data/F-attempt1.txt, F-attempt1-controller-journal.txt

### C5.11-e - Part F attempt 2 - OK (after one transfer_error retry)
result: job_01M6HG3V035J571YC78TT392DR succeeded, 412 s total. Attempt 1 (att_01M6HG3V1E...) failed transfer_error after 84 s of preparing
    (failure_detail null), auto-retried on L2; attempt 2 staged 20:04:23 -> running 20:09:36 = 313 s, ran 10 s.
    L2 printed "dataset sha256 b9124ddf4d3e9b6fd609354190cd4ac38907a1b11f72fbcfb0926b6c4976c113" = L1 local hash of ds5g part*.bin: data intact.
    Throughput (attempt 2 staging): 5 GiB / 313 s ~ 137 Mbit/s vs iperf3 L1->L2 169 Mbit/s = ratio ~0.81 (upper bound: attempt 1 may have cached part of it).
    Criterion 2 is Ethernet: NOT RUN (D425); Wi-Fi number recorded, no pass mark.
BUG candidate (Lead): transfer_error with failure_detail=null (no reason given). Together with attempt-1 node_lost: big fetches on Wi-Fi are fragile.
evidence: lab/reports/raw/2026-11-04-V0.5-data/F-attempt2.txt, F-attempt2-controller-journal.txt

### C5.11-i - Part J compute node prepare (0.5.8) - OK
commands: envs.yaml {base python-cpu, requirements numpy==2.1.3}; compute node prepare --all --image python-cpu --envs ~/mf-lab/envs.yaml (x2)
result: run 1 (60.3 s, exit 1): L1 image hit + env built 14.0 s; L2/L3/L4 image "failed (env_error/docker_unavailable ...)" (typed, no hang) + env built by venv 9.6-21.1 s.
    run 2 (3.8 s, exit 1): every env "hit" (0.1-0.7 s), L1 image hit, Windows images typed docker_unavailable again. Exit 1 = expected (no Docker on Windows).
evidence: lab/reports/raw/2026-11-04-V0.5-data/J.txt

### C5.11-e - addendum from L2's agent log (L2 REPORT C5.11-e)
result: job 1 (node_lost): 8 x agent.cas.source_dropped "chunk fetch failed: ConnectError" + stage_retry in 27 s, then L2 reconnect loop
    `discovery_failed controller_not_found` for ~4 min 13 s (back ONLINE 20:00:30 UTC) - same discovery weakness L4 hit at join (mDNS).
    job 2 attempt 1: "chunk fetch failed: RemoteProtocolError" with the session up -> failed transfer_error, no retry; the detail is not passed to the controller (failure_detail null).
    L2: SIGNAL V0.5-done.

### C5.11-f - Part G first half: reader on L3 - OK after one transfer_error retry (peer half SKIPPED)
commands: compute run ~/mf-lab/reader --node <L3-ID> -f   (L3 free 174.5 GB; peer serving off everywhere -> controller is the only source)
result: job_01M6HHQX4TS0MF1JPBN2NNCHQR succeeded in 464 s. Attempt 1 failed transfer_error after 95 s staging (same as L2 job 2),
    attempt 2 staged 20:33:01 -> running 20:38:54 = 354 s (~121 Mbit/s for 5 GiB). L3 printed the same dataset sha256 b9124ddf... = data intact.
    Expected "transfer lines from the controller AND from L2": NOT possible - no peer endpoints (QUESTION above). Second half (L1 Wi-Fi vs others Ethernet): NOT RUN (D425).
    Pattern: the FIRST attempt of every 5 GB fetch so far failed (L2 node_lost, L2 transfer_error, L3 transfer_error); the retry always succeeded.
evidence: lab/reports/raw/2026-11-04-V0.5-data/G-L3.txt (+ L3's L3-agent.txt)
SIGNAL: v05-f-done L3

### C5.11-g - Part H kill + resume on L4 (c3) - job OK; resumed_chunks to be read from L4's agent log
commands: L4 prep: agent stopped, cas+gc deleted, started (L4 REPORT, free 231 GB). J1 = compute run ~/mf-lab/reader --node <L4-ID> (20:52:55Z);
    LIVE: human relayed "STOP NOW" -> L4 Stop-Service, 5 s, Start-Service. J2 = same, -f.
result: J1 job_01M6HJZDCDN6G3TDFDPQJW1J2Q failed: attempt 1 transfer_error by itself at 20:55:33 (before the stop, ~158 s staging - same first-attempt bug),
    attempt 2 node_lost at 20:56:21 (the agent stop). J2 job_01M6HK8J40V20JA99WRWGT11JK succeeded: preparing 20:57:55 -> running 21:05:59 (484 s), same dataset sha256 b9124ddf....
    The controller journal and `compute logs` have no resumed_chunks field; the cache/resume line is in L4's agent log (agent.cas.input_staged) - L4 records it in L4-agent.txt. Pass/fail of c3 (resumed_chunks > 0) waits for that.
    Note: the stop came late (relay by human), so most of J1's bytes were from attempt 1; that is fine for resume (verified chunks stay in cas).
evidence: lab/reports/raw/2026-11-04-V0.5-data/H-L4.txt
SIGNAL: v05-h-done L4 (L4: save agent.cas.* lines incl. resumed_chunks, then V0.5-done)
