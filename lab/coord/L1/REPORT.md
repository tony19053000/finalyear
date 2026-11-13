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

### C5.11-g - Part H result (from L4's agent log) - FAIL c3 (resume not demonstrated)
result: J2 agent.cas.input_staged: blobs_hit 2, blobs_fetched 4, bytes_fetched 3,825,205,248, no resumed_chunks field on the dataset line;
    code tree chunks_resumed 0. Only whole-blob reuse (2 of 6 blobs, ~1.5 GB) is shown; partial-chunk resume (> 0) NOT demonstrated.
    Caveats: (1) the stop came late via human relay, after attempt 1 had already failed transfer_error by itself; (2) the dataset input_staged line
    does not carry chunks_resumed, so c3 cannot be read from it. QUESTION/BUG candidate for the Lead: add chunks_resumed to the dataset staging line.
    Part I (GC): SKIPPED on L4 (L4 REPORT).

### LEAD CHECKPOINT V0.5
result: E (upload once) OK; F Wi-Fi 5 GB to L2 OK on retry (~0.81 of iperf3); G peer half SKIPPED (no peer endpoints), controller-only fetch to L3 OK on retry;
    H resume NOT demonstrated (blob reuse only); I SKIPPED; J prepare OK. Main bug: first attempt of every 5 GB Wi-Fi fetch failed
    (node_lost/ConnectError + discovery controller_not_found ~4 min on L2; RemoteProtocolError on L2/L3/L4), failure_detail not passed to the controller.
    L2, L3, L4: V0.5-done.
SIGNAL: V0.5-done

## Phase V0.6 (2026-11-04)

### C6.13-a - Part C network matrix (0.6.6) - attempt 1 FAIL (no pair measured: peer serving off)
commands: compute network; compute network --json
result: "No node pair measured yet"; probes on, started 0, measured 0, failed 0. Only controller->node links from transfer history:
    L2 7.8 MB/s, L4 4.5 MB/s, L3 6.7 MB/s (1 sample each). Node-to-node probes need `[agent.peer] bind` on every agent (ANNOUNCE 2026-11-04).
    Linux path for the installed agent: /etc/meshforge/config.toml (SYSTEM_LINUX_CONFIG_DIR; file does not exist yet). HUMAN turns peer serving on in all 4, then retry after 5 min.
evidence: lab/reports/raw/2026-11-04-V0.6-sched/C-network.txt, C-network.json

### C6.13-a (prep) - L1 peer serving on - OK
commands: HUMAN: printf '[agent.peer]\nbind = "192.168.31.13"\n' | sudo tee /etc/meshforge/config.toml && sudo systemctl restart meshforge-agent
result: agent restarted 02:57:20 IST; journal agent.peer.listening; ss: 192.168.31.13:47120 LISTEN. (Correction: my earlier "peer_endpoint None" checks read a JSON key
    that does not exist in `compute nodes --json` - not evidence that peers were off; V0.5 doctor "peer Off" and the empty probe matrix are.)
SIGNAL: peer-on L1 ip=192.168.31.13

### C6.13-a (prep) - peer ports from L1 - BLOCKED on all 3 Windows laptops
commands: bash </dev/tcp/<ip>/47120 from L1 (4 s timeout)
result: SIGNAL peer-on from L2 (192.168.31.155), L3 (192.168.31.71, "firewall rule MISSING"), L4 (192.168.31.244). All three: 47120 blocked from L1.
BUG (confirmed on 3 of 3 Windows agents): with [agent.peer] bind set and the service restarted, no inbound 47120 firewall rule is created (sync_firewall).
    Asked the human whether to add a manual Private-profile rule on L2-L4 to continue part C/G; otherwise C and the peer parts stay blocked.

### PAUSE - 2026-11-04 (human break, continue tomorrow)
state: V0.2, V0.17a, V0.3, V0.4, V0.5 done (signals pushed). V0.6 in progress, stopped at C6.13-a.
next: (1) human decides on a manual 47120 Private firewall rule on L2/L3/L4 (sync_firewall bug); (2) recheck `compute network` 5 min after the peers are reachable;
    (3) C6.13-b wait v06-d-done from L2; (4) C6.13-c/d G1 Wi-Fi partition + G2 agent kill (LIVE). Parts E/F SKIPPED (folded into the V1.0 72 h soak).
    L1 peer serving on (/etc/meshforge/config.toml); the iperf3 systemd service is still running on L1 (installed in V0.5) - harmless.

### RESUME - 2026-11-05 (ANNOUNCE 2026-11-05 + (2))
result: merged origin/main. Correction to C6.13-a (prep): the missing 47120 rule on L2-L4 is NOT a bug - Windows needs the admin command
    `meshforge.agent.winservice firewall-sync` (D389); recorded as UX gap F7. L2: on hold (not present) - V0.6 continues with L1, L3, L4;
    L2 parts marked "L2 part pending". IP 192.168.31.13 unchanged; controller + agent active. Waiting for SIGNAL peer-rule-ok L3 / L4.

### C6.13-a - Part C network matrix (0.6.6) - OK with L1, L3, L4 (L2 part pending)
commands: after peer-rule-ok L3/L4 (firewall-sync), compute network (+ --json) at +5 min and +15 min
result: +5 min: L3->L1 19.1, L4->L1 18.7 MB/s measured; L1->L3, L1->L4, L3<->L4 fail:timeout (probes from before the rules).
    +15 min: all 6 L1/L3/L4 cells measured, no fail, no stale:
      L1->L4 24.5 MB/s 4.6 ms | L1->L3 7.3 MB/s 66.5 ms | L4->L1 17.0 MB/s | L4->L3 2.7 MB/s (rtt 0.0 ms ?) | L3->L1 15.0 MB/s | L3->L4 24.4 MB/s
    vs S1 Wi-Fi TCP x1 (2026-11-03): 171-248 Mbit/s = 21-31 MB/s -> same order of magnitude for 4 of 6 cells; L1->L3 (7.3) and L4->L3 (2.7) are 3-8x lower
    than S1 (L1->L3 191, L4->L3 216 Mbit/s): downloads FROM L3's peer server are slow. The 0.0 ms RTT on L4->L3 looks wrong (QUESTION).
    L2 (AILESH): on hold - its only cell is a stale 11 h L2->L1 16.7 MB/s. L2 part pending.
evidence: lab/reports/raw/2026-11-05-V0.6-sched/C-network.txt/.json, C-network-2.txt/.json

### C6.13-b - Part D (c4, submitted from L2) - PENDING
result: L2: on hold (not present). L2 part pending (ANNOUNCE 2026-11-05 (2)).

### C6.13-c - G1 Wi-Fi partition on L4 - RUNNING (attempts 1-2 did not count: 120 s/300 s jobs finished before the Wi-Fi drop)
result: earlier: auto-placed job_01M6JX0Q65WPN4C7JP6B4PF852 landed on L1 (controller host - cannot drop its Wi-Fi) -> cancelled; pinned to L4 instead
    (deviation: a pinned job retries on L4 itself, not "elsewhere"). job_01M6JX12BHHA62HJR4GEKW6922 (120 s) and job_01M6JXEA5SBHJ5QYSJK59JJES0 (300 s)
    succeeded before any drop. Now job job_01M6JY167XY1FVXB1VTAD60AT1 (main.py 300) running on L4 since 14:55:24 IST.
SIGNAL: v06-g1-go L4 job=job_01M6JY167XY1FVXB1VTAD60AT1 - L4 HUMAN NOW (Administrator PowerShell): $w=(Get-NetAdapter | Where-Object {$_.Name -like '*Wi*Fi*' -and $_.Status -eq 'Up'}).Name; Disable-NetAdapter -Name $w -Confirm:$false; Start-Sleep 60; Enable-NetAdapter -Name $w -Confirm:$false
    then record the times and EMIT SIGNAL: v06-g1-wifi-done L4.

### C6.13-c - G1 Wi-Fi partition on L4 - PARTIAL (lost + fencing OK; "attempt 2 elsewhere" not testable with a pinned job)
commands: job_01M6JY167XY1FVXB1VTAD60AT1 (main.py 300) pinned to L4 at 14:55:24 IST; Lead relayed the cue; L4 Wi-Fi off ~60 s via Disable-/Enable-NetAdapter.
result: 14:59:22 attempt 1 lost (node_lost), retry_scheduled; 14:59:27 pinned_node_gone -> dispatch_refused -> job failed (node_lost / placed_node_offline).
    L4 back online 15:00:28; at 15:00:26 two controller.jobs.status_fenced = L4's late reports for the lost attempt were refused (fencing works).
    No execution_overlap line in the journal (grep). Job did not succeed: because it was pinned (auto-placement picks L1, the controller host,
    whose Wi-Fi cannot be dropped). QUESTION (Lead, sent by message): a way to place automatically while excluding the controller host, to show "attempt 2 elsewhere".
evidence: lab/reports/raw/2026-11-05-V0.6-sched/G1-controller-journal.txt

### C6.13-d - G2 agent hard kill on L4 - RUNNING
SIGNAL: v06-g2-go L4 job=job_01M6JYC715XDXRTJ18WFBH238D (main.py 300, running since 15:01:23 IST, ends ~5 min later) - L4 HUMAN NOW, Administrator PowerShell:
    $a = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*meshforge.agent.winservice*child*" } | Select-Object -First 1; Stop-Process -Id $a.ProcessId -Force
    then BEFORE starting anything: Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object CommandLine -like '*main.py 300*'  (must print nothing = no orphan)
    then make sure the service runs again (Start-Service MeshForgeAgent if needed). Record all output, EMIT SIGNAL: v06-g2-done L4 orphan=<yes|no>.

### C6.13-d - G2 round 1 did not count; round 2
result: round 1 job_01M6JYC715XDXRTJ18WFBH238D succeeded at 15:06 before any kill (cue too late).
SIGNAL: v06-g2-go L4 job=job_01M6JYXKR18DH1QX6ANJA23K5C (main.py 300, running since 15:10:53 IST, ends ~5 min later) - L4 HUMAN NOW, Administrator PowerShell:
    $a = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*meshforge.agent.winservice*child*" } | Select-Object -First 1; Stop-Process -Id $a.ProcessId -Force
    then BEFORE starting anything: Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object CommandLine -like '*main.py 300*'  (must print nothing)
    then Start-Service MeshForgeAgent if needed. Record outputs + times, EMIT SIGNAL: v06-g2-done L4 orphan=<yes|no>.

### C6.13-d - G2 agent hard kill on L4 - OK (no orphan; retry elsewhere not testable with a pinned job)
result: job_01M6JYXKR18DH1QX6ANJA23K5C (main.py 300) on L4 since 15:10:53 IST. L4 human hard-killed the agent child at 15:13:45 (PID 11548);
    orphan check 3 s later printed nothing (no main.py 300 left) - L4 REPORT. Service came back Running by itself at 15:13:59.
    Controller: attempt 1 lost (node_lost) 15:13:52; job failed 15:13:59 (pinned to L4, same reason as G1 - auto-placement chooses L1).
    Criterion part "no orphan" = OK; "the job retries elsewhere and succeeds" = not testable with a pinned job (QUESTION already with the Lead).
evidence: L4 REPORT C6.13-d-L4; lab/reports/raw/2026-11-05-V0.6-sched/G2-controller-journal.txt

### LEAD CHECKPOINT V0.6
result: C network matrix OK for L1/L3/L4 (L2 part pending; F8 slow L3 peer server + 0.0 ms RTT); D (from L2) pending;
    E/F SKIPPED (folded into the V1.0 72 h soak); G1 lost + fencing OK, G2 no orphan OK - both pinned to L4, so "attempt 2 elsewhere" not shown.
    L2 parts pending: C (L2 cells), D.
SIGNAL: V0.6-done (L2 parts pending)

## Phase V0.7 (2026-11-05)

