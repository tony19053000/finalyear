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

PAUSED 2026-11-04 15:29 (local) - auto-watch loop cancelled; L4 idle in V0.7 (after C7.12-a), waiting for L3 part C.

## C8.10-d (V0.8 part E1/E2, from chat 20261105-002615-L1-to-L4) - 2026-11-05
- E1: Wi-Fi disabled ~00:31:58, re-enabled 00:32:38 (Get-Date after Enable-NetAdapter; 40 s off).
- E2: 8 busy-loop jobs (60 s each) started 00:35:50 (PSBeginTime 00:35:51-52), end ~00:36:52.
- L1 side observations (offline/online/CPU sparkline) to be recorded by L1.
SIGNAL: v08-e-done L4

## C8.10-d E2 rerun (chat 20261105-004015-L1-to-L4) - 2026-11-05
- 8 busy-loop jobs (60 s) started 00:41:18, load ended ~00:42:20; jobs removed 00:44:28.
SIGNAL: v08-e2-done L4

## G9 upgrade to v0.17.1 (chat 20261106-120000 / 20261110-165039-LEAD-to-ALL) - 2026-11-10
- nvidia-smi: RTX 3050 Laptop 4 GB, driver 592.27, CUDA 13.1 (answers L1 GPU-probe msg 20261105-134835).
- L1 msgs 20261105-134913 / -234437 (index_url workaround) superseded by Lead v0.17.1 note; index_url never applied on L4.
- Installer MeshForge-Agent-Setup-0.17.1-1.exe from release v0.17.1, SHA256 matches SHA256SUMS (dc11aeb2...5b64). Human ran it.
- current.txt = 0.17.1-1; service MeshForgeAgent Running from 0.17.1-1.
- config.toml rewritten (Admin): controller_address REMOVED; added [agent.envs] extra_index_urls = ["https://download.pytorch.org/whl/cu126"]; kept [agent.peer] bind. Restart-Service OK.
- Without controller_address: TCP Established 192.168.31.244 -> 192.168.31.13:47100 (F4 discovery fix works on L4).
- Note: first two Set-Content attempts failed "Access is denied" (window not elevated); third in Admin window OK.

## G9-C (V0.8 E2 + doctor cpu, v0.17.1) - 2026-11-10 - PASS
- Admin PowerShell. Load (8 busy jobs, 60 s) started 17:21:13; `meshforge-agent doctor cpu --seconds 60` started right after; ended 17:22:25.
- doctor header: threads 12, owner max_cpu_percent 50% (live), enforced by Windows Job Object CPU-rate cap.
- New `cpu` column during load (t=1..49 s): 72-100% (mostly 93-100%); after load ends (t=50..60 s): 34-54%.
- Old meter `old` column: mostly 0.0% with random 100% spikes (e.g. t=7-9 s 100, t=10-14 s 0) - the old bug, as expected.
- Result: `cpu` column rises and tracks load; `old` does not. PASS.
SIGNAL: g9-c-done L4

## Stuck chunk 68, job job_01M73B13999W9AWDT89F7KDX2H (chat 20261111-001509-L1-to-L4) - 2026-11-11 00:20
- Look only, nothing killed. Times IST (log is UTC; +5:30).
- Chunk 67 OUTCOME_DONE 00:04:01 -> chunk_grant (chunk 68) 00:04:01 -> env cache_hit + env_ready 00:04:03 (venv 8e2ebada...).
- Processes: agent host 5236 + child 15084 (0.17.1, since 17:20). Chunk: 13032 = venv Scripts\python.exe run_chunk.py ...att_01M73B13AC748DSBP3XDFBTAFM.chunk\in\items.json 68 ...\out (started 00:04:03, CPU 0.02 s, 4 MB) -> 19640 = "C:\Program Files\MeshForge\0.17.1-1\python\python.exe" run_chunk.py ... 68 (started 00:04:20 = 17 s after launcher; CPU 1.6 s total, 431 MB, 12 threads ALL Wait/UserRequest, no running thread).
- nvidia-smi: no GPU compute process, 0 MiB used -> chunk never reached CUDA.
- agent.log after env_ready: no run_finished / no output line; only refresh_peer_tokens every 20 s + gc.pass (00:10, 00:20). Agent healthy, chunk process idle-blocked.
- Earlier chunks 60/62/65/67 each ran ~24-25 s, exit 0.
- Reading: worker hung early in startup (import / CUDA-torch init or a lock), not a crash, not CPU-bound. Possible: run_chunk.py waits on something (stdin? file lock? torch import deadlock).
- Q4 (screen/lid/other apps at 00:04): asked human, pending.

