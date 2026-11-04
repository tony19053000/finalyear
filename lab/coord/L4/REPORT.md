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

### C0-b - HUMAN BATCH - OK
result: sleep on AC = 0 (off), Windows Update paused until 2026-11-13, clock resynced (Leap 0, last sync 15:08:21, time.windows.com).

### CU-0 - S1 firewall - OK
result: "MeshForge S1 TCP" / "MeshForge S1 UDP" inbound 47201 from 192.168.31.0/24, Private, enabled (added by human, Administrator).

### CU-b - S1 server (0.2.5) - RUNNING
commands:
    uv run python lab/spikes/s1_network.py serve --bind 192.168.31.244 --idle 3600 --duration 14400
SIGNAL: s1-serve-up L4 ip=192.168.31.244

### C17-a - Download + check - OK
commands:
    gh release download v0.17.0 --repo tony19053000/finalyear --pattern "MeshForge-Agent-Setup-*" --pattern SHA256SUMS --dir $HOME\mf-pkgs; Get-FileHash
result: MeshForge-Agent-Setup-0.17.0-1.exe (57,488,826 bytes) sha256 3e95eb45...183753d matches SHA256SUMS.

### C17-b - controller-up - OK
result: origin/lab/L1 has SIGNAL: controller-up url=https://192.168.31.13:47110. Invite code to be asked from L1's human.

### C17-c..e - install/join - ON HOLD
result: ANNOUNCE 2026-11-03 (2): installs/joins held until the Lead posts "joining may start". Installer is downloaded and verified (C17-a).

### CU-b - S1 server - still RUNNING (HEAD b46ddab0)
result: serving on 192.168.31.244:47201, waiting for client runs L1->L4, L2->L4, L3->L4 (L4 runs no client per the ANNOUNCE table).

### CU-b - S1 server (0.2.5) - OK
result: served the three pairs: origin/lab/L1 s1-client-done L1-to-L4, origin/lab/L2 s1-client-done L2-to-L4,
    origin/lab/L3 s1-client-done L3-to-L4. Server stopped (killed by agent after the last pair). Server writes no JSON; numbers are in the clients' raw files.
    Firewall rules "MeshForge S1 TCP/UDP": removal asked of the human.

### CU-b - firewall cleanup - OK
result: human removed "MeshForge S1 TCP/UDP"; Get-NetFirewallRule "MeshForge S1*" count = 0.

### CU-c - GPU stack (0.2.3) - SKIPPED (for now)
result: torch not installed; the ~2.5 GB CUDA torch install needs the human's yes (asked, no answer yet). Docker absent.

### CU-d - raw files pushed - OK
result: inventory, S6, S7 (3 runs), network facts are in lab/reports/. S1: L4 was server only (no client JSON on L4).
SIGNAL: v02-raw-pushed L4
SIGNAL: s1-matrix-done L4

### C17-c - install (no join) - OK
when: 2026-11-03 (local)
laptop: L4 (Windows 11, LAPTOP-7ORVM30G) - not a timed run (L3 is Windows and does the timed criterion-1 run)
commands:
    HUMAN: MeshForge-Agent-Setup-0.17.0-1.exe (SmartScreen Run anyway, UAC Yes, Finish). Join NOT done (Lead: wait).
    sc.exe queryex MeshForgeAgent; type "C:\Program Files\MeshForge\current.txt"; Get-NetFirewallRule -DisplayName *MeshForge*
result: service MeshForgeAgent RUNNING (PID 1552), current.txt = 0.17.0-1, CLI at C:\Program Files\MeshForge\0.17.0-1\bin\meshforge-agent.cmd.
    Firewall: no rule named *MeshForge* found (non-admin query) - to recheck from an Administrator shell at C17-e.
    agent.log and `identity show` need Administrator: done at C17-e after joining.
SIGNAL: installed L4

### C17-c - join - OK
when: 2026-11-03 16:25 (local)
result: HUMAN joined in the console with L1's invite (code typed by human only). Done page: "This laptop is in the fabric fab_01M6BMCGQBHTN059R0R5MY3WYW".

### C17-d - owner settings - OK
result: name L4; Run jobs on; Let jobs use this laptop's GPU on; Allow jobs without a container on; rest default
    (Share GPU off, network off, pause benchmarks off, CPU 50 %, 1 job at a time).

### C17-e - Check - OK
commands (Administrator):
    meshforge-agent identity show; meshforge-agent policy show; Get-NetFirewallRule -DisplayName *MeshForge* | Get-NetFirewallPortFilter