### C7.0 / C7.12-a - HUMAN BATCH + Section 0 - OK (L1 part)
result: HUMAN: sudo apt install tcpdump tshark -> /usr/bin/tcpdump, /usr/bin/tshark. HUMAN: sudo meshforge-controller user create alice (password kept by the human).
    compute whoami: admin (admin), token until 2027-02-01. L1 clock: date -u +%s = 1793785751, NTP synchronized. Admin user is `admin` (not labadmin).
    L2: on hold. WAIT: SIGNAL clock-unix from L3, L4.
SIGNAL: clock-unix L1 t=1793785751

### C7.12-c - Part C invite refusals on L3 - GO
SIGNAL: v07-c-go L3 - L3: first EMIT SIGNAL: clock-unix L3 t=<date -u +%s>. Then your human types 4 codes that L1's human gives by voice/screen
    (never written anywhere), each with `meshforge-agent join --force <code>` in an Administrator PowerShell:
    (1) an already-used code -> expect token_used; (2) a revoked code -> token_revoked; (3) a --ttl 1m code tried after 70 s -> token_expired;
    (4) a fresh code with one character changed in its 2nd group (CA-pin part) -> pin_mismatch.
    After each: `meshforge-agent identity show` = same old node, still online. Record the refusal texts (never the codes), EMIT SIGNAL: v07-c-done L3.