## Chunk-68 hang follow-up / BUG-4 (chat 20261111-002353 + -002541) - 2026-11-11 00:28
1. pids 19640/13032: already GONE at 00:26:21 (before any kill) -> not leaked; nothing killed.
2. Q4 (screen/lid/power/apps at 00:04): asked human, answer pending.
3. nvidia-smi 592.27, CUDA 13.1. venv: Python 3.12.14, torch 2.14.0+cu126, cuda 12.6, is_available True.
4. Repro: 30/30 runs of `import torch; torch.zeros(1,device='cuda')` in the same venv (Admin) -> all ok, 2-3 s each, no hang. Did NOT reproduce outside MeshForge.
5. Event Viewer System 00:00-00:12 IST: no events at all (no nvlddmkm/Display/TDR, no WU).
Note: in the hang, the 17 s gap between launcher (00:04:03) and worker (00:04:20) is unusual vs ~1 s normally.

## G10 upgrade to v0.17.2 + BUG-4 b setup (chat 20261111-092512/134456 LEAD, 135422/141924/142058 L1) - 2026-11-11
- MeshForge-Agent-Setup-0.17.2-1.exe from release v0.17.2, SHA256 f5332d55...93c7 matches SHA256SUMS. Human installed; current.txt = 0.17.2-1.
- BUG-4 b: added [agent.split] stall_floor_s = 60 to config.toml (Admin), Restart-Service 14:58:42, service Running. (Must be removed after the test.)
SIGNAL: upgraded L4 0.17.2

## BUG-4 b result + cleanup (chat 20261111-150144/150245/150558 L1) - 2026-11-11
- BUG-4 b job_01M74Z7T6JD6KVJ85B7CAQ9CVB: agent.log `agent.lane.chunk_stalled` warning at 09:33:58Z (15:03:58 IST): chunk 1, threshold_s 60.0, stalled_s 60.2, pid 17960, "reported to the controller with the lease". (job_01M74Z5VRP... first run: no chunk_stalled - copied at queue-empty 29 s, as L1 said.)
- Get-Process python not taken during the job (missed). After: only 15324/18248 = agent host+child started 18:22:18/19 (restart) -> no sleeping python.exe left.
- stall_floor_s + [agent.split] REMOVED, Restart-Service 18:22:19. config now: [agent.peer] bind, [agent.envs] extra_index_urls.

## G11 perf (1) Windows chunk start (chat 20261111-163146-LEAD-to-L4) - 2026-11-11 18:22
- Log, job_01M74WKXPN8V7XM1N9HEBYNZ6N on L4: only one lane line found: chunk 2 OUTCOME_DONE at 08:47:47Z (no chunk_started/run_finished lines with that job id in agent.log*; may have rotated).
- Same venv (8e2ebada..., torch 2.14.0+cu126), Admin shell, agent idle:
  - `import torch; torch.cuda.init()` run 1: 13.18 s (cold), run 2: 3.42 s
  - `import torch` only: 2.16 s
- Defender RealTimeProtectionEnabled: True (read only, not changed).
- Reading: warm start ~3.4 s, cold first start ~13 s (likely Defender scanning torch DLLs on first load). Neither explains 45-50 s per chunk alone -> rest is probably env check / staging / per-chunk process start; need chunk_* timings from a fresh run.