result: enrolled True, fabric fab_01M6BMCGQBHTN059R0R5MY3WYW, cert valid to 2026-12-03, renewal due 2026-11-23;
    CA fingerprint 056b7ba3...45724585 = L1 controller-up fp. Service RUNNING, current.txt 0.17.0-1.
    policy: accept_jobs True, allow_unsandboxed True, allow_network False, max_cpu_percent 50, max_cpu_jobs 1,
    max_gpu_jobs one per GPU, accept_gpu_sessions False (that is "Share the GPU", left off), allowed_users everyone.
    Firewall: Get-NetFirewallRule -DisplayName *MeshForge* returned NO rules (Administrator) - installer opens no inbound port on L4.
BUG?: (non-blocking, for the Lead) no MeshForge firewall rule after the Windows install; fine if the agent only dials out to the controller.
SIGNAL: joined L4 node=node_01M6GGJP3FW5VHBDFKYQ6SF318

### C17-f (0.3.2) - agent unit tests on Windows - FAIL (1 test, non-blocking)
commands:
    uv sync --extra agent; uv run pytest tests/unit/agent -q -p no:cacheprovider
result: 1 failed, 3426 passed, 437 skipped in 385.87 s. Rerun of the single test: same failure (2 attempts total).
BUG: tests/unit/agent/test_v017_g1_system_cli.py::test_apply_returns_on_the_common_case fails on a Windows laptop that has
    the MeshForge agent INSTALLED. The test monkeypatches agent system_cli.system_install_present -> False, but
    src/meshforge/common/system_cli.py:327 `_apply_windows` -> :377 `say(decision.message)` still prints
    "meshforge-agent: note: MeshForge is installed on this computer; this development command uses YOUR user-mode state ...
    or set MESHFORGE_INSTALL_MODE=user to silence this." -> "Failed: must not print". The Windows branch detects the
    install by another route than the patched function. Reproduce: install the agent .exe, run that test.
    (Lead note read: no firewall rule after install is by design; recheck at V0.5 when peer serving is on.)

### LEAD CHECKPOINT V0.17a (L4)
SIGNAL: V0.17a-done

### C4.0-L4 - clock offset - OK (close to limit)
when: 2026-11-03 16:34 (local)
commands:
    w32tm /stripchart /computer:time.windows.com /samples:5 /dataonly   (resync /force done by human at 15:08)
result: offsets -173.9, -176.7, -179.9, -178.8, -167.8 ms (mean about -175 ms) vs time.windows.com. Under 200 ms but close; L1 compares against its own clock.
SIGNAL: clock-offset L4 ms=-175

### C4.15-g-L4 - part G (Windows) - OK
commands:
    uv sync --extra agent --extra controller --group dev
    uv run pytest tests/unit/common/test_bundle_case_insensitive.py tests/component/agent/test_docker_sandbox.py -q -rs -p no:cacheprovider
result: 3 passed, 9 skipped in 2.48 s. Docker tests SKIPPED (7, no Docker daemon); 2 GPU tests skipped (need --run-gpu).
SIGNAL: v04-g-done L4

### C17-e (recheck) - L4 connected to controller? - FAIL (BLOCKED)
when: 2026-11-03 16:46 (local)
commands:
    (Administrator) Get-Content C:\ProgramData\MeshForge\logs\agent.log -Tail 20; Get-NetTCPConnection -State Established | ? RemotePort -in 47101,47110
    Test-NetConnection 192.168.31.13 -Port 47101
result: agent NOT connected. Log loops DISCOVERING -> BACKOFF: "agent.discovery.none" service_type _meshforge-ctl._tcp.local. timeout 5 s,
    then "discovery_failed", reason "controller_not_found"; attempt 198 at 11:16:35Z. No established TCP to 47101/47110.
    identity: session_epoch 0 (L3 has 1 = has connected). TCP 192.168.31.13:47101 IS reachable from L4 (Test-NetConnection True).
BUG: on L4 (Windows 11, Wi-Fi JARVIS, Private) the installed agent never finds the controller via mDNS, although the join worked and
    the controller port is reachable. Possible cause: mDNS (UDP 5353) replies not reaching the service on this laptop.
    Possible workaround (Lead to decide, not done): set `[agent] controller_address = "192.168.31.13:47101"` in
    C:\ProgramData\MeshForge\config\config.toml (Administrator) + Restart-Service MeshForgeAgent.
    Blocks: every L4 job/phase (V0.3 C3.7 LIVE included).