### C7.12-c - codes for L3 (human approved posting them; none of them can join anything)
SIGNAL: v07-c-codes L3 - try each with `meshforge-agent join --force <code>` (Administrator PowerShell), record the refusal text:
    (1) used code (L1's own join, used 2026-11-03):     mf1-aecmb-ka7bw-372wy-ja5rf-peilt-22g5a-ytunz-r6lqf-nn52h-f4jzt-52ftf-ada7s-wntfx-qqb5q-y   -> expect token_used
    (2) revoked code (inv_01M6GG62NQVEV7TTJ6D3BM7E1T):  mf1-aecmb-ka7bw-373aa-leqa7-ornrw-7u2jy-svzgf-ahmaf-nn52h-f4jzt-52ftf-ada7s-wntf6-xqk7g-i   -> expect token_revoked
    (3) expired 1-minute code (inv_01M6JZK6EH..., expired 09:53:40Z, posted after expiry): mf1-aecmb-ka7bw-372kj-nseit-2i74a-gagdn-rzenn-7igaf-nn52h-f4jzt-52ftf-ada7s-wntfd-ajpej-y -> expect token_expired
    (4) wrong-pin code (real invite inv_01M6JZK6NV... with 2nd group ka7bw changed to ba7bw by L1; the posted text is never valid):
        mf1-aecmb-ba7bw-373we-ohoqp-ggp6u-b67qd-urypx-oafaf-nn52h-f4jzt-52ftf-ada7s-wntf6-usujd-q -> expect pin_mismatch (L1 revokes the real invite after)
    After each: identity show = same node, still online. EMIT SIGNAL: v07-c-done L3.

### NOTE - rule check (re-read RULES.md, be06f5e8)
QUESTIONS: RULES say pairing codes are never written down. The C7.12-c entry above posts 4 codes (human approved, test-only); all are unusable
    (used, revoked, expired before posting, and one altered by L1 so it never existed). Lead: tell me if they must be removed from the report.

### C7.12-c - codes re-posted as a proper -go cue (L3's auto-watch looks for "-go L3")
result: previous pin invite inv_01M6JZK6NV... revoked; new real pin invite inv_01M6K0281N0AXM84MPP97RH384 (2 h), posted ONLY in altered form.
SIGNAL: v07-c-go2 L3 - HUMAN on L3, Administrator PowerShell, one at a time: `meshforge-agent join --force <code>`; after each `meshforge-agent identity show` = same node, online.
    (1) expect token_used:    mf1-aecmb-ka7bw-372wy-ja5rf-peilt-22g5a-ytunz-r6lqf-nn52h-f4jzt-52ftf-ada7s-wntfx-qqb5q-y
    (2) expect token_revoked: mf1-aecmb-ka7bw-373aa-leqa7-ornrw-7u2jy-svzgf-ahmaf-nn52h-f4jzt-52ftf-ada7s-wntf6-xqk7g-i
    (3) expect token_expired: mf1-aecmb-ka7bw-372kj-nseit-2i74a-gagdn-rzenn-7igaf-nn52h-f4jzt-52ftf-ada7s-wntfd-ajpej-y
    (4) expect pin_mismatch:  mf1-aecmb-ba7bw-373ey-ikt6x-ri5or-yxpsv-2pbya-xtrqf-nn52h-f4jzt-52ftf-ada7s-wntfh-57log-a
    Record each refusal text, EMIT SIGNAL: v07-c-done L3.

PAUSED 2026-11-04 15:31 (human: pause) - at C7.12-c, waiting for L3 v07-c-done; pin invite in REPORT (2 h) to revoke after.

STARTED 2026-11-04 15:35 (human: start, loop 2m)

### C7.12-c - Part C invite refusals - PARTIAL (3 of 4; pin_mismatch path not reached)
result: L3 (v07-c-done): (1) token_used, (2) token_revoked, (3) token_expired - all refused with the right reason, identity unchanged.
    (4) one character changed in the 2nd group -> local "this is not a valid code (a character is wrong)" = bad_checksum (codes carry a SHA-256
    checksum, join_code.py), so the CA-pin check was never reached. Refusal correct and safe; only the tested path differs.
    Tried: a valid-checksum code with a wrong ca_pin re-encoded from a real invite - NOT done: it would carry a live token in the repo
    (blocked as credential leakage, rightly). The altered-pin invite inv_01M6K0281N... was revoked. No pending invites left from this step.
QUESTION (Lead): how should pin_mismatch be tested? Options: (a) a unit/cluster test that builds a code with a foreign CA pin (no live token);
    (b) L1's human makes such a code locally and gives it to L3 by voice, never in git.

### C7.12-d - Part R revoke the busy L3 (c1) - revoke OK, rejoin pending
commands: sleep-300 job pinned to L3 (as admin - alice not logged in on L1; deviation); `date +%s.%N; compute node revoke <L3-ID>`
result: job_01M6K0M22DG59HZ3M03QEQNQYN running on L3 since 15:40:38. Revoke at 1793787054.468 (15:40:54.47 IST), CLI "revoked ... (session cut)".
    Attempt att_01M6K0M234... lost at 15:40:54.710 = 0.24 s after the revoke (< 2 s). failure_type node_lost, failure_detail EMPTY (expected detail
    node_revoked - QUESTION/BUG candidate: the reason is not carried). Job pinned -> queued, will not run elsewhere (same pin caveat as V0.6).
    L3's agent-log time of node_revoked: from L3's REPORT.
SIGNAL: v07-r-go L3 - L3 was REVOKED. L3: record the local time of the node_revoked line in the agent log. Then HUMAN (Administrator PowerShell):
    (a) `meshforge-agent join --force <code 1>` -> expect refused node_revoked;
    (b) `meshforge-agent identity reset --yes`; then `meshforge-agent join <code 2>` (or the console) -> identity show = a NEW node_id;
    (c) re-apply owner settings (Run jobs on; then Restart-Service MeshForgeAgent for bug F5). Codes come from L1's human by voice (not in git).
    EMIT SIGNAL: rejoined L3 node=<new id>.

PAUSED 2026-11-04 15:53 (human: pause) - at C7.12-d, waiting for L3 rejoin (SIGNAL rejoined L3). Two 2 h invites made for L3 (code 1 unused - revoke after).

### C7.12-d - Part R - L3 rejoined as a NEW node - OK (step a SKIPPED)
result: L3 (Mohit) rejoined with code 2 (invite inv_01M6K0N60Q..., used) -> NEW node_id node_01M6K0TZRNPEGBRN166GP6S61N, online; old node_01M6GGZ7... stays revoked.
    Step (a) "join --force -> refused node_revoked": SKIPPED (operator typed the placeholder, identity already reset; L3 REPORT).
    Unused code 1 invite inv_01M6K0N5S0FP... revoked by L1. L3 revoke timing: L3 agent log node_revoked 15:40:53.72 IST vs L1 revoke stamp 15:40:54.47
    (L3 0.75 s earlier = clock offset between the laptops); controller marked the attempt lost 0.24 s after L1's stamp -> < 2 s. Criterion c1 OK except
    failure_detail empty (QUESTION above) and "retried elsewhere" not shown (pinned job).
    <L3-ID> is now node_01M6K0TZRNPEGBRN166GP6S61N. Chat: read 20261105-160000-LEAD-to-ALL (auto-watch loops stopped; work on `continue`).

### C7.12-e - Part X captures (c3), L1 side - OK (L2 Wireshark part pending; L4 used instead of L2 for the job)
commands: lab/reports/raw/2026-11-05-V0.7-security/run_x.sh (one sudo): markers MK_ARGV/MK_FILE + secret LAB_KEY; tcpdump on wlp0s20f3
    (47100/47101/47110) -> l1.pcap; tcpdump -i any 47101 -> enroll.pcap around a real enrolment (dev CLI, user mode, `join --config` with a
    throwaway data_dir; new node node_01M6K66M7877... enrolled: True); job with markers on L4 (-f, logs, get --dest) and on L3 (new id), sleeper on L3 + cancel.
result: l1.pcap: all 9 marker counts 0 (argv, file, secret, secret b64, secret hex, join code, "Bearer ", peer-token prefix, API token);
    http/http2 frames 0; ServerHello 9 x 47110 0x0304 (TLS 1.3). No new 47100 handshakes in the window (agent sessions were already up - long-lived).
    enroll.pcap: code 0, token_raw 0, token_hex 0, CSR armour 0, cert armour 0; 2 connections on 47101; 0 non-TLS payload packets; 2 x ServerHello 0x0304.
    Jobs: L4 job_01M6K66QSNYZA0ME32WKDSX3T9 and L3 job_01M6K66SCPZ25ANGCBTFEJVC72 printed both markers; the secret is not in the env
    (printed "<file>": delivered as a file by default, as_env not set) - part S checks the as_env case. Sleeper job_01M6K66TZZ... cancelled.
    Cleanup: throwaway node revoked, LAB_KEY deleted, enrol data dir, secret value and code removed (never in git). Pcaps committed (all counts 0).
    Pending: L2's peer capture (47120) and the Wireshark protocol-hierarchy screenshot (L2 on hold); step 4 SNI check needs l2.pcap.
evidence: lab/reports/raw/2026-11-05-V0.7-security/{l1.pcap,enroll.pcap,analysis.txt,run_x.sh}

### C7.12-f - Part L CPU share on L1's installed agent - OK
commands: journal: agent.execution.cpu_cgroup_ready (2026-11-03); systemd 255. HUMAN (console): "Most CPU one job may use" = 25 %,
    "Allow jobs without a container" on, Save; agent restarted 17:23:06 (F5). Job cpu-burn (8 spinning processes, 60 s) pinned to L1.
result: job_01M6KB005W95Y51KYPNRHRBF6V ran in a Docker scope (cuda/python image); sum %CPU of the 8 main.py processes = 400-404 % of 1600 % (16 threads)
    in 5 samples = 25 %; machine total ~25-30 % us. The cap is enforced. (Ran in a container, so this shows the container CPU quota;
    the no-container path was allowed but not chosen by the agent.)
    Settings restore: HUMAN pending (CPU empty, "Allow jobs without a container" off).

### C7.12-g - Part E wheels-only - steps 1 + 3 OK on L1 (Docker) and L4 (venv); step 2 pending (config edit)
commands: job with requirements.txt `docopt==0.6.2` (sdist only) and a job whose requirements.txt starts with `--no-binary :all:`; each pinned to L1 then L4.
result: L1 sdist job_01M6KB2BDTXAM9XDC7780KHB1J: env_error setup_failed "installs are wheels-only ([agent.envs] only_binary = true) ... needs only_binary = false" - OK.
    L1 --no-binary job_01M6KB2DKJ285XWYN18GZCNMCA: env_error requirements_rejected "requirements line 1: option lines are not accepted ('--no-binary :all:')" - OK.
    L4 sdist job_01M6KB2Q3S20TF2ETKJ72MGQXG: env_error setup_failed (uv: "building from source is disabled") - OK; L4 --no-binary job_01M6KB2TNTZ7WTACV1K2VJNPE9: requirements_rejected line 1 - OK.
    L4 replaces L2 as the venv laptop (L2 on hold). Step 2 (only_binary = false -> succeeded): needs `[agent.envs] only_binary = false` in /etc/meshforge/config.toml + restart (HUMAN).
evidence: lab/reports/raw/2026-11-05-V0.7-security/E.txt

### C7.12-i - G4 extras (part) - OK
result: curl -k https://192.168.31.13:47110/v1/nodes without a token -> 401; /healthz -> 200 {"status":"ok","sessions":3}. Owner rule (alice vs admin jobs): needs an alice login (HUMAN). G4 0.1 dev DB password: NOT RUN (dev-only).

### C7.12-g - Part E step 2 (only_binary = false -> sdist job succeeds) - BLOCKED by the planner (finding)
commands: tools step2.sh: append `[agent.envs] only_binary = false` to /etc/meshforge/config.toml, restart agent (execution.ready), rerun the docopt job on L1; restore config + restart.
result: job_01M6KBBEAZRCM94M2EX68EVADW stayed `queued`: compute explain -> L1 infeasible, reason env_known_bad "this job's environment failed to build on
    the node recently" (decision 48). The planner's memory of the failed build (from step 1, ~10 min earlier) keeps it off L1 even after the owner
    changed only_binary; no other node is eligible (pinned). Cancelled; config restored (only [agent.peer]), agent active.
BUG/UX candidate (Lead): env_known_bad is not cleared when the agent's env policy changes (agent restart / new Hello). Workaround for the test: a
    requirements file with a different hash, or wait for the known-bad window to expire.

### C7.12-f (restore) - OK
result: HUMAN cleared "Most CPU one job may use", unticked "Allow jobs without a container", Save; agent restarted 18:55:40 IST.

### C7.12-i - G4 owner rule (alice vs admin) - OK
commands: alice logged in with a separate credentials dir (MESHFORGE_CLI__CREDENTIALS_DIR=~/mf-alice; HUMAN typed the password); admin job (sleeper, L4) then alice's own job.
result: alice `compute jobs` = 0 jobs, admin job not listed; alice jobs/cancel/logs <admin job> -> 404 "no such job" (no existence leak); admin job stayed running.
    Admin cancelled alice's job_01M6KBT8K1... -> cancelled (admin may). Admin job job_01M6KBT23Z... cancelled afterwards. compute whoami: alice (user) / admin (admin).
evidence: lab/reports/raw/2026-11-05-V0.7-security/owner.txt

### LEAD CHECKPOINT V0.7
result: OK: a (tools, alice, clocks L1/L3/L4), c 3 of 4 (pin_mismatch path not reached - QUESTION), d revoke 0.24 s + L3 back as a new node,
    e captures all marker counts 0 / TLS 1.3 / clean enrolment, f CPU 25 % enforced, g wheels-only refusals on L1 + L4, i curl 401/200 + owner rule.
    FINDINGS: failure_detail empty on revoke (expected node_revoked); env_known_bad blocks a rebuild after only_binary changes (step 2); test codes posted (QUESTION).
    PENDING (L2 on hold): b icacls, e L2 peer capture + Wireshark screenshot, h part S (secrets on L2, as_env), g venv part done on L4 instead.
SIGNAL: V0.7-done (L2 parts pending)

## Phase V0.8 (2026-11-05)

### C8.10-a - A3/A4 CA trust on L1 Chrome - OK
result: CA exported to ~/meshforge-fabric-ca.crt (from the TLS chain; sha256 05:6B:7B:A3...). HUMAN imported it in chrome://certificate-manager
    (Installed by you > Trusted certificates: "MeshForge fabric CA" 056b7ba3...). Still "Not secure" until Chrome was fully restarted (old click-through
    exception), then the dashboard loads with no warning. Server cert SAN = DNS:meshforge-controller, IP:192.168.31.13; curl --cacert verify 0.
    Dashboard: 3 of 4 nodes online (L2 offline - on hold), 2 revoked (old L3 + L1's throwaway enrol node).
    NOTE (Lead): Overview Alerts shows repeated `clock_uncertain` {"reasons":["inconsistent"], bound_ms 60-81, offset_ms -11..+10, consistent:false} every ~20 min.

### C8.10-c - Part C browser flows on L1 - OK in CHROME (deviation: checklist says Firefox; human uses Chrome) - Firefox run pending if the Lead wants it
result: C1 no certificate warning after the CA import + Chrome restart; sign-in page shows - OK.
    C2 keyboard-only sign-in as alice; focus ring clearly visible (screenshot) - OK. (First tries: "Wrong user name or password" message shown correctly.)
    C3 Submit job (folder ~/mf-lab/v08-small) -> job_01M6KCM17FM1FXEA2EVBAM7RAX succeeded on L3 (planner), live log line 0-4, exit 0 - OK.
    C4 Outputs download (job_...-attempt1-outputs.tar.zst, 113 B), Plan tab decision table (L3 rank 1, L2 not_online, 2 revoked excluded), Attempts (1) - OK.
    C5 v08-long job_01M6KCRTRX5KV82G2SKETZ7V8G live log, cancelled via Cancel job + dialog at line 548 - OK (keyboard, per the human).
    C6 console: after clearing, submitting job_01M6KD060Y7N59DTNDPA5S3N81 added no new error. Earlier 4 x 401 on /session = from the failed sign-ins.
       One "Uncaught TypeError: Cannot read properties of undefined (reading 'startTime') at et.reportAllChanges (<anonymous>)" in VM47 - not from the
       app's index-*.js; likely a browser extension (web-vitals style). QUESTION: Lead to confirm.
    C7 keyboard sign-out -> signed out - OK.
    UI notes: (1) the red "Cancel job" button stays visible on finished jobs (succeeded/cancelled); (2) after picking a folder the page still says
       "No file chosen" next to "Choose files" although the project summary loaded; (3) Plan: transfer predicted 0 ms "assumed", actual 1.8 s (uncalibrated).
evidence: human screenshots in this session (not committed)

### C8.10-e - Part E3 ui_perf on real hardware (c2) - OK (VERDICT: pass, exit 0)
commands: uv run playwright install chromium; MF_PASSWORD/MF_TOKEN set by the HUMAN (admin; token "TEST" from Settings > API tokens - to be revoked);
    uv run python scripts/ui_perf --controller https://192.168.31.13:47110 --ca ~/meshforge-fabric-ca.crt --user admin --history 100 --bulk 10000 --out .../ui-perf.json
result: seeds: history 300 succeeded jobs (100 x 3 laptops - L2 offline/on hold) in 147.9 s; mixed 20; bulk 10 000 cancelled in 420.8 s.
    p50/p95/max ms (history | bulk): jobs list cold 52.3/109.9/118.7 | 52.3/67.4/70.9; in-app nav 25.6/31.8/33.1 | 29.4/30.9/31.1;
    next 50 rows 23.9/31.6/34.2 | 24.2/37.1/40.9; filter 10.1/21.5/23.1 | 9.2/26.8/27.0; job page 20.6/31.0/33.3 | 34.5/35.4/35.5;
    heartbeat->DOM 167.7/234.1/239.7; job.state->DOM 8.6/41.0/54.1; scroll: 0 long tasks, <= 36 rows in the DOM (371 / 1000 loaded).
    clock (history): offset 0.69 ms, bound 1.5 ms, 0 pings dropped, consistent before/after, reasons [], uncertain false. VERDICT: pass.
    The lab DB now holds 10 400+ seeded jobs (planned).
evidence: lab/reports/raw/2026-11-05-V0.8-visual/ui-perf.json

### C8.10-g - Part H controller on another port (L1 Chrome; L2 on hold) - OK
commands: h_move.sh: /etc/meshforge/controller.toml http_bind 47110 -> 47121, restart; h_back.sh: restore + restart.
    Deviation: port 47121 instead of 47120 - 47120 is L1's own agent peer server ([agent.peer] bind), the controller could not bind there.
result: H3 old 47110 tab: header "Nodes unavailable / Reconnecting...", body "Could not load jobs - The controller did not answer ... Retry" - no blank page, no crash - OK.
    Controller on 47121: healthz 200, 3 agent sessions (gRPC 47100 unchanged).
    H4 new tab https://192.168.31.13:47121/jobs: signed in as admin, Live, same jobs listed - OK (the cookie/session carried over or the human signed in; screenshot shows signed in).
    H5 after restoring 47110: the old tab went back to Live with the job list by itself, no reload - OK. Config back to http_bind 47110.
    NOTE (Lead): seeded bulk-09986 shows "succeeded" (updated 19:35:06) while all other bulk seeds are "cancelled" - unexpected state in ui_perf seed data.

### C8.10-d - Part E1/E2 live node view (L4 instead of L3/L2) - GO
SIGNAL: v08-e-go L4 - L1's human watches Nodes in the dashboard. L4 HUMAN, when L1's human says "now":
    E1: Administrator PowerShell: $w=(Get-NetAdapter | Where-Object {$_.Name -like '*Wi*Fi*' -and $_.Status -eq 'Up'}).Name; Disable-NetAdapter -Name $w -Confirm:$false; Start-Sleep 40; Enable-NetAdapter -Name $w -Confirm:$false
    E2: after Wi-Fi is back: start something heavy for ~60 s (e.g. a 4K YouTube video, or PowerShell: 1..8 | % { Start-Job { $t=(Get-Date).AddSeconds(60); while((Get-Date) -lt $t){} } }).
    Record times, EMIT SIGNAL: v08-e-done L4.

### C8.10-d - Part E1/E2 with L4 (L2 on hold) - E1 OK (controller side); E2 needs the human's view
result: chat L4->L1 20261105-003740: L4 Wi-Fi off 00:31:58, back 00:32:38; CPU load 8 jobs 00:35:50-00:36:52.
    Controller journal: L4 state_changed 00:31:59 (suspect), lease_expired -> offline + session.closed 00:32:03 = ~5 s after Wi-Fi off;
    session.opened + state_changed (online) 00:32:45 = ~7 s after Wi-Fi back. The dashboard is push-updated from these events (ui_perf: heartbeat->DOM p95 234 ms).
    L1's live poll started 00:33:16 (after the drop) - missed it; the human's own view of the Nodes page (seconds, no reload) not recorded.
    E2 (CPU sparkline during the load): pending the human's yes/no.
    E2 (2026-11-05 00:42-00:43): L4 node page Live tab - CPU value changes live (human saw 0/28/32/42/50/70 %), sparkline moves,
    "Latest sample" advances every ~2 s (00:42:03 -> 00:43:07), 108-111 samples over 300 s, no reload - OK. (Whether the 60 s load from the chat
    re-run was running at that moment: see L4 REPORT v08-e2-done.)
    Header now "4 of 6 nodes online" - L2 back.
    E2 correction after chat L4->L1 20261105-004508: L4's load ran 00:41:18-00:42:20. The human's screenshot at 00:42:03 (inside the load) showed
    CPU 0 % with no clear 60 s plateau in the sparkline; values 28-70 % were seen around 00:43. So "live updates" = OK, but "the sparkline rises
    with the load" is INCONCLUSIVE (either the PowerShell Start-Job loops did not load the CPU much, or the samples lag). Optional redo with a
    stronger load (e.g. a 4K video or a compile) while watching.

### C8.10-d - E2 redo with a fabric job - FAIL: the Live CPU value does not follow a real load on L4 (BUG candidate)
commands: job_01M6M0B8E2BDSXSKF90283HMZV pinned to L4: 12 spinning Python processes for 90 s (L4 = 12 threads); ran 00:55:06-00:56:36 IST, "burn done", succeeded.
result: human watched L4's node page Live tab during the run (4 screenshots 00:55:54, 00:56:02, 00:56:22, 00:56:51): CPU 32 %, 0 %, 27 %, 32 %;
    no plateau in the sparkline, values look the same as when idle. Latest sample advanced every ~2-8 s; 85-90 samples / 300 s.
    Expected ~100 % (or the owner cap, if L4 has one; "Most CPU one job may use" on L4 not checked - L2's console showed 50 %).
    A 0 % sample in the middle of a 12-process burn means the Windows CPU sample is wrong or not the machine total.
BUG candidate (Lead): Windows agent cpu_percent sampling (first-call/interval issue or per-process instead of system-wide?). Reproduce: run
    a 12-process 90 s burn on a Windows node and watch Nodes > node > Live. Compare: Linux (L1) part L showed the real 400 % of 1600 % in top.

### C6.13-a - Part C network matrix, L2 cells (L2 part pending -> done) - OK with 2 slow links
result: after L2 peer-rule-ok (chat 20261105-010305), compute network: all L1/L2/L3(new 6GP6S61N)/L4 cells measured except L3new<->old rows:
    L1->L2 26.2 MB/s 5.1 ms | L2->L1 18.5 | L2->L3 16.3 (rtt 0.0 ms again) | L3->L2 7.5 | L1->L3 9.3 | L3->L1 27.1 | L1->L4 16.4 | L4->L1 27.0
    | L3->L4 5.8 | L4->L3 18.5 | L2->L4 1.8 MB/s (93 ms) | L4->L2 1.9 MB/s - the L2<->L4 pair is ~10x slower than S1 (L2-L4 TCP x1 ~248 Mbit/s) - add to F8.
    Old revoked L3 (H3AX7QQY) cells are stale (10 h). Chats read: L2-to-L1 010305 + 013222, LEAD-to-L1 170000 (triage: fixes in v0.17.1, re-run list noted).
evidence: lab/reports/raw/2026-11-05-V0.6-sched/C-network-3-with-L2.txt/.json

### C7.12-e - Part X L2 capture (L2 part pending -> done) - OK
commands: L2 Wireshark/dumpcap 02:02:12 (tcp 47120/47100/47110); L1: marker project (argv + data.txt + secret LAB_KEY as_env) pinned to L2
    (job_01M6M48WFYMZAP7F5E4ENVYZZP) then L4 (job_01M6M48YF73KNGSN73BE4A8RWF; L4 fetched the project from L2's peer server, 18 frames on 47120);
    L1 greps on origin/lab/L2 l2.pcapng (1660 packets).
result: all 8 counts 0 (argv, file, secret value, secret b64, secret hex, "Bearer ", peer-token prefix, API token); http/http2 frames 0;
    ServerHello 4 x 47110 0x0304 + 1 x 47120 0x0304 (TLS 1.3); 0 non-TLS payload on 47120; SNI node-01m6gckn7pyt2j297bt241p436.node.meshforge
    visible by design (PB-I2). Job logs printed "secret [secret:LAB_KEY]" (redacted in logs). LAB_KEY deleted after (note: it "replaced" an existing
    LAB_KEY - L2's part S value; told L2).
    L2 also reports: v08-d-done (Edge D1-D5 PASS, as admin, 4 UI findings in L2 REPORT); V0.8 G FAIL - PowerShell completion "Shell cmd is not supported" (new bug for the Lead).
evidence: lab/reports/raw/2026-11-05-V0.7-security/l2-analysis.txt; origin/lab/L2 .../l2.pcapng

## Phase V0.9 (2026-11-05)

### C9.10-d - C3 gpu_load on L1 - BLOCKED: L1 does not advertise gpu_load
result: compute bench plan (gpu_load:50 on L1): "the node does not offer gpu_load (bench-conditions capability)". L1 caps = bench-conditions:cache,cpu_load
    (no gpu_load) although L1 has the RTX 3050 + CUDA + Docker - likely the installed agent's own Python has no torch (GPU work runs in Docker). QUESTION (Lead).
### C9.10-c - C2 cpu_load on L2 - BUG: planner refuses cpu_load although every node advertises it
result: cpuload.yaml (cpu_load:2 on L2, nodes/origin L2): "invalid campaign: node ...BT241P436 x condition cpu_load:2: the node does not offer cpu_load".
    `compute nodes <id> --json` hardware.capabilities on ALL 4 nodes = ['bench-conditions:cache,cpu_load']. Checkout code (advertised_kinds /
    unsupported_reason, bench_routes._nodes) would accept it - so the installed v0.17.0 controller reads something else (stale snapshot? different field?).
    BUG candidate (Lead). With on_unsupported: skip -> see plan output below. C1 netem: NOT RUN (no Linux agent other than L1).
### C9.10-e - R3 Linux idle source (part 1) - while in use
result: loginctl: Type=x11, IdleHint=no, IdleSinceHintMonotonic=0. Part 2 (after 6 min idle) + agent owner_activity line: pending.
    C2 run: with `on_unsupported: skip` the same campaign plans 6 runs (2 configs x 3) INCLUDING cpu_load:2 -> camp_01M6M4S854YBNF90K28MASXSDZ,
    `bench submit -n 6 --wait`: "submitted 6 ... ended: 6 done, 0 failed", exit 0, all < 1 min. So the default-mode refusal is a validation bug,
    not a missing capability. L2 burner-process / PriorityClass check: pending from L2.
evidence: lab/reports/raw/2026-11-05-V0.9-campaign/{cpuload.yaml,c2-submit.txt,c2-runs.txt}

### V0.9 - PARTIAL, rest deferred to an evening (human decision 2026-11-05 morning)
deferred: C9.10-g/h/i overnight lab-v1-data campaign (needs all laptops idle for 5-7 h), R3 part 2 (L1 idle 6 min), R1/R2/B1/P1 (L2 TODO),
    C2 burner check (sent to L2). Not blocking V0.10 (C9.10-h blocks only V0.14/V0.15).

## Phase V0.10 (2026-11-05)

### C10.10-a - Part 0 - OK (0.1 = installed v0.17.0 on all 4, not a git pull)
result: compute nodes: L1 JARVISM3, L2 AILESH, L3 Mohit (new id 6GP6S61N), L4 LAPTOP-7ORVM30G all online, accept_jobs True, pause_benchmarks False
    (read from `compute nodes <id>` policy fields). SESSION_START 2026-11-05T07:06:27Z. Wi-Fi, same router.
    0.3: mc = split.montecarlo_range items=5000 samples=5e6 seed 10 (whole job ~530.6 s ref laptop); hf = split.hash_files 60 x 1 MiB seed 11 (~0.8 s).

### C10.10-b - Part A C1 identical output (4 laptops) - OK (C1 PASS)
commands: c1.yaml (split.image_classify images=3000, strategies local/mode1/mode3, nodes L1 L2 L3 L4, origin L1, 3 reps) -> camp_01M6N8FTSB95KJ9E2WF7AD4VGT
result: done (complete), 15 / 15 runs done, 0 failed, infra failures 0 of 39 attempts.
    ONE output digest across all 15 jobs: sha256:6aafd7d7f78bd694ec0053cea3af6ac00a567394850d07a10540fb15d198382c (local L1, mode1 L2/L3/L4, mode3 4-node).
    verify: mode3 job_01M6N8XYAZ... and mode1 job_01M6N8G0EK... both "OK - every item once, in order, each value recomputed", same output digest f968f38c...
    watch --json (mode3): chunks 17/17; lanes L1 13, L3 2, L4 1, L2 1 (all 4 laptops took work; very uneven - L1 is origin + fastest).
    explain: first decision "not split: the forced node set: 3 of 4 node(s) can take a lane now" (split_too...), later chosen split on L1 lane set.
evidence: lab/reports/raw/2026-11-05-V0.10-mode3/{c1.yaml,c1-runs.json,c1-digests.txt,c1-verify.txt}

### C10.10-c - Part B single-laptop runs - L1 OK; L2 BUG: a long single chunk loses its lease forever
result: B1 L1: job_01M6N9DFQPCVT9S8QH5961BA37 `compute run mc --node L1 -f` succeeded in 427 s (one chunk, items 0..4999).
    B1 L2: job_01M6N9TH2GQBDTGNTK7V90BKFM - one lane, one chunk (5000 items); leased 07:29:56Z, re-leased 07:45:00Z (lease_seq 2, failed_tries 1),
    again at ~08:00Z (failed_tries 2), chunks_done 0 after 31 min -> cancelled by L1 at 13:31 IST. The chunk on L2 (slower i5-12450H) takes longer than the
    lease (~15 min = [controller.split] visibility_timeout_s?) and is restarted from zero each time = never finishes.
BUG (Lead, serious for Mode 3 on slow laptops): a chunk that runs longer than visibility_timeout_s is lost and re-leased with no progress; no lease
    renewal / heartbeat extension while the chunk runs, or the auto chunk size ignores the node's speed. Single-node "don't split" runs of a long job
    are one chunk, so they hit it directly. L3/L4 single runs NOT RUN (same risk, slower CPUs). C3 (speedup vs single) needs singles -> INCONCLUSIVE.
    Note: the L1 single run log said "the attempt ended (done); the job was retried - following attempt 2" (lane + combine, reads like a retry - UI wording).

### C10.10-c - Part B split run (c2, c3) - split OK + correct; c2/c3 INCONCLUSIVE (no singles for L2-L4, lease bug)
commands: compute run mc -f (planner's own choice) -> job_01M6NBKN8VF27R18Z6WX1MJGZ9; verify; telemetry export since SESSION_START; mode3_check report --single <L1 single>
result: planner chose split over all 4 laptops (17 chunks x 312 items), exit 0, makespan 270.3 s (recorded bound 250.2 s). Lanes: L1 11 chunks 11.6 items/s,
    L3 3 / 3.6, L2 2 / 3.1, L4 1 / 1.7 items/s (L4 slowest - same CPU model as L2, half the rate: noisy/loaded?). explain predicted 40.49 s (uncalibrated, x6.7 off).
    verify: OK - every item once, in order, each value recomputed (digest a76d97a1...).
    report: best_single 426.9 s (L1) -> speedup 1.579; c2 INCONCLUSIVE, c3 INCONCLUSIVE ("no single-node run measured for L4, L2, L3").
    Per-lane rates from the split itself: sum 20.0 items/s vs L1 11.7 -> rate_ratio ~1.71; observed speedup 1.58 = ~92 % of that bound.
evidence: lab/reports/raw/2026-11-05-V0.10-mode3/{b-split.log,b-report.json,b-report.txt}

### GPU probe - Lead GO relayed (2026-11-05)
result: Lead GO (chat 20261105-135430-LEAD-to-L1) for owner [agent.envs] index_url = cu126 on L2/L4 (remove after v0.17.1). Relayed to L2 + L4 via chat. Waiting "envs set".

### C10.10-e - Part D C5 "don't split" (hf) - FAIL: tiny job was split (finding)
commands: (in ~/mf-eval/v010) compute network; compute run hf -f --no-progress -> job_01M6PER6F4NFMBJAWJK2A6A4ZJ; compute explain
result: job ran OK (chunks compute_ms ~130, check=ok) but the planner SPLIT it over 3 nodes (12 x 5 items), predicted 43.28 s <= 0.9 x 65.72 s.
    Cause: local/offload rates are "(assumed)" (1/s for L1, 0.57-0.78/s others) -> 60 items predicted ~66 s, real whole job ~0.8 s (x80 off).
    The split test is right given its inputs; the item-rate guess for split.hash_files is not calibrated, so "don't split" never triggers.
    Links measured (Wi-Fi 7-25 MB/s, some H3AX7QQY rows stale 32 h = old revoked L3).
FINDING (Lead): uncalibrated per-item rate makes tiny jobs split; use a measured/benchmark rate (or the job's hint ~0.8 s) before the margin test.
evidence: lab/reports/raw/2026-11-05-V0.10-mode3/{d-network.txt,d-run.log,d-explain.txt}

### 2026-11-10 - resume after v0.17.1
read: LEAD-to-ALL 20261106-120000 (re-run list) + 20261110-165039 (resume). Downloaded v0.17.1 .debs (sha256 OK). Cued ALL to upgrade + drop workarounds.
check: L1 controller+agent 0.17.1-1. compute nodes: JARVISM3, AILESH (L2), LAPTOP-7ORVM30G (L4), Mohit (L3 new node_01M6K0TZ...) all online 0.17.1.
    L2 GPU column still "-" (L4 shows 0:0%). No chat replies yet (configs/nvidia-smi pending).

### G9-B a/b (L4) on v0.17.1 - PASS
a: compute nodes L4: exec-venv, exec-gpu, cuda, env-policy:e73b630eff86, no exec-torch; GPU RTX 3050 Laptop 4 GiB. PASS
b: run 1 job_01M72PSP48GKN46STG1AHWGEMS failed user_error "Numpy is not available" - MY job folder lacked numpy in requirements (not a fabric bug; torch cu126 wheel does not pull numpy). Install 10 pkgs 2m33s.
   run 2 (+numpy) job_01M72QFCTBN7XM7Z2QGSX12NMB: "done on cuda: final loss 0.018201" (same as L1), env install 11 pkgs 2m28s. PASS
evidence: lab/reports/raw/2026-11-10-G9/b-L4*.log

### G9-B c - Mode 3 GPU split (L1/L2/L4, L3 no GPU) - PARTIAL + 2 BUGs
job folder ~/mf-eval/gpu-split (range 0..24, chunk 2 items, gpu required, python-cpu + torch==2.14.0 + numpy, CUDA MLP per item, merge-json).
run 1 job_01M72QTRYT43884YFENTMYY7N9 (no item_seconds): planner chose offload L1 - "not split: ... only on assumed per-item" (G9-E working). L3 row "no_gpu" PASS.
  BUG-1 (L1, serious): L1 builds the env in Docker (python-cpu base) and EVERY chunk fails "OSError [Errno 28] No space left on device" right after
  downloading torch 554.6 MB, although / has 21 GB free (Docker root /var/lib/docker, 16.8 GB images). Small build tmp/limit?
  BUG-2: the lane re-builds the env per chunk: 8 chunks = 8 builds = ~4.4 GB downloaded, no typed env_error; job ran on until I cancelled it.
run 2 job_01M72RF2RXXF5284Z7K9R0CRB5 (item_seconds 6, --exclude-node JARVISM3): "split over 2 nodes: 160.24s <= 0.9 x 220.74s" (L2+L4), succeeded,
  combine 12 parts on L4; results.json 24 items, all "NVIDIA GeForce RTX 3050 Laptop GPU" (L4 did every chunk while L2 was still building its new env).
  BUG-3 (minor): after the job succeeded, L2's lane att_01M72RF2T052FGBMX4989EPFC2 still shows "running exit=None" (not ended/cancelled).
verdict: GPU lanes only on GPU nodes PASS (L3 no_gpu); L1 GPU lane FAIL (BUG-1/2); L2 lane did no chunk (env build time) - re-run once L2 env is warm.
evidence: lab/reports/raw/2026-11-10-G9/c-*.{log,txt}

### G9-D - long single chunk (mc, item_seconds 0.106) on L2/L3/L4 - L2 PASS, L3 node_lost (re-run), L4 running
note: `--node <hostname>` refused ("NODE_ID is not a canonical node_<ULID>") - known follow-up; used node ids.
L2 job_01M72RXB5XM79SCDC1SZG4SWA5: succeeded, 1 chunk (0..4999), ~30 min, resumed_chunks=0, "following the combine step". Lease bug FIXED on L2. PASS
L3 job_01M72TMDAVRB8SEQ50E753TJX9: failed node_lost placed_node_offline - L3 went offline mid-run (asked human: sleep?). Re-run after L4.

### G9-E - hf twice - PASS
run1 job_01M72WJC4R4EBGR5Y7JE76JYMC: not split (37.47s > 0.9 x 26.13s), rate "(declared)", offload L1, OK.
run2 job_01M72WJE9XR7V49TN4YH40CTNQ: not split (24.68s > 0.9 x 22.44s). PASS (C5 now OK).
evidence: lab/reports/raw/2026-11-10-G9/{d-*,e-hf*}
G9-D L4 job_01M72VM0B919WJKV4XTF10BHDK: succeeded, 1 chunk, ~38 min (19:21-19:59), resumed_chunks=0, combine OK. PASS
G9-D L3 re-run job_01M72WM9FA382SMEGZ8Z14KRJZ (sleep=never, plugged in): failed node_lost again at 19:53 (~15 min in). Controller: session.closed
    end_reason lease_expired, last_rtt_ms 1212, clock_offset -564 ms, commands_unacked 1; L3 RAM 6.9/7.7 GiB. FINDING: L3 (low RAM, weak Wi-Fi?) loses its
    agent session under a 1-core CPU job; pinned job then fails (no other node). Asked L3 human for RAM/Wi-Fi + agent.log tail.

### G9-E V0.10 part B again (c2, c3) - split OK, speedup 1.371; c2/c3 still INCONCLUSIVE (no L3 single)
singles: L1 job_01M72Y0HQV6NEF0BHY5KC3FCBT 420.1 s; L2 job_01M72RXB5XM79SCDC1SZG4SWA5 (~30 min); L4 job_01M72VM0B919WJKV4XTF10BHDK (~38 min); L3 lost twice (G9-D).
split job_01M72YDCB1R6V8E3ST71SMVDDW (planner's own choice): 4 nodes, 22 chunks, makespan 306.4 s (recorded bound 292.9 s), speedup 1.371 vs L1.
    rates items/s: L1 11.9, L2 2.8, L4 2.2 (L3 rate not listed). report: c2/c3 INCONCLUSIVE only because "no single-node run measured for L3".
    Planner split on run 1 (G9-E: item_seconds declared) - PASS for that part. Need L3 single after the L3 drop fix.
evidence: lab/reports/raw/2026-11-10-G9/b-*
G9-D L3 3rd run job_01M72YVARE8WHZ0AY8GY4ZEEXR (after L3 freed RAM 0.72 -> 2.14 GiB): succeeded, 1 chunk, ~27 min, resumed_chunks=0. PASS.
    => L3 drops were memory pressure (lease renewals late under ~0.7 GiB free), not a link fault. Still worth a Lead look: lease expiry under RAM pressure; L3 log also had
    "stream_ended INTERNAL Internal error from Core" at 14:40:03Z during split job ...VDDW (L3 REPORT).
G9-D verdict: L2, L3, L4 PASS (long single chunk no longer loses its lease).
V0.10 part B with all 4 singles (b-report2): sum_rate 20.0, rate_ratio 1.679, ideal bound 250.8 s, makespan 306.4 s (ratio 1.222), speedup 1.371 vs L1 -> c2 PASS, c3 PASS.

### V0.10 part C (c4) - kill L2's agent at ~31 % - C4 PASS
campaign camp_01M730MT81CN230A1YJTQ20ZM9 (c4.yaml: image_classify 20000, mode3, 4 nodes, seed 41 -> run seed 3811327456). K = job_01M7319V0YV99ZQRVJAN4M9GNH.
kill: 21:01:05 at 6250/20000 items, L2 human Stop-Process -Force on the winservice child (Admin PowerShell, armed beforehand).
result: K succeeded 21:02:12. Lanes: L1 done, L3 done, L2 lost (node_lost: node_offline), L4 "running exit=None" after success (= BUG-3 again).
    chunks 16, max won per chunk 1, items 20000; L1 won 15, L3 1, L2 1 not won, L4 1 not won.
    verify (seed 3811327456): OK - every item once, in order, each value recomputed. (first try with seed 41 = my mistake, PROBLEM.)
    U = job_01M731GH4QW7R0SMQY48RWGZ0R (unsplit on L1): output sha256:9d1c3624... = K output. Same digest. C4 PASS.
evidence: lab/reports/raw/2026-11-10-G9/c4-*
c4 L2 side (chat 20261110-210811): kills at 20:59:56 (early, before K's lanes) and 21:01:21 (the counted one). Service host stayed up and restarted the
    child by itself; L2 back online 21:04 (session epoch 65) = AFTER K ended 21:02:12, so L2 did not rejoin K. Result unchanged: C4 PASS.
    (Note for Lead: Windows service auto-restarts the child, so "do not restart until K ends" cannot be held on Windows; fine here.)

### G9-B c re-run - GPU split over L2 + L4 (L1 excluded for BUG-1, L3 no GPU) - PASS
job_01M73A285PW7B5MEV24MPWHVM2 (--exclude-node JARVISM3): succeeded; lanes L2 done exit=0, L4 done exit=0, combine on L4.
results.json 24 items: RTX 3050 (L4) 18, RTX 2050 (L2) 6 - both GPUs did real CUDA work in one split job. PASS.

### Extra (human request): gpu: preferred split, L2/L4 GPU + L3 CPU - planner chose L2+L4 only (correct by its model)
job_01M73ADNM4FX44KRNNGSVHBV9T (~/mf-eval/gpu-pref: same job, gpu: preferred, CPU fallback; --exclude-node JARVISM3): succeeded, lanes L2 + L4.
explain: L3 feasible on its CPU slot (rate 0.0244 items/s vs GPU nodes 0.167/s, ~7x slower). Sets: {L2,L4} 155.69 s (chosen) vs {L2,L4,L3} 186.88 s -
    adding L3 predicted SLOWER (a 2-item chunk on L3 takes ~82 s, it would finish last). So L3's CPU is used only when it helps. Behaviour OK.
    Idea for Lead: mixed CPU+GPU sets could use per-node chunk sizes (small chunks for slow nodes) so slow CPUs still add work.

### Big mixed job (human demo) run 1 job_01M73AP6VXPTA6NZEE2X90VNCV - cancelled (MY script bug)
480 items / 120 chunks, gpu preferred, --exclude-node JARVISM3: planner chose 3 nodes (L2 GPU, L4 GPU, L3 CPU), predicted 1575.8 s vs 3117.6 s single.
L3's chunks failed "Torch not compiled with CUDA enabled": my run_chunk.py called torch.cuda.synchronize() unconditionally. Fabric OK: failed_tries 3, chunks
re-queued, L2/L4 went on. Rates: L2 0.129, L4 0.084 items/s (L4 slower although RTX 3050: tiny MLP is CPU/launch-bound; L4 CPU ~half of L2 also in mc
runs; power mode already Best performance - open). Fixed script, cancelled at 64/480, re-run.

### Big mixed job run 2 job_01M73B13999W9AWDT89F7KDX2H (480 items, 120 chunks, gpu preferred, L2 GPU + L4 GPU + L3 CPU) - DONE, straggler rescued
planner: split over 3 nodes, predicted 1575.8 s vs best single 3117.6 s. Chunks: L2 64, L4 30, L3 26 (L3 CPU really used: 0.053 items/s), failed 0.
Rates: L2 0.13, L4 0.154 (when healthy), L3 0.053 items/s.
FINDING (L4 hang): chunk 68 on L4 hung from 00:04 IST: launcher 13032 -> python 19640 (431 MB, all 12 threads Wait, CPU 1.6 s), never reached CUDA
    (nvidia-smi 0 MiB), no output; agent healthy and kept the lease for ~17 min. Speculation copied chunk 68 when the queue emptied (speculated 1),
    job finished. Lead: (1) a chunk with no progress/output and 0 CPU holds its lease until the end - could speculate earlier;
    (2) cause of the hang on Windows (torch import/CUDA init deadlock?) unknown - L4 evidence in chat 20261111-002145-L4-to-L1.
L4 answers (chat 20261111-002852): hung pids gone by 00:26 (agent cleaned up, no leak). Driver 592.27, torch 2.14.0+cu126, cuda True.
    Repro 30/30 torch CUDA starts in the same venv OK (2-3 s), no hang; Event Viewer clean. Unusual: 17 s gap launcher -> worker start (normally ~1 s).
BUG-4 (medium, Lead): a chunk process that hangs at start (0 CPU, no output) keeps its lease until the queue is empty (~17 min); speculation rescued it.
    Hang not reproducible outside MeshForge (1 in ~120 chunk starts). Q4 (human activity at 00:04) pending.

### G9-B d / C4.15-c - env.base cuda-pytorch with only L2/L3/L4 - PASS
compute run (pytorch-gpu-venv copy, base cuda-pytorch, gpu required, --exclude-node JARVISM3): 409 "none of the 6 node(s) ... can ever run this job";
L2/L4 "no_runtime_for_base - ... no Docker and its agent's Python has no torch (install Docker ... or env.base python-cpu ...)", L3 "no_gpu". PASS
### G9-B h - started 00:33 IST: all agent nodes on Wi-Fi; check clock_uncertain alerts after 1 h.

### G9-B e (#7) - env_error then owner only_binary=false -> places at once - PASS
1: job_01M73DFYTV45T7KDDGRX4094E2 (docopt==0.6.2 sdist-only, pinned L1): failed env_error "No matching distribution" (wheels-only).
2: HUMAN appended [agent.envs] only_binary = false to /etc/meshforge/config.toml (backup .bak-g9be) + restart; job_01M73DJD6M58AN2N6CY23AKH8K submitted 00:34:45,
   succeeded 00:34:52 "docopt ok 0.6.2" - no env_known_bad TTL wait. PASS. Config restore pending (human).
### 2026-11-11 00:40 - account switch (Lead). No test running. L1 config restore (only_binary) pending human. Next steps: TODO "NEXT".

### G9-B h - nodes on Wi-Fi ~4.5 h (00:33-05:01 IST) - PASS
controller journal since 00:33: 0 clock alerts (clock_uncertain), max clock_offset_bound_ms 75.6 (< 250), 2 info-level clock_inconsistent flips (L2, bound 61 ms)
that returned to consistent within seconds - no alert. PASS.
note: L1 config still has only_binary = false (restore from .bak-g9be pending human).
### 2026-11-11 05:09 - account switch ON (final). No test running. Next: G9-B f (victim L3). L1 config restore still pending human.

### 2026-11-11 ~14:20 - upgrade to v0.17.2 + G10 BUG-1/2 row (G9-B c on L1 GPU lane) - PASS
upgrade: L1 controller+agent 0.17.2-1 (sha256 OK, human apt install); config restored from .bak-g9be (only_binary gone, no tmp_size_mb).
    `compute nodes --json`: JARVISM3, AILESH, LAPTOP-7ORVM30G, Mohit all online 0.17.2.
try 1 job_01M74VBKT76RNKB8GEVNF8NRHD (--exclude-node AILESH, LAPTOP-7ORVM30G, node_01M6K0TZ...): chunk 0 failed FAILURE_TYPE_DISK_FULL at 14:07:36
    after ~10 min Docker env build (torch 2.14 cu wheels ~3 GB). CAUSE = L1 root disk really full (188G, 2.4G free, 99%), not BUG-1 (tmp is now on disk).
    Controller retried the chunk (applied=retried) - correct. Cancelled by me. Human OK'd cleanup: dangling pytorch/pytorch 11.5 GB, docker build cache 2.9 GB,
    uv cache prune 1.7 GB -> 19 GB free.
prepare: `compute node prepare node_01M6BMFV... --envs envs.yaml` (python-cpu + gpu-split requirements, runtime docker): image hit, env 0 hit eddf96c3960c1e91988
    (try 1's second build had finished before the cancel).
run job_01M74WGWAECTYVR15MR0WDKPEC: succeeded, 24/24 items, 12 chunks, 57 s (predicted 2m45s); agent log env_cache "hit" on every chunk;
    results.json: all 24 items gpu = "NVIDIA GeForce RTX 3050 Laptop GPU". L1 GPU lane works on v0.17.2 -> PASS (BUG-1 path OK; env cache hit OK).
small CLI finding: `--exclude-node Mohit` refused "matches 2 nodes" because the old REVOKED Mohit node still matches by name; revoked nodes could be skipped.
evidence: ~/mf-eval/g10/

### 2026-11-11 14:16 - G9-B c run 2: GPU split L1 + L2 + L4 (item_seconds 6, L3 excluded) - job PASS, lane-end timing PARTIAL
job_01M74WKXPN8V7XM1N9HEBYNZ6N: succeeded 51 s (predicted 28 s), 24/24 items, output sha256:7c6bf30f..., combine on L1, all 3 lanes done exit=0.
chunks won: L1 11 (one every ~4.6 s), L4 1 (chunk 2: granted 08:47:00Z, committed 08:47:45Z = 45 s for 2 items), L2 0 (granted 08:46:59Z, still running
    when the job ended 08:47:50Z). Speculation copied the last chunk at 08:47:45Z.
lane end vs last commit (08:47:47.5Z): L2 lane ended 08:47:50.1 (+2.6 s, with job end), L4 lane ended 08:47:51.7 (+4.2 s; ~1.5 s after job end).
    Not inside "~1 s of the last commit" -> PARTIAL; no lane is left "running" (BUG-3 gone).
FINDING (perf, for Lead): a 2-item GPU chunk takes ~45-50+ s on the Windows nodes (L4, L2) vs ~4.6 s on L1 (Docker, warm env). The planner predicted 28 s
    total, so it expects Windows chunks far faster; per-chunk start cost on Windows venv (new python.exe + torch/CUDA init each chunk, D451) looks ~40 s.
    Result: L2 GPU does no work in a short job. Worth a check: L4 agent.log chunk start->first output for this job.

### 2026-11-11 15:01 - G10 BUG-4 b (hung-chunk check, L4 stall_floor_s 60) - PASS (controller side; L4 log lines pending)
setup: L4 [agent.split] stall_floor_s = 60, restarted 14:58:42 (chat 20261111-145916). Job ~/mf-eval/bug4b: python-cpu, every chunk on L4 sleeps 600 s silently.
try 1 job_01M74Z5VRPEYTWXSSBCJF6JQR1 (12 items): succeeded 43 s, but L4's chunk 2 was copied at +29 s by normal end-of-queue speculation -> did not test the stall path.
try 2 job_01M74Z7T6JD6KVJ85B7CAQ9CVB (60 items, 30 chunks, L1+L2+L4): succeeded 2m45s (predicted 2m42s).
    L4 chunk 1 granted 09:32:47.97Z -> controller.split.chunk_stalled (L4) 09:33:59.88Z (+72 s = floor 60 + check) -> speculated chunk 1 reason "stalled"
    to L2 at 09:34:08.07Z while the queue still had work (NOT waiting for the queue to empty) -> resolved 09:34:20Z.
    L4 got chunk 19 at 09:34:21Z (also sleeps); copied to L1 at 09:35:14Z by end-of-queue speculation (53 s, under the floor). Job done 09:35:32Z.
verdict: stall detected ~1 min after start and copied at once -> BUG-4 fix works. (Before: 17 min.) L4 agent.log chunk_stalled + Get-Process pending from L4.
note: L4 receives new chunks after a stalled one (it stalled again); fine for a real rare hang, could matter if a node hangs every chunk.

### 2026-11-11 15:30 - G10 L3 row: G9-D on L3 (v0.17.2) - PASS (normal RAM; low-RAM case still pending)
job_01M74ZGH35HWKBP8EQ1CR3W2MC (~/mf-eval/v010/mc, --node L3): succeeded, 5000/5000 items, 1 chunk, 25m09s (predicted 27m23s), combine on L3,
    output sha256:02af067fd6cd... No grace/resume/node_lost/lease events in the controller log for this job.
L3 free RAM ~2.0 GiB (human cleared RAM before, chat 20261111-141102) -> this is NOT the low-RAM case the Lead row asks for; that part stays open.
evidence: lab/reports/raw/2026-11-11-G10/

### 2026-11-11 15:36 - big mixed job on ALL 4 laptops (human demo) - PASS
job_01M75155RE5YFXJ6FW46DV8HRP (~/mf-eval/gpu-big: 480 items, 120 chunks, gpu preferred, no excludes): planner chose split over 4 nodes,
    predicted 941 s vs best single 2903 s (offload L4). Actual: succeeded in 8m31s (511 s), all 4 lanes done exit=0, combine on L1, failed 0.
chunks committed: L1 81, L4 18, L2 15, L3 6 (CPU). items by device: RTX 3050 (L1+L4) 396, RTX 2050 (L2) 60, CPU Mohit (L3) 24.
    => ~5.7x faster than the predicted best single laptop; every laptop (3 GPUs + 1 CPU) did real work in one job.

### 2026-11-11 15:52 - G9-B f / C7.12-d: revoke L3 with a running job - PASS on data, CLI detail PARTIAL
job_01M7520YAPJM4CC49DRNCPJ337 (mc, --node L3) running; 15:52:24 `compute node revoke node_01M6K0TZRNPEGBRN166GP6S61N` -> "revoked ... (session cut)".
15 s later: job failed, failure_type node_lost; attempt state lost, attempt detail **node_revoked** (jobs --json) = G9 fix works.
BUT job failure_detail = "placed_node_offline" and the CLI table (`compute jobs <id>`) shows only "node_lost / placed_node_offline" -
    "node_revoked" is visible only in --json attempts[].detail. Minor (Lead: show attempt detail in CLI job view or set job failure_detail node_revoked).
L3 re-join: invite inv_01M7523RFJ9HHKF3AZD56R5602 (code handed human-to-human, not in git); L3 agent-side observations pending.
G9-B f L3 side (chat 20261111-160224/160422): 15:52:24.4 stream_ended PERMISSION_DENIED node_revoked -> session stopped; 15:52:27.5 run_cancelled
    (job python gone), service host stayed Running. Re-joined 16:00:48 as NEW node node_01M752J3FZ912G0ZJ9W4ANRPE1, online 16:04. Also: grpc GOAWAY
    too_many_pings at 15:53:23 (after revoke; Lead note). Verdict G9-B f: PASS (revoke cuts session + kills job + fails it as node_revoked); CLI shows detail only in --json.

### 2026-11-11 17:10 - G9-B g / V0.6 G1 with --exclude-node L1 + L3 (Wi-Fi drop on L2) - PASS
(earlier tries job_01M752WVWE56HXK06R1HCX8RHG: no drop, L2 agent session account ended -> succeeded on L2, not counted; job_01M753V6CJ1DQ9KM7FAR3AJS9R cancelled.)
job_01M756GF94HNCF0CWRVWERYFWM (main.py 900, --exclude-node JARVISM3 --exclude-node node_01M752J3FZ912G0ZJ9W4ANRPE1): placed on L2 17:09:51.
L2 human Wi-Fi off ~17:17 -> 17:18:08 attempt_lost (node_offline) + retry_scheduled -> 17:18:13 dispatched attempt 2 to L4 (LAPTOP-7ORVM30G).
    L3 and L1 never used. controller.jobs.execution_overlap logged at 17:18:13 (L2's old process may still run while offline - known, by design warning).
result: succeeded, attempt 2 on L4 done exit=0, output sha256:1f3acec3.... "attempt 2 elsewhere" (open since V0.6 G1, 2026-11-05) now shown. PASS.
L2 off/on times + orphan check pending from L2.

### 2026-11-11 17:36 - C5.11-e (re-run on v0.17.2): 5 GB fetch to L2 over Wi-Fi - PASS
new dataset ds5g@3 (5 x 1 GiB fresh /dev/urandom + labels.csv, so no node has it cached), local sha256 146bceb9f67fa114...98c4.
job_01M7581NM5N8R0B6ZREN56YFW4 (~/mf-lab/reader, input ds5g@3, --node L2): ONE attempt, staging 17:36:43 -> running 17:42:59 (~376 s, ~114 Mbit/s for 5 GiB),
    succeeded 17:43:13. L2 printed dataset sha256 146bceb9...98c4 = L1 -> data intact. No node_lost, no transfer_error retry.
verdict: the v0.17.0 pattern "first attempt of every 5 GB Wi-Fi fetch fails" is gone (G9-A fix). PASS. (L2 agent.cas lines requested.)

### 2026-11-11 17:47 - C5.11-f (re-run on v0.17.2): 5 GB fetch to L3 over Wi-Fi - FAIL (BUG-5 candidate)
job_01M758N58FSW4G69NDF197CY7K (reader, ds5g@3, --node L3 node_01M752J3FZ912G0ZJ9W4ANRPE1): staging from 17:47:22; L3 went
    online->suspect within ~1 min and flapped suspect/online the whole fetch (12:18:59Z, 12:25:20Z, 12:28:00Z, 12:28:25Z back online for <2 s each),
    then suspect->offline 12:34:02Z, lease_expired 12:34:05Z (last_rtt 245.6 ms, commands_unacked 0) -> attempt_lost node_offline -> pinned job failed
    node_lost / placed_node_offline at 18:04:10 IST (~17 min). Controller also re-sent start_attempt 53 times (every ~20 s) during staging.
    L3 still offline right after (free RAM 1.76 GiB at last report).
compare: same dataset to L2 at 17:36 = one attempt, 376 s, PASS. L3 = weak CPU (75.7 GF), 7.7 GiB RAM, Wi-Fi.
BUG-5 candidate (Lead -> BUG-FIX): a 5 GB fetch on a weak/low-RAM Windows node starves the agent's lease/heartbeats -> node suspect for most of the
    fetch -> offline -> job lost. G9-A fix (v0.17.1) is not enough on L3. Need L3 agent.log (cas + session lines) for 17:47-18:05.
evidence: lab/reports/raw/2026-11-11-G10/c511f-*.txt/.log
C5.11-f r2 job_01M759S1XCAM7R913TESW2R1ND (same, after L3 back online; L3 free RAM 0.84 -> 1.44 GiB, human closed apps during staging):
    ONE attempt, succeeded 18:10:54, staged by 18:10:43 (~4 min - faster: part of ds5g@3 was likely already in L3's cache from the failed run = reuse/resume).
    L3 sha256 146bceb9...98c4 = L1 -> intact. Brief suspect<->online flicks (<1 s) at 12:37:22, :25, :55, 12:39:53, 12:40:00Z, no drop.
verdict C5.11-f: r2 PASS; r1 FAIL stays as BUG-5 candidate = a 5 GB fetch on L3 with < ~1 GiB free RAM starves heartbeats -> offline -> job lost.
CORRECTION C5.11-f r1 (human, 18:15): L3's Wi-Fi actually DISCONNECTED during r1. So r1 = real network loss; the pinned job failing node_lost is
    correct behaviour. BUG-5 candidate WITHDRAWN (the earlier suspect flaps fit a weak/dropping Wi-Fi link, not heartbeat starvation). C5.11-f = PASS (r2).
C5.11-f r3 (low-RAM attempt) job_01M75AFTY19Q1S2W3R8ENGSCS4: NEW dataset ds5g@4 (fresh 5 GiB, sha256 ef2f1aaa...0bdd, nothing cached), --node L3,
    L3 free RAM 0.74 GiB at start -> 1.6 GiB mid-run; ran IN PARALLEL with C5.11-g J1 to L4 for the first minutes (shared L1 uplink, human OK).
    ONE attempt, 18:19:2x -> succeeded 18:27:03 (~7.5 min). L3 sha256 ef2f1aaa...0bdd = L1. Only <1 s suspect flicks. PASS.
    Note: RAM rose during the run, so the "< 1 GiB the whole time" case is only partly covered.
C5.11-g J1 job_01M75A5YJKS51FZ5W0JWW54SGA (ds5g@3 to L4): succeeded 18:19:26 with NO agent stop (L4 did not act; L4 silent since 14:59) -> kill/resume NOT tested; redo.

### 2026-11-11 18:40 - C5.11-g (re-run on v0.17.2): 5 GB fetch to L4, agent restart mid-download, resume - PASS
fresh dataset ds5g@5 (sha256 3a1032de...7a78, never on L4). Times fixed by chat (start 18:40, stop 18:42); L4 human Admin PowerShell.
J1 job_01M75BNHZWCKYJD160E4AJ7SZP: started 18:40:01; 18:42:01 session.closed agent_closed (L4 Stop-Service), back online 18:42:19 -> J1 failed
    node_lost (pinned; expected).
J2 job_01M75BT6Y5AQER3AF64GS76WW4 (same, submitted 18:42:33): succeeded 18:45:41 (~3 min vs ~6 min for a full 5 GB). Transfer lines:
    dataset tree 8aa131ed: **cache hits=3 (2048.0 MiB) resumed_chunks=10 refetched=0**; from controller 2992.0 MiB (374 units).
    L4 sha256 3a1032de...7a78 = L1 -> intact.
verdict c3: resumed_chunks = 10 > 0 and 2 GiB reused (not fetched again) -> PASS. (v0.17.0 result was FAIL: no resumed_chunks field.)

### 2026-11-11 18:49 - G10 BUG-2 fail path (torch==0.0.0) on L1 lane - PASS
job_01M75C6EE8DBF1V5TWTSY0FTE1 (~/mf-eval/bug2f = gpu-split with requirements torch==0.0.0, L1 only): submitted 18:49:14, failed 18:49:19 (5 s).
    agent log: ONE agent.execution.run_failed setup_failed at 18:49:17 -> agent.lane.env_failed -> lane ended; no second install attempt.
    job: failed env_error "no lane left with 12 of 12 chunks to run: env build failed on this node: setup_failed: requirements install failed (exit 1)".
verdict: exactly one install attempt, failed env_error right after it (before v0.17.2: retried per chunk). PASS.

### 2026-11-12 - G11 perf evidence (LEAD chat 20261112-035020): per-chunk times, grant -> committed (controller journal)
job_01M74WKXPN8V7XM1N9HEBYNZ6N (2 items/chunk): L1 11 chunks 4.5-4.8 s each; L4 1 chunk 46.8 s; L2 0 chunks.
job_01M75155RE5YFXJ6FW46DV8HRP (4 items/chunk):
| node | chunks | 1st chunk | rest avg (min-max) | pure train for 4 items (results.json s) | overhead per chunk |
| L1 Linux docker | 81 | 6.5 s | 6.3 (6.1-6.5) | ~6.2 (1.54 s/item) | ~0 |
| L4 Win venv 3050 | 18 | 42.3 s | 26.6 (24.3-28.2) | ~6.2 (1.54 s/item) | ~20 s every chunk (+~15 s on the 1st) |
| L2 Win venv 2050 | 15 | 43.2 s | 31.5 (26.9-36.3) | ~21.4 (5.34 s/item) | ~10 s every chunk (+~12 s on the 1st) |
| L3 Win venv CPU | 6 | 93.9 s | 77.9 (76.7-79.4) | ~66.6 (16.66 s/item) | ~11 s every chunk (+~16 s on the 1st) |
reading: NOT first-chunk only. Every Windows chunk pays ~10-20 s fixed cost (new python.exe + torch/CUDA import per chunk); the first chunk
    adds ~12-16 s more (lane setup). L1 (Linux, Docker, warm env) has ~0 per-chunk cost. Phase times per chunk are not in `jobs --json`
    (only per-attempt transfers); numbers are from controller.split.granted -> chunk_result committed.

### 2026-11-12 03:56 - G10 BUG-4 a: Mode 3 GPU split on venv (as job_01M73B13999W9AWDT89F7KDX2H) - PASS (controller side; L4 process check pending)
job_01M76BG06PMZTQ5MHRBZT3JVSN (~/mf-eval/gpu-big, 480 items / 120 chunks, gpu preferred, --exclude-node JARVISM3): succeeded 25m27s (predicted 20m37s),
    lanes L4, L2, L3 done exit=0, combine on L4. Chunks: L4 58 (GPU, rest avg 25.2 s, max 36.1), L2 46 (GPU, rest avg 31.6 s, 1st 70.8),
    L3 16 (CPU, avg 92.5 s). chunk_stalled / stalled speculation: 0. No hung chunk in 120 chunk starts (before: chunk 68 hung 17 min on L4).
    Per-chunk max on L4 36.1 s -> no outlier. L4 Get-Process (one python.exe per chunk, Program Files interpreter, no launcher) pending from L4.
BUG-4 a process check (chat 20261112-045057-L4-to-L1): job_01M76E3YHA1YT5157097JWD9Q6 (bug4a2, 160 items, L4 only). L4 samples 04:45-04:50:
    ONE python.exe per running chunk, parent = agent child 21076, cmdline "C:\Program Files\MeshForge\0.17.1-1\python\python.exe run_chunk.py ...";
    no venv launcher, no py.exe, no chunk_stalled -> BUG-4 a PASS (complete).
FINDING (L4, medium, for LEAD/BUG-FIX): chunk runs the 0.17.1-1 interpreter while agent is 0.17.2-1 - venv 8e2ebada... built under 0.17.1 keeps
    its base python = C:\Program Files\MeshForge\0.17.1-1. If old version folders are removed (cleanup/uninstall), cached venvs break; env hash does
    not include interpreter path/version. Fix idea: add base interpreter path/version to env_hash or rebuild venvs after upgrade. Relevant to 0.17.6.

### 2026-11-12 13:2x - v0.17.3 upgrade + D452 (names skip revoked nodes) - PASS
upgrade: L1 controller+agent 0.17.3-1 (sha256 OK, human apt install); `compute nodes`: JARVISM3, AILESH, LAPTOP-7ORVM30G, Mohit online 0.17.3. config clean.
D454 baseline: `df -h /var/lib/meshforge` = 161G used, 18G avail.
D452 (old revoked Mohit node_01M6K0TZ... still in the fabric): 
  `compute run --exclude-node Mohit` -> accepted (was refused "matches 2 nodes" on 0.17.2); job_01M77CATA0BPMAD8NAJT92M4BZ succeeded on L1, not L3.
  `compute run --node Mohit` -> accepted; job_01M77CATP6PGZZGG7324X8MK62 succeeded on the LIVE L3 node_01M752J3FZ912G0ZJ9W4ANRPE1. PASS.

### 2026-11-12 13:3x - D457 (`run --local` with the packaged agent) - PASS; demo 04 act C FAIL (demo-script issue)
`ls -ld`: /var/lib/meshforge drwxr-x--- meshforge; /var/lib/meshforge-public drwxr-xr-x meshforge; node-id.json `644 meshforge` = as expected.
demo 01: PASS in 3.0 s (job_01M77CKNWVQSST7SQNWCH8QPQJ ran on L1, result.json right). Was FAIL exit 4 on 0.17.2.
demo 04 (--dashboard-url): Local act PASS (job A kept on L1; job B offloaded Mode 1, Local rejected slots_full).
    Act C FAIL: job_01M77CN3KBYQ9TGJBZYGE066SN (48 items) planned LOCAL, not Mode 3. explain: split set 52.64 s < local 83.81 s, but
    "not split: ... only on assumed per-item rates - no run of this workload is recorded yet ... (set split.item_seconds in compute.yaml to split
    on the first run)". = the G9-E safety rule; the demo's job C has no split.item_seconds and a new name each run, so it is always "new".
    Job C still succeeded with correct output. FINDING (Lead): demo script job C must declare split.item_seconds (planner is right).
verdict D457: PASS.

### 2026-11-12 13:4x - D454 (`data rm` frees + `compute data gc`) - PASS
setup: pushed d454test@1 (2.0 GiB random). df before rm: 163G used / 16G avail.
`compute data rm d454test@1` -> "freed 0 blobs / 0 B on the controller; 2 recently used blobs kept (run `compute data gc` later)" - prints bytes freed;
    0 here because the blobs were just uploaded (min_age protection), told clearly.
`compute data gc --dry-run` -> "would free 29 blobs / 20.0 GiB; kept 2 unreferenced blobs used recently (2.0 GiB)".
`compute data gc` -> "freed 29 blobs / 20.0 GiB" (the removed ds5g@1-@5 from 2026-11-11). df after: 143G used / **36G avail** (+20 GB). PASS.
note: the brand-new d454test blobs stay until min_age passes (by design, protects upload-then-submit).

### 2026-11-12 13:38 - D453 (revoke cause in the normal job view) - PASS
job_01M77CSJ55MTNAD9SM6SYYBG2V (main.py 900, --node Mohit) running on L3; 13:38:29 `compute node revoke node_01M752J3FZ912G0ZJ9W4ANRPE1` (session cut).
`compute jobs <id>` (no --json): state failed, failure_type node_lost, failure_detail placed_node_offline, **failure_cause node_lost: node_revoked**,
    row **failed: node_lost: node_revoked**. (0.17.2 showed node_revoked only in --json.) PASS. L3 re-join pending (human code).

### 2026-11-12 13:39-13:54 - D456 (planner learns per-node chunk cost) - big split PASS, small split PARTIAL
(L3 revoked during these runs -> L1/L2/L4 only.) First Windows chunk after the upgrade = venv rebuild (D458): L2 201 s, L4 180 s (one time).
| run | job | planner chose | predicted | real | chunks |
| warm-up 24 it | job_01M77CWJ7H2ZGJ48K5GKH11H6Y | split L1+L2 | 40.4 s | 67 s | L1 12 (L2 rebuilding) |
| run1 24 it | job_01M77CYMPG7WVE8YSYH5PM6M79 | local L1 | 61.7 s | 67 s | L1 12 |
| run2 24 it | job_01M77D0Q4QDFHSYTSMGT4FZ596 | split L1+L2 | 41.1 s | 57 s | L1 12 (L2 chunk >50 s rebuild -> speculated) |
| **big 480 it** | job_01M77D2FNRE7SCB1KSGDZ0NMS4 | split L1+L4+L2 | **557.4 s** | **558 s** | L1 92 (6.1 s), L2 14 (26.2 s, 1st 201), L4 14 (27.4 s, 1st 180) |
| run3 24 it (warm) | job_01M77DM46NTQ3777EN6EDY18SP | split L1+L2 | 38.3 s | 52 s (+36 %) | L1 10 (4.8 s), L2 2 (18.0, 16.1 s) |
| run4 24 it | job_01M77DNQN6SRTRVPBPFYTM62AK | local L1 | 60.3 s | 58 s | L1 12 |
chunk_overhead (big run): L1 fixed 0.21 s; **L2 fixed 2.79 s +12.1 s first; L4 fixed 2.28 s +16.9 s first** (source node history). First-chunk costs match
    LEAD's expectation (L2 ~12, L4 ~16); fixed costs lower than expected (2-3 s vs 4/14 s).
FINDING: for the 24-item gpu-split workload L2's overhead stays "shared" 0.015 s (no per-node sample for this workload) -> run3 under-predicts by 36 %
    (> 20 %). Per-node cost seems learned per workload, not per node across workloads. Planner also alternates local/split on identical runs (run1/3 vs 2/4).
vs v0.17.2: big job (L2+L4+L3, L1 excluded) 25m27s; today L1+L2+L4 558 s - not same set, no direct speed claim.
verdict D456: PASS for the big split (0.2 % error); PARTIAL for small 24-item split (36 % error, L2 cost not learned for that workload).

### 2026-11-12 14:0x - 1.0.7 demos on v0.17.3: 01 PASS, 02 PASS, 03 FAIL, 04 act C FAIL (same demo-script cause)
demo 02: PASS 8.9 s (Local on L1, Mode 1 to L2, result byte-identical).
demo 03 (--kill-cmd/--revive-cmd = lab-chat cue helper ~/mf-eval/demo/kill_via_chat.sh): FAIL 10.9 s - planner ran the job LOCAL ("not split ...
    only on assumed per-item rates - no run of this workload recorded yet"), job ended before a laptop could be killed; no stop cue sent.
cause (03 + 04 C): demo split projects have a fresh name each run and no split.item_seconds -> G9-E rule never splits on first run. Sent to LEAD.

### 2026-11-12 14:2x - planner local/split flapping evidence (LEAD 20261112-141539 item 3)
run1 job_01M77CYMPG7WVE8YSYH5PM6M79 -> LOCAL: candidates local L1 61.67 s, split L1+L4 83.14 s, offload L4 541.6 s; **L2 offload infeasible
    reject_code slots_full** (L2 still busy from the previous job's lane / its rebuilding chunk) -> "not split: best set 83.14 s > 55.50 s".
run2 job_01M77D0Q4QDFHSYTSMGT4FZ596 -> SPLIT: split L1+L2 41.10 s, split L1+L2+L4 64.26 s, offload L2 61.97 s, local 62.28 s -> "split over 2 nodes".
reading: flapping = L2 availability between back-to-back runs (slots_full while its previous lane/speculated chunk still runs), not a cost bug.
    L4 alone is slow for this small job (541 s offload = venv rebuild history?). Evidence: ~/mf-eval/g10/flap-explain.txt.

### 2026-11-12 14:2x-14:40 - demos after a69266bb: 04 PASS; 03 FAIL x2 (hook timeout, then job too small)
demo 04: PASS 42.4 s (A local, B Mode 1 slots_full, C split Mode 3, output correct).
demo 03 r1 (14:19): split over 3 laptops OK; kill hook (lab-chat cue to L2) timed out after the demo's 120 s - human cue too slow (my process).
demo 03 r2 (timed: L2 scheduled stop 14:40:00, demo start 14:39:48): job_01M77GAEVZBR8SEV3T871BP2JV planned LOCAL - "not split: best set 12.79 s
    above 0.9 x 13.02 s". With history the 48 x 400k job takes ~13 s on L1; split overhead to Windows (~12-17 s first chunk) makes splitting lose.
    Planner correct; demo LIVE profile too small. Sent to LEAD (bigger split_rounds).

### 2026-11-13 12:2x - v0.17.4 upgrade; D459 PARTIAL (cost source PASS, time +41 %); D460 PASS
upgrade: L1 controller+agent 0.17.4-1 (sha256 OK, human apt); L2, L4 online 0.17.4; L3 still revoked (re-join = demo 05).
warm-up 480 split job_01M79TVY566EVNPVT96PPE78W4 (L1+L4+L2): predicted 529.9 s, real 621 s (+17 %); first chunk after upgrade = venv rebuild
    (L2 224 s, L4 181 s); L1 93 / L4 17 / L2 10 chunks.
D459 new workload d459-new-1232 (24 items, chunk 2, item_seconds 6) job_01M79VEXHKHQ3QNYG24NKTB7DK: split L1+L2+L4; chunk_overhead note
    "T241P436 0.5s (node_history venv n=16) +1.5s first, YQ6SF318 1.6s (node_history venv n=18) +0.5s first" -> per-node source PASS (not shared).
    predicted 71.1 s vs real 42 s = over by 41 % -> time criterion NOT met; cause: compute rate from the declared item_seconds 6 (real ~2.4 s/item
    on L1 GPU, local predicted 152 s vs ~58 s measured before). Lab job over-declares; not a D459 defect.
D460 same workload 4x back to back (15 s idle between): run1-4 job_01M79VH9W5J6FPW8QX3S3TGPQS / ...K3GFKB7TEGASXS4WXA80 / ...MX5D8CEFVTFNHJYD7HK1 /
    ...PPSG8X77KV3CH5MHD0ZG: all mode3 L1+L4+L2, real 44 s each; runs 1-3 margin "kept previous mode mode3 (new best local 60.66s within 10% of
    56.73s)". PASS. (pred ~57 s vs 44 s real: +30 %, history now pulls toward real.) kept explain: raw/2026-11-13-G12/d460-kept-explain.json

### 2026-11-13 12:4x-12:57 - demos on v0.17.4: 04 PASS, 03 PASS
demo 04: PASS 131.4 s (job C split Mode 3 with the 4M-round job).
demo 03: PASS 101.3 s. job_01M79WRFTN90P3WJWMA0NT594A split over 3 nodes (111.62 s <= 0.9 x 199.03 s); victim L2 picked at 7.4 s; L2 human pre-armed
    a timed Stop-Service at 12:56:00 (+ Start at 12:57:00), demo started 12:55:40, kill hook waited for L2 offline. Job still succeeded, L2 lane not
    done, all 48 results in order and correct, < 300 s. (Process: context + fixed time sent first, started only after L2 "armed".)
demo status v0.17.4: 01 PASS (v0.17.3), 02 PASS (v0.17.3), 03 PASS, 04 PASS, 05 pending (= L3 re-join).
note for evaluation: prediction accuracy is judged on big jobs (480-item +17 %, yesterday 0.2 %); small 24-item jobs over-predict 30-41 % from the
    declared item_seconds 6 (real ~2.4 s) - fixed costs dominate small jobs.
demo 05: PASS 219.3 s - invite listed pending (code never shown again), L3 re-joined with the code (handed human-to-human by phone),
    new node node_01M79X8WTANK64QH74W9YMHBG5 (Mohit, 0.17.4) online. => all 5 demos PASS (01/02 on v0.17.3, 03/04/05 on v0.17.4).
    L3 back: all 4 nodes online 0.17.4.

### 2026-11-13 13:38 - 0.14.8 A0 campaign STARTED: camp_01M79Z60H7TNQ674ZS2MMKCQAG (lab-v014-planner-r5)
spec = checklist A0 (cpu.primes x10, cpu.zlib x10, split.image_classify x10; local/mode1/mode3; all 4 nodes; origin L1) with repetitions 5
    (human: cleaner stats) and budget max_runs 700 / max_hours 10. plan: 650 runs = 130 configurations x 5, ~4.64 h on the reference laptop.
    `compute bench start --per-node 1 --cooldown 2` at 13:38:01. Pre-check: all 4 online 0.17.4, accept_jobs True, pause_benchmarks False;
    humans: plugged in, sleep Never, apps closed (L2 ~3.5 GB after closing Claude, L3 2.4 GB after reboot, L4 6.1 GB). spec: raw/2026-11-13-G12/v014.yaml
FINDING L3: after a reboot 13:28 MeshForgeAgent was Stopped (StartType Automatic), 2nd time - sent to LEAD.
13:4x small campaign camp_01M79Z60H7TNQ674ZS2MMKCQAG CANCELLED at 14 runs (human): runs were ~6 s each = small-job regime, not the product's target.
### 2026-11-13 13:58:45 - 0.14.8 A0 BIG campaign STARTED: camp_01M7A0BZQB5AN4TBW0CY7M4DZY (lab-v014-planner-big)
spec raw/2026-11-13-G12/v014big.yaml: cpu.primes limit 200M-380M (repeats 10), cpu.zlib 160-250 MB level 9 (repeats 6), split.image_classify
    11k-20k images (workload max 20k); local/mode1/mode3; all 4 nodes; 3 repetitions; 390 runs, ~7.11 h reference; budget 700 runs / 24 h.
    Human decision: long jobs only (product target; predictions closer on big jobs), 3 repeats enough. All 4 ready (L2 3.3 GB, L3 2.2 GB free).

### 2026-11-13 17:58:24 - FINAL PLAN Run 1 (planner exam 0.14.8) STARTED: camp_01M7AE2T3K7AMXP5EAEEZHFKMB
spec raw/2026-11-13-G12/run1g.yaml: ml.cnn_train batch 40-256 (local+mode1) + split.image_classify 9k-20k images (local+mode1+mode3), 12 sizes,
    3 reps, origin L1, all 4 nodes; 324 runs, 24 groups, 4.88 h ref. Pre-check: all 4 online 0.17.4, no jobs; free RAM L1 9.9, L2 2.9, L3 3.2,
    L4 6.4 GB; humans ready (plugged, sleep Never). Datasets for 2B already on L1 (Tiny ImageNet 237 MB, IMDB 41 MB via Hugging Face).
Earlier today: campaigns camp_01M79Z60H7TNQ674ZS2MMKCQAG (small jobs, 14 runs) and camp_01M7A0BZQB5AN4TBW0CY7M4DZY (big A0, 15 runs) cancelled for re-planning.