## C5.11-g (chat 20261111-181402/181758/181944 L1)
- J1 job_01M75A5YJKS51FZ5W0JWW54SGA ended 18:19:26 before the stop; no restart done for it. Human ready at Admin PowerShell for the redo.

## C5.11-g PASS (L1) + wrap-up answers (chat 20261111-183000/185808 L1) - 2026-11-11
1. L4 Claude session: WORKING. (My 18:23 chat 20261111-182343-L4-to-L1.md already had items 2+3 - please `read` it.)
2. stall_floor_s + [agent.split]: REMOVED, Restart-Service at 18:22:19 IST (before C5.11-g). config = [agent.peer] bind + [agent.envs] extra_index_urls only.
3. BUG-4 b: `agent.lane.chunk_stalled` 15:03:58 IST, job_01M74Z7T6JD6KVJ85B7CAQ9CVB chunk 1, threshold_s 60.0, stalled_s 60.2, pid 17960, "reported to the controller with the lease". No sleeping python.exe left: now only 6612/21076 = agent host+child (started 18:42:08 after the C5.11-g restart).
4. C5.11-g: agent stop 18:42:01 (scheduled one-liner), start ~18:42:06, Running.
   - J1 job_01M75BNHZWCKYJD160E4AJ7SZP: only agent.cas.staged (code tree) 18:40:01: blobs_hit 1, fetched 1, 389 B, chunks_resumed 0. No input_staged (interrupted by the stop).
   - J2 job_01M75BT6Y5AQER3AF64GS76WW4: cas.staged 18:42:33 (hit 2, 0 B); `agent.cas.input_staged` 18:45:32 ref ds5g@5 mount data/ds: blobs_hit 3, blobs_fetched 3, bytes_fetched 3137339392 (~3.1 GB), chunks_resumed 10. -> resume works (5 GB - 3.1 GB fetched ~= 2 GB reused).
BUG-4 a: L4 ready - submit it whenever you want; tell L4 and we will record Get-Process python during it.

## G10 BUG-4 a (chat 20261112-035614/044205/044246 L1) - 2026-11-12
Job job_01M76E3YHA1YT5157097JWD9Q6 (L4 only). Samples (IST):
- 04:45:05: only agent host 6612 + child 21076 (between chunks).
- 04:46:21: + ONE python.exe 28184, parent 21076 (agent child), started 04:46:15.
- 04:47:36: + ONE python.exe 23608, parent 21076, started 04:47:20 (28184 gone).
- 04:50:32 (Admin, full cmdline): ONE chunk process 12284, parent 21076:
  "C:\Program Files\MeshForge\0.17.1-1\python\python.exe" run_chunk.py ...att_01M76E3YJG580EBJH2B9X3TAEP.chunk\in\items.json 18 ...\out
- No venv Scripts\python.exe launcher, no py.exe at any sample. No chunk_stalled lines for job_01M76* in agent.log*. No hung chunk.
RESULT: BUG-4 a PASS (one python.exe per chunk, Program Files interpreter, parent = agent).
FINDING (minor, for LEAD): the chunk runs the **0.17.1-1** interpreter while the agent is 0.17.2-1. The cached venv 8e2ebada... was built under 0.17.1, so its base python points to C:\Program Files\MeshForge\0.17.1-1. If the old version folder is ever removed (uninstall/cleanup), this venv breaks; env hash does not include the interpreter version/path. Suggest: include base interpreter path/version in env_hash, or rebuild venvs after upgrade.
(First job job_01M76BG06PMZTQ5MHRBZT3JVSN: missed - ended before check.)
SIGNAL: bug4a-done L4