### C17-e workaround (Lead note) - attempt 1 - FAIL (wrong port in the note)
when: 2026-11-03 17:03 (local)
commands (human, Administrator):
    C:\ProgramData\MeshForge\config\config.toml did NOT exist (only C:\ProgramData\MeshForge\policy.toml). Created it with exactly:
        [agent]
        controller_address = "192.168.31.13:47101"
    Restart-Service MeshForgeAgent; agent.log -Tail 5; identity show
result: agent now dials the controller (session_epoch 0 -> 5, CONNECTING -> HELLO_SENT), then
    stream_ended code UNIMPLEMENTED "Method not found!" -> BACKOFF. 47101 is the ENROLL port
    (src/meshforge/controller/settings.py: DEFAULT_GRPC_PORT = 47100, DEFAULT_ENROLL_PORT = 47101).
    Test-NetConnection 192.168.31.13:47100 = True. Attempt 2 uses 47100.
BUG (Lead note / ANNOUNCE): "gRPC 47101" should be 47100 for the agent session; 47101 is enrollment.
NOTE: this line pins L1's IP; if L1's IP changes, controller_address must be updated.

### C17-e workaround - attempt 2 - OK (L4 CONNECTED)
when: 2026-11-03 17:05 (local)
commands (human, Administrator): config.toml now exactly:
        [agent]
        controller_address = "192.168.31.13:47100"
    Restart-Service MeshForgeAgent; agent.log -Tail 5; Get-NetTCPConnection -State Established | ? RemotePort -eq 47100
result: agent.session.reconciled session sess_01M6GK1YGA4CQHQ6KKN8ACEV6Y first_connect true; scores scheduled; cache snapshot sent;
    gc command received from controller. Established TCP L4:12335 -> 192.168.31.13:47100. session_epoch >= 5.
NOTE: controller_address pins L1's IP 192.168.31.13 - if L1's IP changes, update this line and restart the service.
SIGNAL: connected L4 node=node_01M6GGJP3FW5VHBDFKYQ6SF318

### C3.7-a-L4 - Wi-Fi drop (LIVE, run by L1) - FAIL (1 of 6 reps, non-blocking)
result: per origin/lab/L1 REPORT: n=6, OFFLINE mean 5.00 s (all <= 8 s); ONLINE 14.52/8.21/8.46/7.85/8.13/6.96 s
    (rep 1 > 10 s, reps 2-6 within); agent restarted: no. L4 human toggled Wi-Fi on L1's cue.
evidence: on L1: lab/reports/2026-11-03-V0.3-fabric-watch.md

### C3.7-b-L4 - agent restart (LIVE) - WAITING
result: waiting for L1's cue. No new commit on origin/lab/L1 for 70 min, L2 2 h, L3 3 h (lab looks paused). Human told after 30 min.

### C3.7-b-L4 - agent restart keeps node_id (LIVE, run by L1) - OK
result: per origin/lab/L1: 6 reps, same node_id node_01M6GGJP3FW5VHBDFKYQ6SF318 every rep, restarted = yes;
    OFFLINE after stop 0.06-0.18 s; ONLINE after start 7.25/5.25/4.98/6.40/5.49/6.48 s. L4 human ran Stop-/Start-Service (Administrator).
    Service Running after the test.

### LEAD CHECKPOINT V0.3 (L4)
SIGNAL: V0.3-done

### LEAD CHECKPOINT V0.4 (L4)
result: L4's V0.4 parts (C4.0 clock-offset, C4.15-g part G) were done earlier.
SIGNAL: V0.4-done

### C5.11-a-L4 - peer port check - recorded (no rule yet)
commands:
    Get-NetFirewallRule | Where-Object DisplayName -like '*MeshForge*'
result: 0 rules. Lead note: the rule appears only when peer serving ([agent.peer] bind) is on; recheck when V0.5 turns peer serving on.

### C5.11-g-L4 - part H victim, prep (V0.5) - OK
when: 2026-11-04 (local)
laptop: L4 (LAPTOP-7ORVM30G, Wi-Fi 192.168.31.244 unchanged), HEAD 5bcbc814
commands (human, Administrator):
    Stop-Service MeshForgeAgent; Get-ChildItem C:\ProgramData\MeshForge
    Remove-Item -Recurse -Force C:\ProgramData\MeshForge\cas, C:\ProgramData\MeshForge\gc   (only these two, both existed)
    Start-Service MeshForgeAgent  -> Running
