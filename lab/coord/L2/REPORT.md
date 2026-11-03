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

---
# Round 1b

### R1b-2 - numpy for real CPU scores - OK (exit 3 only because of --skip-gpu)
when: 2026-11-01 10:39 (local)
laptop: L2 (Windows 11, hostname AILESH)
commands:
    copy l2-d76354d8.json -> l2-d76354d8__nonumpy.json
    uv sync --extra agent
    uv run python lab/spikes/s7_hardware.py run --name L2 --skip-gpu
result: exit 3 (not measured: gpu_skipped). float32 1t 95.59 +- 6.06 GFLOP/s, all 315.80 +- 8.84; float64 1t 43.66 +- 3.40, all 97.53 +- 6.12; python 1t 26.00 MFLOP/s, all 132.58.
    Memory: copy 14.51 +- 1.20 GB/s, scale 8.72, add 9.11, triad 5.49. No battery/throttling warnings.
    The new harness wrote a new file (__run2) instead of overwriting, so the round-1 bug is fixed on main.
evidence: lab/reports/raw/2026-11-01-S7-hardware/l2-d76354d8__run2.json (numpy), l2-d76354d8__nonumpy.json + l2-d76354d8.json (round-1 full run, identical copies)

### R1b-1 - Wi-Fi profile Private - OK
when: 2026-11-01 10:54 (local)
laptop: L2 (Windows 11, hostname AILESH)
commands:
    Get-NetConnectionProfile
result: human switched it. Name JARVIS, InterfaceAlias Wi-Fi, NetworkCategory Private.
evidence: this entry

### R1b-3 - CUDA torch - PENDING (human has not answered yes/no yet)

---
# Full campaign

### C0-a - Same commit - OK
when: 2026-11-01 10:55 (local)
laptop: L2 (Windows 11, hostname AILESH)
commands:
    git fetch origin; git checkout lab/L2; git merge origin/main; git rev-parse --short HEAD; uv sync --extra agent
result: HEAD 1b31e6ea (lab/L2 with origin/main merged); uv sync --extra agent OK.
evidence: this entry