## v0.17.3 upgrade + G11 rows (chat 20261112-053604 L1, 123426/130602 LEAD) - 2026-11-12
- Missed L1 demo 03 standby (chat 20261112-053604, 05:36): nobody typed `read` until 13:08; demo long over.
- Upgrade: MeshForge-Agent-Setup-0.17.3-1.exe, SHA256 84a1c2cd...ac35 matches SHA256SUMS. Human installed. current.txt 0.17.3-1, MeshForgeAgent Running (host 27008 / child 12632 since 13:17:55), TCP Established to 192.168.31.13:47100.
- D457 Windows part 1 PASS: as non-admin user `Get-Content C:\ProgramData\MeshForge\public\node-id.json` -> {"agent_version": "0.17.3", "format": 1, "key_fingerprint_sha256": "f0a152a5...7740", "node_id": "node_01M6GGJP3FW5VHBDFKYQ6SF318", "written_at": "2026-11-12T07:47:58+00:00"} (node id matches).
- D457 part 2 (`compute run --local`) NOT RUN: non-admin `compute nodes` -> exit 4 "no fabric CA certificate to verify the controller with: run `compute login --fingerprint FP` ... on a laptop with an enrolled agent it is found automatically". The CA is NOT found automatically for a non-admin user on Windows (the system agent's CA is in the private ProgramData dir). Needs a human `compute login` (password typed by the human) - or L2 does this row.
- D458: pending - needs the first Mode 3 GPU job on L4 (venv rebuild ~2.5 GB first).
SIGNAL: upgraded L4 0.17.3

## v0.17.4 upgrade (chat 20261112-184744/225957 LEAD) - 2026-11-13
- MeshForge-Agent-Setup-0.17.4-1.exe, SHA256 1deb478b...9997 matches SHA256SUMS. Human installed.
- current.txt 0.17.4-1, MeshForgeAgent Running, TCP Established 192.168.31.13:47100.
- node-id.json (read as non-admin): agent_version 0.17.4, node_01M6GGJP3FW5VHBDFKYQ6SF318, written 06:49:08Z.
- Next: D458 (needs the first Mode 3 GPU chunk on L4 + admin log/pyvenv.cfg check), D457 part 2 (needs fabric fingerprint from L1 for `compute login`).
SIGNAL: upgraded L4 0.17.4

## 0.14.8 prep (chat 20261113-131320 L1) - 2026-11-13 13:23
- Plugged in; sleep on AC = Never (powercfg); big apps closed earlier; free RAM 6.1 GB. Power mode Best performance: set by human. Agent running 0.17.4.
- D457 part 2 / D458 checks (chat 20261113-130657) postponed until after 0.14.8 (human decision).
SIGNAL: ready L4 0.14.8

## 0.14.8 BIG-jobs campaign prep (chat 20261113-133358/134658 L1) - 2026-11-13 13:47
- First small campaign (13:38) cancelled by L1. Ready for big one: AC power, sleep Never, free RAM 6.2 GB, lid open.

## Campaign status (chat 20261113-135225..144216 L1) - 2026-11-13 14:54
- Big campaign camp_01M7A0BZQB5AN4TBW0CY7M4DZY started 13:58:45, CANCELLED by L1 at 15 runs. New plan Run 2B/2A/1 (L1 RUN_PLAN.md), not started; L4 waits for a start time.

## Run 1 prep (chat 20261113-152300 L1) - 2026-11-13 15:24
- Ready sent, free RAM 6.4 GB.

## 2026-11-13 17:52 IST - FINAL PLAN read (L1 17:49)
- Run 1 -> Part E -> Run 2B -> Run 2A. Check now: free RAM 5.8 GB, MeshForgeAgent Running, on AC power. Waiting for human checklist, then reply ready to L1.
- 17:53 sent READY (final plan) to L1, free RAM 5.8 GB.

## 2026-11-14 04:48 IST - read Run 1 / GPU quick test notices + LEAD Defender test
- Run 1 (CPU) started 18:12:54, ran overnight; GPU quick test running since 04:17 (~1.5 h, uses L4).
- LEAD small-files/Defender test: postponed until the GPU quick test ends (rule: only when no MeshForge job on L4). At 04:48 CPU 8%, no job process seen, but the test is still in progress.

## 2026-11-14 04:49 IST - LEAD Defender small-files test (done on human request)
time: 04:50   step: Run 1 follow-up (Windows image slowdown)   reply-needed: no
Ran 04:49:03-04:49:17 in %TEMP%\mftest with MeshForge bundled python 0.17.4-1 (-I); no MeshForge job process on L4 at the time (GPU quick test in progress elsewhere). Folder removed after.
run1:
write 5000 files 2.05s (0.4 ms/file)
read  5000 files 1st 4.90s (1.0 ms/file)  2nd 0.30s (0.1 ms/file)
one big file (same bytes) write+read 0.04s
run2:
write 5000 files 2.09s (0.4 ms/file)
read  5000 files 1st 4.50s (0.9 ms/file)  2nd 0.34s (0.1 ms/file)
one big file (same bytes) write+read 0.04s
Defender: RealTimeProtectionEnabled True, IoavProtectionEnabled True, AMServiceEnabled True
Reading: Defender costs ~1 ms per new file on first read (10x vs cached), but NOT ~50 ms/file. 20k files x 1 ms = ~20 s, so Defender alone does not explain 16-18 min. Cause is likely elsewhere (per-file work in the job, e.g. process/env startup or per-file network fetch).
- SIGNAL: defender-test-done L4

## 2026-11-14 05:03 IST - LEAD test 2: numpy int64 matmul
time: 05:03   step: Windows image slowdown   reply-needed: no
Run by human in admin PowerShell, every venv under C:\ProgramData\MeshForge\envs\venv that has numpy (newest first):
- venv 001f0edb...: numpy 2.5.3, Python 3.12.14 -> int64 matmul 0.182s  float64 matmul 0.026s  ratio 7x
- venv b1f64891...: numpy 2.5.4, Python 3.12.14 -> int64 matmul 0.186s  float64 matmul 0.017s  ratio 11x
Reading: int64 is 7-11x slower than float64, but only ~0.18 s per 32768x288 @ 288x32 matmul on L4. Compare with L1 Docker numbers to see if Windows numpy is the outlier.
- SIGNAL: numpy-test-done L4

## 2026-11-14 05:12 IST - LEAD test 3: CNN maths outside MeshForge
time: 05:12   step: Windows image slowdown   reply-needed: no
Human ran t3.py in admin PowerShell with venv 001f0edb... python (numpy 2.5.3), outside MeshForge, folder removed after:
- normal priority:       20000 images CNN: 51.6 s
- BELOW_NORMAL priority: 20000 images CNN: 50.7 s
Reading: the maths is fine on Windows (~51 s vs L1 real job ~45 s), and low priority makes no difference. So the 16-18 min comes from HOW MeshForge runs the job on Windows (data fetch/unpack, per-file staging, chunk overhead), not the CNN maths.
- SIGNAL: test3-done L4

## 2026-11-14 05:24 IST - read LEAD Windows slowdown test matrix (T1-T5). T1 given to human; T2-T5 driven by L1.
time: 05:27   step: Windows slowdown investigation   reply-needed: no
T1 (B) on L4, admin PowerShell, env stripped to PATH, SYSTEMROOT, TEMP, TMP + LANG=C.UTF-8, PYTHONUNBUFFERED=1, HOME=TEMP; venv 001f0edb... python -I t3.py, start 05:24:45:
- 20000 images CNN: 49.3 s
Reading: T1 is FAST (49 s, same as 51 s with the full env) -> the stripped environment (B) is NOT the cause. Ready for T2 (L1 submits t3 job pinned to L4); the L4 human will look at Task Manager during it.
- SIGNAL: t1-done L4