result: data dir had cache, cas, config, console, envs, gc, identity, logs, prepare, work, migrate-identity.lock, node_name,
    policy.toml, scores.json, scores.lock. Deleted: cas, gc. Nothing else touched. (Before this the human also did a few
    extra Stop/Start-Service cycles while practising the commands.) Free disk C: 231.4 GB (>= 12 GB).
SIGNAL: v05-h-ready L4 free=231

### C5.11-g-L4 - part H stop/start during the 5 GB download (LIVE) - done, waiting for L1's result
result: on L1's "STOP NOW" (human, Administrator): stop issued 02:26:21, stopped 02:26:22, Start-Service 02:26:28 (local, UTC+05:30).

### C5.11-g-L4 - part H result from L4's agent log - FAIL (c3 not shown: no chunk resume)
commands (human, Administrator):
    Select-String agent.log* for agent.cas.|resumed_chunks|lease|discovery|transfer_error, UTC 2026-11-03T20-23 + 11-04 -> 15 lines
result (UTC; local = UTC+05:30):
    20:52:18 agent.started (controller_address 192.168.31.13:47100, discovery false) - after the cas/gc wipe.
    J1 job_01M6HJZDCDN6G3TDFDPQJW1J2Q: code tree staged (2 blobs fetched, chunks_resumed 0); then
    20:55:33 agent.cas.source_dropped source controller, reason transfer_error, "chunk fetch failed: RemoteProtocolError" (warning)
    and agent.execution.status failed, failure_type transfer_error -> J1 FAILED ~48 s BEFORE the human's stop (20:56:21).
    20:56:32 agent.started again (the human's Stop/Start), ONLINE 20:56:34.
    J2 job_01M6HK8J40V20JA99WRWGT11JK: code tree blobs_hit 2, chunks_resumed 0; dataset ds5g@1 (mount data/ds)
    agent.cas.input_staged 21:05:58: blobs_hit 2, blobs_fetched 4, bytes_fetched 3,825,205,248 (no chunks_resumed field on this line);
    attempt released 21:06:10 (acked).
    -> Data already fetched by J1 was reused at BLOB level (2 dataset blobs hit), the rest (3.83 GB) fetched again.
    No line shows resumed_chunks/chunks_resumed > 0, so c3 (resume of a partly fetched chunk) is NOT demonstrated.
    Also the stop did not interrupt a running download: J1 had already failed with transfer_error.
BUG (for the Lead): (1) J1 5 GB fetch from the controller failed after ~2.5 min with "chunk fetch failed: RemoteProtocolError"
    (no stop yet); (2) the dataset input_staged line has no chunks_resumed field, so c3 cannot be read from it.
evidence: lab/reports/raw/2026-11-04-V0.5-data/L4-agent.txt (15 lines; fencing_token already [REDACTED] by the agent)

### C5.11-h-L4 - part I GC - SKIPPED
result: optional; not run (not cued by L1). Can be done on request.

### LEAD CHECKPOINT V0.5 (L4)
SIGNAL: V0.5-done

### C5.11-g-L4 - addendum after L1's summary
result: L1: J1 attempt 1 transfer_error 20:55:33Z (before the stop), attempt 2 node_lost (the stop); J2 succeeded 20:57:55-21:06:11Z.
    J2 values from L4's log (already saved, 15 lines covering 20:00-21:07Z): dataset ds5g@1 agent.cas.input_staged 21:05:58Z
    blobs_hit 2, blobs_fetched 4, bytes_fetched 3,825,205,248, NO resumed_chunks/chunks_resumed field; J2 code tree chunks_resumed 0.
    J2 resumed_chunks = not reported (0 / absent) -> c3 (> 0) NOT met. Only blob-level reuse (2 blobs) is shown.
    Part I: SKIPPED (not quick on Windows: service config edit + restart + restore).
SIGNAL: V0.5-done

### C6.13-a-L4 (prep) - peer serving on - PARTIAL (peer listens, firewall rule MISSING = BUG)
when: 2026-11-04 (local)
commands (human, Administrator):
    Add-Content config.toml; config.toml is now exactly:
        [agent]
        controller_address = "192.168.31.13:47100"

        [agent.peer]
        bind = "192.168.31.244"
    Restart-Service MeshForgeAgent; Get-NetFirewallRule -DisplayName *MeshForge*
result: service Running; peer server LISTENING on 192.168.31.244:47120 (Get-NetTCPConnection -State Listen).
    Firewall: Get-NetFirewallRule *MeshForge* (name or group) -> 0 rules after the restart.
BUG: Windows service did not create the inbound 47120 peer rule at start although [agent.peer] bind is set (ANNOUNCE 2026-11-04:
    "If the rule is still missing after that, THAT is a bug"). Inbound peer connections to L4 will likely be blocked by Windows Firewall.
    Not adding a rule myself (rules: no firewall changes by the agent); Lead to decide a manual rule.
NOTE: bind pins L4's Wi-Fi IP 192.168.31.244 (DHCP); update if it changes.
SIGNAL: peer-on L4 ip=192.168.31.244

### PAUSE - 2026-11-04
result: L1 confirms 47120 on L4 is blocked from L1 (no firewall rule). L1 paused the lab until tomorrow; waiting for the
    human/Lead decision on a manual Private 47120 rule. L4 state: joined + connected, peer serving on 192.168.31.244:47120,
    config.toml = controller_address 192.168.31.13:47100 + [agent.peer] bind 192.168.31.244. Next for L4: V0.6 (C6.13 c/d if the long job lands here).

### C6.13-a-L4 (prep) - peer firewall rule via firewall-sync - OK
when: 2026-11-05 (local), HEAD 8d79aa40
laptop: L4 (Wi-Fi 192.168.31.244 unchanged; config.toml unchanged: controller_address + [agent.peer] bind)
commands (human, Administrator):
    & "C:\Program Files\MeshForge\0.17.0-1\python\python.exe" -I -m meshforge.agent.winservice firewall-sync
    Get-NetFirewallRule -DisplayName *MeshForge* | Get-NetFirewallPortFilter
result: "peer firewall rule MeshForge-Agent-Peer: present (Private + Domain networks only)";
    rule MeshForge-Agent-Peer, profiles Domain+Private, Enabled True, TCP 47120. Console Checks page: (human to confirm).
NOTE: the earlier "BUG: no firewall rule" (C6.13-a prep, 2026-11-04) is UX gap F7, not a defect (ANNOUNCE 2026-11-05).
SIGNAL: peer-rule-ok L4

### C6.13-a-L4 - part C network matrix (run by L1) - OK (L2 part pending)
result: per origin/lab/L1, +15 min after firewall-sync, all 6 L1/L3/L4 cells measured: L1->L4 24.5 MB/s 4.6 ms,
    L4->L1 17.0 MB/s, L4->L3 2.7 MB/s (rtt 0.0 ms - odd, L1 flagged), L3->L4 24.4 MB/s. Peer port 47120 on L4 reachable now.

### C6.13-c-L4 - G1 Wi-Fi partition (LIVE) - done (L1: PARTIAL)
commands (human, Administrator): Disable-NetAdapter Wi-Fi; Start-Sleep 60; Enable-NetAdapter Wi-Fi (ANNOUNCE 2026-11-05 (3))
result: Wi-Fi off 14:59:17 IST (printed); "on" line not captured (session dropped) - ~15:00:17 by the 60 s sleep.
    L1: job_01M6JY167XY1FVXB1VTAD60AT1 lost; L4 back online 15:00:28; two controller.jobs.status_fenced at 15:00:26 (fencing OK);
    "attempt 2 elsewhere" not testable with a pinned job.
SIGNAL: v06-g1-wifi-done L4

### C6.13-d-L4 - G2 agent hard kill, round 2 (LIVE) - OK on L4's side (no orphan)
when: 2026-11-05 15:13 IST
job: job_01M6JYXKR18DH1QX6ANJA23K5C (main.py 300, on L4 since 15:10:53)
commands (human, Administrator):
    $a = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*meshforge.agent.winservice*child*" } | Select-Object -First 1; Stop-Process -Id $a.ProcessId -Force
    Start-Sleep 3; Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object CommandLine -like '*main.py 300*'
    Start-Sleep 10; (Get-Service MeshForgeAgent).Status
result: kill 15:13:45, agent child PID 11548 killed. Orphan check (3 s after): printed NOTHING -> no main.py 300 process left.
    Service MeshForgeAgent Running at 15:13:59 by itself (no Start-Service needed). Round 1 (job_01M6JYC715XDXRTJ18WFBH238D) did not count: finished before the cue.
SIGNAL: v06-g2-done L4 orphan=no

### LEAD CHECKPOINT V0.6 (L4)
result: L4 parts of V0.6: C6.13-a (peer rule + matrix via L1) OK, C6.13-c G1 done (L1: PARTIAL), C6.13-d G2 OK (orphan=no).
SIGNAL: V0.6-done

### C7.12-a-L4 - clock (V0.7) - OK
commands:
    [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
result: 1793785734 (local clock printed 2026-11-04 15:18:54).
SIGNAL: clock-unix L4 t=1793785734