### C0-b - Power, updates, profile - OK (updates pause not confirmed)
when: 2026-11-01 10:55 (local)
laptop: L2 (Windows 11, hostname AILESH)
commands:
    (human, admin) powercfg /change standby-timeout-ac 0; powercfg /change hibernate-timeout-ac 0
    powercfg /query (check); Get-NetConnectionProfile; w32tm /query /status; [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
result: standby AC 0, hibernate AC 0 (never). Profile Private. Unix time 1793510736.
    Windows Update pause: not visible in HKLM\...\WindowsUpdate\UX\Settings PauseUpdatesExpiryTime (empty); human asked to pause.
    w32tm: Leap Indicator 3 (not synchronized), Stratum 0, last successful sync 01-11-2026 08:56:22, source time.windows.com. Relevant for C4.15-a later.
evidence: this entry

### C0-c - Ready - OK
when: 2026-11-01 10:55 (local)
result: ipconfig Wi-Fi IPv4 192.168.31.155.
SIGNAL: L2-ready ip=192.168.31.155 os=windows host=AILESH

### C2.4-a - Network facts (0.2.4) - OK
when: 2026-11-01 10:57 (local)
laptop: L2 (Windows 11, hostname AILESH)
commands:
    netsh wlan show interfaces; Get-NetAdapter | ft Name,Status,LinkSpeed; Get-NetConnectionProfile
result: Wi-Fi JARVIS 5 GHz ch 157, signal 92%, rx/tx 1201 Mbps, profile Private. Same room as L1: yes (R1-1). DHCP reservation: unknown (L1's human handles the router).
evidence: lab/reports/raw/2026-11-01-V0.2-network-facts/L2.txt

### C2.0-h - HUMAN BATCH 1 (S1 firewall) - OK
when: 2026-11-01 11:05 (local)
laptop: L2 (Windows 11, hostname AILESH)
commands:
    (human, Administrator PowerShell) New-NetFirewallRule "MeshForge S1 TCP" / "MeshForge S1 UDP" (port 47201, 192.168.31.0/24, Private)
result: both rules created (Enabled True, Private, Inbound, Allow). First attempt in a non-admin window: "Access is denied" for both; retried as Administrator.
evidence: this entry

### C2.5-a - S1 server (0.2.5) - OK (running)
when: 2026-11-01 11:08 (local)
laptop: L2 (Windows 11, hostname AILESH)
commands:
    uv run python lab/spikes/s1_network.py serve --bind 192.168.31.155 --idle 3600 --duration 14400   (own terminal window)
result: "S1 server 'AILESH' listening on 192.168.31.155 port 47201 (TCP + UDP); LAN clients only; stops after 3600 s idle or 14400 s total."
BUG: (environment) in the user's interactive PowerShell, uv printed "Ignoring existing virtual environment linked to non-existent Python interpreter: .venv\Scripts\python.exe", re-downloaded cpython 3.12.16 and rebuilt .venv (82 packages). Same uv-managed-Python link problem as R1-2. The rebuilt .venv may lack the `agent` extra; will re-run `uv sync --extra agent` before steps that need it.
SIGNAL: s1-serve-up L2 ip=192.168.31.155

### C0-b (addendum) - Windows Update paused - OK
when: 2026-11-01 11:10 (local)
result: human paused updates; PauseUpdatesExpiryTime 2026-11-07T05:37:49Z (renew before the V1.0 soak). R1b-3 torch: human said YES; deferred until S1 measurements on L2 are finished (no download during a measurement).

### C2.5-b - S1 client L2 -> L3 (0.2.5) - OK
when: 2026-11-01 11:32-11:37 (local)
laptop: L2 (Windows 11, hostname AILESH), Wi-Fi 5 GHz, same room
commands:
    uv run python lab/spikes/s1_network.py client --link wifi --server 192.168.31.236   (L4 not present: skipped)
    then Ctrl-C of the L2 S1 server ("interrupted (Ctrl-C); server stopped")
result: exit 0. TCP x1 L3->L2 207.97 +- 5.90 Mbit/s; x4 L3->L2 247.44 +- 11.88; TCP x1 L2->L3 165.03 +- 5.27; x4 L2->L3 169.88 +- 19.43.
    UDP echo: RTT mean 2.274 ms, p95 3.457 ms, jitter 0.740 ms, loss 0 %.
evidence: lab/reports/raw/2026-11-01-S1-network/ailesh-1ad242f0__to-tiwari_ji_wifi-3f7a2a51.json
SIGNAL: s1-client-done L2

### C2.5-c - Remove S1 rules - OK
when: 2026-11-01 11:43 (local)
commands:
    (human, Administrator) Remove-NetFirewallRule -DisplayName "MeshForge S1 TCP","MeshForge S1 UDP"
    Get-NetFirewallRule -Group "MeshForge lab"; Get-NetFirewallRule -Direction Inbound -Action Block -Enabled True | Where-Object DisplayName -like "*python*"
result: no "MeshForge lab" rules left; no inbound block rule for python.

### C2.6-a - S2 client L2 -> L1 (0.2.6) - OK
when: 2026-11-01 11:44-12:16 (local)
laptop: L2 (Windows 11, hostname AILESH), Wi-Fi 5 GHz, same room; no inbound firewall rule on L2
commands:
    uv run python lab/spikes/s2_crossos.py client --server 192.168.31.13 --link wifi --name L2 --no-inbound-rule --note "Defender prompt: none seen by agent at start; human watching"
result: exit 0. Windows 11 client -> Linux 6.8.0-147 server: http-ping, http-blob, grpc-unary, grpc-stream, grpc-bidi idle 30 s / 120 s with keepalive on and off: all "yes"; outbound-only (ADR-0002) yes.
    HTTP first byte 3.68 ms, HTTP stream 300.4 Mbit/s, gRPC unary RTT 4.89 ms, gRPC stream 319.3 Mbit/s.
    Defender prompt: none reported by the human during the run.
evidence: lab/reports/raw/2026-11-01-S2-crossos/l2-d76354d8__to-l1_wifi-3b3edc51.json
SIGNAL: s2-done L2-to-L1

### C2.6-b - HUMAN BATCH 2 (S2 server rule) - OK
when: 2026-11-01 14:08 (local)
commands:
    (human, Administrator) New-NetFirewallRule -DisplayName "MeshForge S2" ... -LocalPort 47202-47203 -RemoteAddress 192.168.31.0/24 -Profile Private
result: rule present, Enabled True, Private. Human reported no Windows Security / Defender pop-up during C2.6-a.

### C2.6-c - S2 server L2 (0.2.6) - running
when: 2026-11-01 14:08 (local)
commands:
    uv run python lab/spikes/s2_crossos.py serve --bind 192.168.31.155 --name L2   (own terminal)
    second, same command while the first runs (port exclusivity)
result: server: "S2 server listening on 192.168.31.155: HTTP 47202, gRPC 47203; LAN peers only; stops after 900 s idle or 7200 s total."
    second serve: exit 1, "serve: cannot listen on 192.168.31.155 (HTTP 47202 / gRPC 47203): [WinError 10048] Only one usage of each socket address ... is normally permitted" -> port exclusivity OK.
BUG: s2_crossos.py serve prints "warning: these adapters are NOT on a Private profile: Wi-Fi" although Get-NetConnectionProfile shows JARVIS / Wi-Fi / NetworkCategory Private at the same moment. Profile check gives a false warning. Also the "§" in that warning prints as "Â§" when stderr is redirected (encoding).
SIGNAL: s2-serve-up L2
    (C2.6-c continued) 14:43: L1 emitted s2-done L1-to-L2 (L1 sessions from 192.168.31.13 seen in the server window); server stopped with Ctrl-C. Firewall rule removal pending (human).

### C2.7-a - S3 client L2 -> L1 (0.2.7) - OK
when: 2026-11-01 14:50 (local)
laptop: L2 (Windows 11, hostname AILESH), Wi-Fi 5 GHz, same room
commands:
    uv run python lab/spikes/s3_transport.py client --server 192.168.31.13 --link wifi --max-size 256MiB --name L2
result: exit 0. 256 MiB: HTTP GET 289.9 +- 10.7 Mbit/s; HTTP 4x Range 287.4; raw TCP download 297.3 / upload 217.9; gRPC client-stream upload 64 MiB 242.4.
    1 MiB is slowest (HTTP GET 165.5, raw TCP 137.2). Full table in the JSON.
evidence: lab/reports/raw/2026-11-01-S3-transport/l2-d76354d8__to-jarvism3_wifi-8142940d.json
SIGNAL: s3-done L2

### C2.3-a - GPU exception (0.2.3) - OK
result: L2 GPU stack: nvidia-smi OK (RTX 2050, driver 581.83, CUDA 13.0, R1-4); torch: not installed yet (R1b-3 approved, download after the V0.2 measurements). Docker GPU container: not tested - no Docker on L2 (Lead decision); CPU/venv fallback. S6/S7 ran in rounds 1/1b.

### R1b-3 - CUDA torch + GPU score - OK, run 1 with thermal-throttling warning
when: 2026-11-01 14:52-15:01 (local)
laptop: L2 (Windows 11, hostname AILESH); human approved the ~2.5 GB download; done after the V0.2 measurements
commands:
    uv pip install "torch==2.14.*" --index-url https://download.pytorch.org/whl/cu126
    uv run --no-sync python -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
    uv pip install numpy   (numpy was missing: the .venv rebuilt in C2.5-a had no agent extra; --no-sync so uv does not remove torch)
    uv run --no-sync python lab/spikes/s7_hardware.py run --name L2
result: torch 2.14.1+cu126, cuda True, NVIDIA GeForce RTX 2050. S7 exit 0:
    GPU float32 4.84 +- 0.01 TFLOP/s; float16 11.36 +- 0.47; bfloat16 11.54 +- 0.30; device copy 103.06 GB/s.
    CPU float32 1t 76.77, all 99.09 GFLOP/s (much lower than R1b-2's 315.80); float64 1t 36.16, all 127.48. Memory copy 9.34 GB/s.
    "!! thermal throttling suspected (last repetition much slower than the first): cpu-float32-1t". Asking the human to fix and rerun.
evidence: lab/reports/raw/2026-11-01-S7-hardware/l2-d76354d8__run3.json

### R1b-3 (run 2) - GPU score after cooling - OK, throttling warning again
when: 2026-11-01 15:15-15:18 (local)
laptop: L2; human: plugged in (PowerOnline True), hard surface, ~5 min cool-down
commands:
    uv run --no-sync python lab/spikes/s7_hardware.py run --name L2
result: exit 0. GPU float32 4.85 +- 0.02 TFLOP/s; float16 11.26 +- 0.23; bfloat16 11.29 +- 0.15; copy 103.04 GB/s (GPU numbers stable across both runs).
    CPU float32 1t 96.58, all 240.89 GFLOP/s; float64 1t 49.58, all 133.85; memory copy 9.06 GB/s.
    "!! thermal throttling suspected: cpu-float64-1t, cpu-python-1t" (different cases than run 1). Not rerun again: this laptop seems to throttle on CPU under sustained load; for the Lead to judge. CPU all-core float32 varies a lot between runs: 315.80 (R1b-2), 99.09 (run 1), 240.89 (run 2).
evidence: lab/reports/raw/2026-11-01-S7-hardware/l2-d76354d8__run4.json
QUESTIONS: Should L2's S7 CPU numbers be taken from R1b-2 (no GPU load before, no warning) or from run 2?

### R1b-4 - Round 1b done
## DONE round 1b

### C2.r-a - Push all raw files - OK
result: pushed lab/reports/raw/2026-11-01-S1-network, -S2-crossos, -S3-transport, -V0.2-network-facts, -S6-environments, -S7-hardware, lab/reports/inventory/AILESH.json, lab/reports/gpu/.
SIGNAL: v02-raw-pushed L2
    Open: firewall rule "MeshForge S2" still present on L2 (human asked to remove it).
SIGNAL: V0.2-done

### C17-a - Download + check - OK
when: 2026-11-01 15:19 (local)
commands:
    gh is not installed on L2: used the GitHub REST API (releases/tags/v0.17.0, asset download) with the stored git credential; saved to $HOME\mf-pkgs
    sha256 of MeshForge-Agent-Setup-0.17.0-1.exe vs SHA256SUMS
result: MeshForge-Agent-Setup-0.17.0-1.exe 57,488,826 bytes; SHA256 3e95eb45...183753d matches SHA256SUMS.
evidence: this entry

### C17-b - WAIT controller-up - still waiting at 15:50 (> 30 min, since 15:20); human told

### C17-b - WAIT controller-up - BLOCKED at 18:53: no controller-up on origin/lab/L1 (last L1 commit: C17-c ok, ~4 h ago). L2 steps C17-c and later wait for it. Firewall rule "MeshForge S2" still present (human asked to remove).

### C17-b - WAIT controller-up - OK
when: 2026-11-01 (local, after the block above)
result: origin/lab/L1 has SIGNAL: controller-up url=https://192.168.31.13:47110 fp=05:6B:7B:A3:...:45:72:45:85
SIGNAL: installed L2

---
# 2026-11-03 - after the break

### IP re-check - OK
when: 2026-11-03 15:04 (local)
result: Wi-Fi 192.168.31.155 (unchanged), JARVIS Private, 5 GHz, signal 96%. L2-ready unchanged, not re-emitted.
    Found: MeshForgeAgent service Running, C:\Program Files\MeshForge\current.txt = 0.17.0-1 (installed during the break). C:\ProgramData\MeshForge not readable without admin; join state unknown - asking the human.
    Leftover firewall rule "MeshForge S2" still present.

### C17-d - Check (part 1) - partial
when: 2026-11-03 15:20 (local)
commands:
    (human, Administrator) Remove-NetFirewallRule -DisplayName "MeshForge S2"; meshforge-agent identity show / policy show / --version; sc.exe qc / queryex MeshForgeAgent; current.txt; setup-result.json   -> saved to file
result: "MeshForge S2" rule removed (no MeshForge lab rules left).
    sc qc: AUTO_START (DELAYED), binPath "C:\Program Files\MeshForge\0.17.0-1\python\python.exe" -I -m meshforge.agent.winservice host --config C:\ProgramData\MeshForge\config\config.toml ..., runs as NT SERVICE\MeshForgeAgent.
    sc queryex: RUNNING, PID 25472. current.txt 0.17.0-1. setup-result.json: outcome ok, version 0.17.0-1, node_id null (at install time, before any join).
    meshforge-agent: "not recognized" in the admin window (that window was probably opened before the installer added C:\Program Files\MeshForge\bin to PATH). Retrying with the full path.
evidence: lab/reports/raw/2026-11-03-V0.17-install/L2-checks.txt

### C17-d - Check (part 2) - not joined yet
when: 2026-11-03 15:25 (local)
commands:
    (human, Administrator) "C:\Program Files\MeshForge\bin\meshforge-agent.cmd" identity show / policy show / --version
result: meshforge-agent 0.17.0 (git 3fca750858ea, python 3.12.14, bundle). node_id node_01M6GCKN7PYT2J297BT241P436, enrolled: False ("not joined to a fabric yet").
    policy (defaults): accept_jobs False, accept_gpu_sessions False, allow_unsandboxed False, allow_network False, max_cpu_jobs 1.
    The join (C17-c console part) is still to do: waits for L1's invite code for L2.
evidence: lab/reports/raw/2026-11-03-V0.17-install/L2-checks2.txt

### C17 - ON HOLD (ANNOUNCE 2026-11-03 (2): network tests first, joining later)
result: join paused until ANNOUNCE says "joining may start". Next: S1 server again on L2 + clients L2->L3, L2->L4.

S1 client RUNNING L2-to-L3

### S1 L2 -> L3 (new L3 MOHIT, 0.2.5 matrix) - OK
when: 2026-11-03 15:22-15:27 (local)
laptop: L2 (Windows 11, AILESH, 192.168.31.155), Wi-Fi 5 GHz
commands:
    uv run --no-sync python lab/spikes/s1_network.py client --link wifi --server 192.168.31.71
result: exit 0. TCP x1 L3->L2 198.50 +- 3.58 Mbit/s; x4 L3->L2 273.50 +- 2.65; x1 L2->L3 207.75 +- 4.24; x4 L2->L3 228.53 +- 15.26.
    UDP echo RTT mean 3.492 ms, p95 4.663 ms, jitter 0.621 ms, loss 0.100 +- 0.224 %.
evidence: lab/reports/raw/2026-11-03-S1-network/ailesh-1ad242f0__to-mohit_wifi-930c4d45.json
SIGNAL: s1-client-done L2-to-L3

S1 client RUNNING L2-to-L4

### S1 L2 -> L4 (LAPTOP-7ORVM30G, 0.2.5 matrix) - OK
when: 2026-11-03 15:27-15:32 (local)
laptop: L2 (Windows 11, AILESH, 192.168.31.155), Wi-Fi 5 GHz
commands:
    uv run --no-sync python lab/spikes/s1_network.py client --link wifi --server 192.168.31.244
result: exit 0. TCP x1 L4->L2 248.26 +- 4.17 Mbit/s; x4 L4->L2 361.43 +- 3.82; x1 L2->L4 247.46 +- 5.08; x4 L2->L4 253.03 +- 53.18 (high spread).
    UDP echo RTT mean 2.750 ms, p95 4.087 ms, jitter 0.664 ms, loss 0 %.
evidence: lab/reports/raw/2026-11-03-S1-network/ailesh-1ad242f0__to-laptop-7orvm30g_wifi-305efdfb.json
SIGNAL: s1-client-done L2-to-L4
    Both L2 client runs of the matrix are done. L2's own S1 server (ANNOUNCE (2)) is not started: it needs the human to re-add the 47201 firewall rules; no pair in the table needs another laptop to run a client against L2 (L1-L2 done 2026-11-01).
