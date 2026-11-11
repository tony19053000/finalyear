# L3 REPORT (new laptop, replaces TIWARI_JI)


### R1-1 - Who am I - OK
when: 2026-11-03 14:12 (local)
laptop: L3 (Windows 11 Home Single Language 10.0.22631, hostname MOHIT)
commands:
    Get-CimInstance Win32_OperatingSystem / Win32_Processor / Win32_VideoController; netsh wlan show interfaces; Get-NetIPAddress; Get-NetConnectionProfile
result: CPU 12th Gen Intel Core i5-1235U, RAM 7.7 GB visible, GPU Intel UHD Graphics only (NO NVIDIA GPU). Wi-Fi IP 192.168.31.71, SSID JARVIS, 5 GHz, ch 157, 802.11ax, signal 95%, rx 600.5 Mbps.
  Network profile was **Public**; human asked to switch to Private (result in a later entry). Same room as L1: asked, answer pending.

### R1-2 - Tools - OK
when: 2026-11-03 14:12 (local)
laptop: L3 (MOHIT)
commands:
    git --version; uv --version; uv sync; uv run python --version; docker version
result: git 2.55.0.windows.3. uv was NOT installed: installed by the agent with `winget install astral-sh.uv --scope user` -> uv 0.13.7.
  `uv sync` attempt 1 FAILED: `error: Missing expected target directory for Python minor version link at ...\uv\python\cpython-3.12.16-windows-x86_64-none` (right after downloading cpython 3.12.16).
  attempt 2 (`uv python install 3.12` printed the same error, then `uv sync`) OK. `uv run python --version` -> Python 3.12.16. System Python 3.13.14 also present.
  Docker: client 29.9.2 installed (Docker Desktop, context desktop-linux) but daemon NOT running (`dockerDesktopLinuxEngine` pipe missing). Not started, not installed by the agent.
  gh CLI 2.103.3 installed earlier today (user scope), logged in.

### R1-3 - Inventory (0.2.2) - FAIL
when: 2026-11-03 14:12 (local)
laptop: L3 (MOHIT)
commands:
    uv run python lab/inventory.py --out "lab/reports/inventory/MOHIT.json"   (run twice)
result: exit 0 both times, but probes cpu, gpus, memory, network, os, power, torch, uv FAILED: `powershell CIM query failed: powershell: timed out after 50.7 s (run deadline reached)`; later probes `skipped: run deadline exceeded`. Secure boot: enabled. nvidia-smi: not found. Docker daemon not reachable.
evidence: lab/reports/inventory/MOHIT.attempt1.json, lab/reports/inventory/MOHIT.json
BUG: lab/_inventory CIM bundle hangs only when launched by the runner. The SAME script (pw.CIM_SCRIPT written to a file) runs in 3.8 s with `powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File cim.ps1`; a single `Get-CimInstance Win32_Processor` takes 1.4 s. So PowerShell/CIM is healthy; suspect the runner's launch (`-EncodedCommand` / new process group / stdin pipe) on this laptop. Reproduce: run the command above on MOHIT. Non-blocking for later steps.

### R1-4 - GPU check - SKIPPED
when: 2026-11-03 14:16 (local)
laptop: L3 (MOHIT)
result: no NVIDIA GPU (only Intel UHD Graphics); nvidia-smi not on PATH.

### R1-5 - Hardware scores (0.2.11) - BLOCKED
when: 2026-11-03 14:16 (local)
laptop: L3 (MOHIT)
commands:
    uv run python lab/spikes/s7_hardware.py run --quick --name L3 --skip-gpu
result: exit 1, traceback before any score: `RuntimeError: powershell CIM query failed: powershell: timed out after 60 s` (environment.snapshot -> probes.probe_power -> windows_cim). Full run not attempted (same code path).
BUG: root cause isolated on MOHIT: `powershell ... -EncodedCommand <pw.CIM_SCRIPT>` (6008 chars) HANGS (>40 s, killed), with or without CREATE_NO_WINDOW|CREATE_NEW_PROCESS_GROUP; the same script via `-File cim.ps1` returns rc 0 in 3.0-3.4 s (2296 bytes) under both flag sets; runner._env() is identical to os.environ. Likely Windows Defender/AMSI holding the encoded command. Suggested fix: write the script to a temp .ps1 and use `-File` (fallback when -EncodedCommand times out). Affects lab/_inventory (R1-3) and everything using windows_cim (S7, maybe S2 env).

### R1-1b - Wi-Fi profile + room - OK
when: 2026-11-03 14:28 (local)
laptop: L3 (MOHIT)
commands:
    Get-NetConnectionProfile
result: human switched JARVIS to Private -> `JARVIS Private` confirmed. Same room as L1: yes (human).

### R1-6 - Environments (0.2.10) - OK (docker cases skipped)
when: 2026-11-03 14:28 (local)
laptop: L3 (MOHIT)
commands:
    uv run python lab/spikes/s6_environments.py run --quick --name L3   (exit 3)
    uv run python lab/spikes/s6_environments.py run --name L3           (exit 3)
result: venv process start 0.85 s (within the <= 3 s target); venv create+install cold 5.09 +- 0.42 s, warm 1.13 +- 0.04 s (n=5).
  Docker cases skipped: quick run = `docker_daemon_unreachable` (Docker Desktop client present, engine not running); by the full run the docker CLI was no longer on PATH (`docker_cli_missing`; C:\Program Files\Docker absent). Treat L3 as **no Docker**. GPU cases n/a (no NVIDIA).
evidence: lab/reports/raw/2026-11-03-S6-environments/l3-842983de__quick.json, lab/reports/raw/2026-11-03-S6-environments/l3-842983de.json

### R1b - round 1b items - OK
when: 2026-11-03 14:28 (local)
laptop: L3 (MOHIT)
commands:
    uv sync --extra agent
result: exit 0. Wi-Fi Private: done (R1-1b). CUDA torch: not applicable (no NVIDIA GPU).

### R1-7 - Push - OK
result: round 1 pushed. Open: R1-5 BLOCKED by the -EncodedCommand hang (see R1-5 BUG); R1-3 inventory incomplete for the same reason.

### C0-a - P0 sync - OK
when: 2026-11-03 14:28 (local)
laptop: L3 (MOHIT)
commands:
    git fetch origin; git checkout lab/L3; git merge origin/main; git rev-parse --short HEAD; uv sync --extra agent
result: HEAD dc257850, uv sync --extra agent exit 0.

### C0-c - ready - OK
SIGNAL: L3-ready ip=192.168.31.71 os=windows host=MOHIT

### NL-1 - old files removed - OK
when: 2026-11-03 14:59 (local)
result: 9 TIWARI_JI files (2026-11-01) removed from lab/L3 after checking each exists on origin/lab/L3-old-TIWARI_JI. MOHIT files (2026-11-03) kept.

### CU-a - Network facts (0.2.4) - OK
when: 2026-11-03 14:59 (local)
laptop: L3 (MOHIT)
commands:
    netsh wlan show interfaces; Get-NetAdapter | ft Name,LinkSpeed; Get-NetConnectionProfile
result: Wi-Fi 5 GHz 802.11ax, Private, same room as L1. MAC/BSSID lines left out.
evidence: lab/reports/raw/2026-11-03-V0.2-network-facts/L3.txt

### C0-b - human batch - OK (clock: FAIL, non-blocking)
when: 2026-11-03 15:09 (local)
laptop: L3 (MOHIT)
result: sleep on AC = 0 (off), on charger (battery status 2), Wi-Fi profile Private, S1 firewall rules TCP/UDP 47201 Private enabled (CU-0). Windows Update pause: asked of human.
  `w32tm /query /status` -> `The service has not been started. (0x80070426)`: Windows Time service stopped. Matters for C4.0 clock offset later.

### CU-0 - S1 firewall - OK
result: `MeshForge S1 TCP True Private`, `MeshForge S1 UDP True Private`.

### CU-b - S1 server - running
when: 2026-11-03 15:09 (local)
commands:
    uv run python lab/spikes/s1_network.py serve --bind 192.168.31.71 --idle 3600 --duration 14400
result: listening on 192.168.31.71:47201. Waiting for L1, L2, L4 clients.
SIGNAL: s1-serve-up L3 ip=192.168.31.71

### C17-a - Download + check - OK
when: 2026-11-03 15:10 (local)
laptop: L3 (MOHIT)
commands:
    gh release download v0.17.0 --repo tony19053000/finalyear --pattern "MeshForge-Agent-Setup-*" --pattern SHA256SUMS --dir $HOME\mf-pkgs
    Get-FileHash vs SHA256SUMS
result: MeshForge-Agent-Setup-0.17.0-1.exe 54.8 MB, SHA256 matches SHA256SUMS. Done early while waiting for S1 clients.

### R1-3 (rerun after be71b7ac) - Inventory (0.2.2) - OK
when: 2026-11-03 15:32 (local)
laptop: L3 (MOHIT)
commands:
    uv run python lab/inventory.py --out "lab/reports/inventory/MOHIT.json"
result: exit 0, failed probes none, problems none. CPU i5-1235U (10 cores / 12 threads), RAM 7.7 GiB, GPU Intel UHD Graphics 1024 MiB (no NVIDIA), torch not installed, docker not installed, Wi-Fi 600.5 Mbit/s, secure boot enabled. The fix works on MOHIT.
evidence: lab/reports/inventory/MOHIT.json (attempt 1 kept as MOHIT.attempt1.json)
note: R1-5 (S7) rerun deferred until no S1 client is measuring against L3 (S7 loads the CPU).

S1 client RUNNING L3-to-L4 (2026-11-03 15:33)

### CU-L3L4 - S1 client L3 -> L4 (0.2.5) - OK
when: 2026-11-03 15:38 (local)
laptop: L3 (MOHIT) -> L4 (LAPTOP-7ORVM30G, 192.168.31.244), Wi-Fi 5 GHz, same room
commands:
    uv run python lab/spikes/s1_network.py client --link wifi --server 192.168.31.244
result: exit 0. TCP x1 L4->L3 215.9 +- 3.9 Mbit/s, L3->L4 192.1 +- 0.6; TCP x4 L4->L3 266.9 +- 38.8, L3->L4 262.9 +- 27.9; UDP RTT mean 3.54 ms, p95 4.84 ms, jitter 0.73 ms, loss 0 %. n=5 each.
evidence: lab/reports/raw/2026-11-03-S1-network/mohit-69e390d0__to-laptop-7orvm30g_wifi-305efdfb.json
SIGNAL: s1-client-done L3-to-L4

### CU-b - S1 server stopped - OK
when: 2026-11-03 15:40 (local)
result: all six pairs done (L1-to-L3, L2-to-L3, L3-to-L4 seen in reports). Server stopped by the agent (process killed; background task exit 255 = the kill). Firewall rules: HUMAN to remove.

### R1-5 (rerun after be71b7ac) - Hardware scores (0.2.11) - OK
when: 2026-11-03 15:40 (local)
laptop: L3 (MOHIT), on charger
commands:
    uv run python lab/spikes/s7_hardware.py run --quick --name L3 --skip-gpu   (exit 3)
    uv run python lab/spikes/s7_hardware.py run --name L3 --skip-gpu           (exit 3)
result: full run: cpu float64 1 thread 51.83 +- 1.42 GFLOP/s, all threads 103.80 +- 2.81 GFLOP/s; python 1t 27.89 +- 1.06 MFLOP/s, all 99.50 +- 3.92 MFLOP/s; memory copy 17.22, add 10.54, scale 9.58, triad 6.54 GB/s. No ON BATTERY / thermal warning; exit 3 only from the gpu_skipped warning. Quick run (smoke) python-all 165.47 MFLOP/s.
evidence: lab/reports/raw/2026-11-03-S7-hardware/l3-842983de__quick.json, lab/reports/raw/2026-11-03-S7-hardware/l3-842983de.json

### CU-d - S1 cleanup + raw files pushed - OK
when: 2026-11-03 15:48 (local)
laptop: L3 (MOHIT)
result: first removal attempt by the human did not take (2 rules still present); second attempt in Administrator PowerShell -> `Get-NetFirewallRule -DisplayName "MeshForge S1*"` none left. Raw files pushed: S1, S6, S7, inventory, network facts.
SIGNAL: v02-raw-pushed L3
SIGNAL: s1-matrix-done L3
SIGNAL: V0.2-done

### C17-c (part 1, install only - Lead: do NOT join yet) - OK
when: 2026-11-03 16:00 (local)
laptop: L3 (MOHIT)
commands:
    agent: Start-Process $HOME\mf-pkgs\MeshForge-Agent-Setup-0.17.0-1.exe   (launched 15:57:06; human clicked SmartScreen/UAC/wizard)
    sc.exe queryex MeshForgeAgent; type "C:\Program Files\MeshForge\current.txt"; Get-NetFirewallRule *MeshForge*
result: install dir created 15:57:30; service MeshForgeAgent RUNNING (PID 19208); current.txt = 0.17.0-1; bin dir `C:\Program Files\MeshForge\bin` on machine PATH (compute.cmd, meshforge-agent.cmd).
  Not a timed run (installer launched by the agent; Lead asked install only). Join NOT done.
  `meshforge-agent identity show` from a normal shell -> exit 4 "only administrators may change ... run it in an elevated prompt" (expected; admin check in C17-e).
  Firewall: `Get-NetFirewallRule` with MeshForge in name or group -> 0 rules after install. QUESTION: does the installer open the agent ports only at join, or is a rule missing? (CAMPAIGN Lead decision 7 asks to report which ports it opens: none so far.)
SIGNAL: installed L3

### C17-c (part 2, join) + C17-d + C17-e - OK (firewall: QUESTION)
when: 2026-11-03 16:30 (local)
laptop: L3 (MOHIT)
commands:
    HUMAN: console Start -> typed invite code -> Join -> owner settings (Run jobs on, GPU off, Allow jobs without a container on, name L3) -> Save -> Checks
    HUMAN (Administrator): "C:\Program Files\MeshForge\bin\meshforge-agent.cmd" identity show / policy show
result: enrolled True, node_id node_01M6GGZ7VNCSRKFAEDH3AX7QQY, fabric fab_01M6BMCGQBHTN059R0R5MY3WYW, session_epoch 1, cert valid to 2026-12-03, CA fingerprint 056b7ba3...45724585 = L1 controller-up fp.
  policy: accept_jobs True, accept_gpu_sessions False, allow_unsandboxed True, allow_network False, max_cpu_percent 50, max_cpu_jobs 1, pause_benchmarks False.
  Service MeshForgeAgent RUNNING.
  Note: an Administrator PowerShell opened before the install did not find `meshforge-agent` (PATH not refreshed in that window); the full path works.
  Firewall: `Get-NetFirewallRule -DisplayName *MeshForge*` -> NO rules, after install AND after join. QUESTION for Lead: is the peer/data port (47120) expected to be opened by the installer? Inbound peer traffic to L3 may be blocked (V0.5 C5.11-a).
SIGNAL: joined L3 node=node_01M6GGZ7VNCSRKFAEDH3AX7QQY
SIGNAL: V0.17a-done

### C3.2-a - agent unit tests on Windows (0.3.2) - FAIL (1 test, non-blocking)
when: 2026-11-03 16:37 (local)
laptop: L3 (MOHIT), MeshForge 0.17.0-1 installed + joined
commands:
    uv sync --extra agent; uv run pytest tests/unit/agent -q -p no:cacheprovider
result: 1 failed, 3426 passed, 437 skipped in 348 s. Symlink tests now skip (Lead fix present).
BUG: tests/unit/agent/test_v017_g1_system_cli.py::test_apply_returns_on_the_common_case fails only because MeshForge is INSTALLED on this laptop: the test monkeypatches `system_install_present` -> False, but on Windows `_apply_windows` (src/meshforge/common/system_cli.py:377) still prints `meshforge-agent: note: MeshForge is installed on this computer; this development command uses YOUR user-mode state ...` -> `Failed: must not print`. The Windows path detects the install some other way than the patched function. Reproduce: run that one test on any Windows laptop with the agent installed. Test isolation bug (or the Windows check ignores system_install_present).
SIGNAL: V0.3-pre (C3.7 LIVE waits for L1's cue)

### C3.7-b (partial) - agent restart (HUMAN, Administrator) - noted
when: 2026-11-04 00:45 (local)
result: human ran Restart-Service MeshForgeAgent (agent's own non-admin attempt was refused: "Cannot open MeshForgeAgent service"); service PID 19208 -> 10152, Running. Later Stop-Service / Start-Service by the human; now Running. Not a counted x5 C3.7 run (no L1 cue recorded).

### C4.0 / C4.15-a - clock offset - FAIL (attempt 1)
when: 2026-11-04 00:45 (local)
laptop: L3 (MOHIT)
commands:
    w32tm /stripchart /computer:time.windows.com /samples:3 /dataonly
result: offsets +618.3, +612.2, +621.4 ms (mean ~ +617 ms) > 200 ms limit. Windows Time service (w32time) is stopped (C0-b: 0x80070426). HUMAN asked to start it and resync; will re-measure.

### C4.15-g - part G Windows checks - OK
when: 2026-11-04 00:46 (local)
laptop: L3 (MOHIT)
commands:
    uv sync --extra agent --extra controller --group dev; uv run pytest tests/unit/common/test_bundle_case_insensitive.py tests/component/agent/test_docker_sandbox.py -q -rs
result: 3 passed, 9 skipped, 0 failed. Docker tests SKIPPED (7: no Docker on L3), GPU tests SKIPPED (2: no NVIDIA). Case-insensitive bundle tests passed.
SIGNAL: v04-g-done L3

### C4.0 / C4.15-a - clock offset - FAIL (attempts 2 + 3)
when: 2026-11-04 01:13 (local)
laptop: L3 (MOHIT)
commands:
    HUMAN (Administrator): w32tm /unregister; w32tm /register; Start-Service w32time; w32tm /config /manualpeerlist:time.windows.com /syncfromflags:manual /reliable:yes /update; w32tm /resync /force
    w32tm /query /status; w32tm /stripchart /computer:time.windows.com /samples:5 /dataonly   (01:08 and 01:13)
result: w32time now Running, `Last Successful Sync Time 01:07:53`, source time.windows.com, stratum 5. But offset NOT corrected: 01:08 +644..+648 ms, 01:13 +648..+656 ms (not converging). Still > 200 ms.
  (first Set-Service/Start-Service attempt: "The computer did not resync because the time service was shutting down" - trigger-start service stopped itself.)
SIGNAL: clock-offset L3 ms=+650 (FAIL > 200; fixing)

### C4.0 / C4.15-a - clock offset - OK (attempt 4)
when: 2026-11-04 01:36 (local)
laptop: L3 (MOHIT)
commands:
    HUMAN (Administrator): Set-ItemProperty HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\Config MaxAllowedPhaseOffset 0; w32tm /config /update; w32tm /resync /force
      (printed "did not resync because no time data was available"; the service synced on its own seconds later)
    w32tm /query /status; w32tm /stripchart /computer:time.windows.com /samples:3 /dataonly
result: Last Successful Sync 01:36:32 from time.windows.com; offsets +8.6, -3.0, +5.2 ms (mean ~ +4 ms) vs time.windows.com. Within 200 ms. Changed setting: W32Time MaxAllowedPhaseOffset = 0 (lets w32time step the clock), w32time startup Automatic, manual peer time.windows.com. Replaces the earlier FAIL value.
SIGNAL: clock-offset L3 ms=4
SIGNAL: V0.4-done

### F5 workaround - agent restart after Run jobs ON - OK
when: 2026-11-04 01:37 (local)
result: Run jobs was ON at join; the human restarted MeshForgeAgent after that (Restart-Service, then Stop/Start; PID 19208 -> 10152 -> current), service Running. Covers ANNOUNCE (5) for L3; L1 may rerun C17-k for L3.

### C5.11-a - peer port check - FAIL (non-blocking)
when: 2026-11-04 01:37 (local)
commands:
    Get-NetFirewallRule | Where-Object DisplayName -like '*MeshForge*'
result: no MeshForge firewall rules at all (same as after install/join). Peer port 47120 has no inbound rule on L3.
QUESTIONS: should the v0.17.0 installer create the 47120 inbound rule? If yes this is an installer BUG on MOHIT.

### C5.11-g/h - part H victim / part I GC - SKIPPED
result: L1: L4 is joined, so L4 is the part H victim. Not run on L3.

### C5.11-f (prep) - free disk - OK
when: 2026-11-04 02:00 (local)
commands:
    (Get-PSDrive C).Free
result: C: free 174.5 GB (need >= 12 GB). Peer serving off on every agent (L1): controller fetch only; part G peer half SKIPPED.
SIGNAL: v05-f-ready L3 free=174.5

### C5.11-f - agent log during L1's 5 GB reader job - OK
when: 2026-11-04 02:15 (local)
laptop: L3 (MOHIT)
commands:
    HUMAN (Administrator): Select-String C:\ProgramData\MeshForge\logs\agent.log* -Pattern 'agent\.cas\.|lease|discovery|transfer_error' | Select -Last 400 > lab/reports/raw/2026-11-04-V0.5-data/L3-agent.txt
result: 171 lines. Attempt 1 (att_...PBET) failed at 20:32:56Z: `agent.cas.source_dropped` source controller, reason transfer_error, `chunk fetch failed: RemoteProtocolError` -> status failed/transfer_error. Retry attempt (att_...X6F): `agent.cas.input_staged` ds5g@1 blobs_hit 2, blobs_fetched 4, bytes_fetched 4,219,469,824 at 20:38:54Z (from controller only; 2 blobs were already cached from attempt 1). Also 51 agent.discovery.none, 19 agent.discovery.found, 8 agent.started. Peer half SKIPPED (peer serving off).
  Secrets check: only `fencing_token: [REDACTED]` (agent already redacts it).
  Note: agent.log is readable only by Administrators (non-admin Get-Content: Access is denied).
BUG?: one RemoteProtocolError mid-transfer from the controller on Wi-Fi; recovered by the retry. For Lead to judge.
evidence: lab/reports/raw/2026-11-04-V0.5-data/L3-agent.txt
SIGNAL: V0.5-done

### C6-peer - peer serving on (V0.6 part C) - FAIL (firewall rule missing)
when: 2026-11-04 03:25 (local)
laptop: L3 (MOHIT), Wi-Fi IP 192.168.31.71
commands:
    HUMAN (Administrator): appended to C:\ProgramData\MeshForge\config\config.toml (file had no other lines):
        [agent.peer]
        bind = "192.168.31.71"
    Restart-Service MeshForgeAgent; Get-Service MeshForgeAgent
    Get-NetFirewallRule -DisplayName *MeshForge*
    Get-NetTCPConnection -LocalPort 47120 -State Listen
    HUMAN (Administrator): Get-NetFirewallPortFilter | Where-Object LocalPort -eq 47120 | Get-NetFirewallRule; Select-String agent.log -Pattern firewall
result: service Running; peer server LISTENING on 192.168.31.71:47120 (pid 19308). But NO firewall rule: no rule named *MeshForge*, no rule for local port 47120 at all, and agent.log has NO line containing "firewall" (sync_firewall left no trace).
BUG: Windows service did not create the inbound 47120 rule at start with `[agent.peer] bind` set (ANNOUNCE 2026-11-04 says `sync_firewall` does it at service start). With the Private-profile Windows firewall on, other laptops' peer fetches from L3 will likely be blocked. Reproduce: MOHIT, v0.17.0-1, config above, Restart-Service MeshForgeAgent. Not added by hand (rules: no firewall changes without Lead).
SIGNAL: peer-on L3 ip=192.168.31.71 (listening; firewall rule MISSING)

### C6-peer-fw - peer firewall rule via firewall-sync (ANNOUNCE 2026-11-05) - OK
when: 2026-11-04 14:21 (local)
laptop: L3 (MOHIT), Wi-Fi IP 192.168.31.71
commands:
    HUMAN (Administrator): & "C:\Program Files\MeshForge\0.17.0-1\python\python.exe" -I -m meshforge.agent.winservice firewall-sync
    Get-NetFirewallRule -DisplayName *MeshForge* | Get-NetFirewallPortFilter
result: `peer firewall rule MeshForge-Agent-Peer: present (Private + Domain networks only)`; rule `MeshForge-Agent-Peer | Domain, Private | True | TCP 47120`.
  Earlier "BUG: firewall rule missing" (C6-peer) is re-classified per Lead: **UX gap F7, not a defect** (rule is made by the admin command, D389).
  Console Checks page: asked of the human (next entry).

### C6-peer-ok - peer rule ok - OK
when: 2026-11-04 14:26 (local)
result: peer rule MeshForge-Agent-Peer TCP 47120 Private/Domain present, agent listening on 192.168.31.71:47120. Console Checks page: not reported by the human (human said emit the signal).
SIGNAL: peer-rule-ok L3

### C7.12-a - clock - OK
when: 2026-11-04 15:23 (local)
commands:
    [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
result: 1793785992 (L1 had 1793785751 earlier; w32time synced, offset ~4 ms)
SIGNAL: clock-unix L3 t=1793785992

### C7.12-c - part C invite refusals - PARTIAL (3 of 4 as expected; case 4 refused by checksum, not pin_mismatch)
when: 2026-11-04 15:36 (local)
laptop: L3 (MOHIT)
commands:
    HUMAN (Administrator): powershell -ExecutionPolicy Bypass -File <scratchpad>\v07c.ps1
      (script reads the 4 codes from origin/lab/L1 REPORT `v07-c-go2 L3`, runs `meshforge-agent join --force <code>` then `identity show` for each; codes masked in all output, never saved)
result: (1) 15:35:19 `token_used`: "this join code was already used; ..." - OK
  (2) 15:35:22 `token_revoked`: "this join code was revoked; ask for a new one" - OK
  (3) 15:35:24 `token_expired`: "this join code has expired; ask for a new one" - OK
  (4) 15:35:26 expected `pin_mismatch`, got LOCAL refusal "this is not a valid code (a character is wrong); check it or ask for a new one" (no controller contact). - NOT the expected reason.
  After every case: node_id node_01M6GGZ7VNCSRKFAEDH3AX7QQY (unchanged), enrolled True; service Running.
evidence: lab/reports/raw/2026-11-04-V0.7-refusals-L3.txt (no codes in it)
BUG?/QUESTION for L1: the code has a checksum, so changing one character in the CA-pin group is caught before the pin check. To reach `pin_mismatch` the altered code needs a valid checksum (e.g. a code built with a different CA fingerprint), or the test needs another way. The refusal itself is correct and safe; only the tested path differs.
  Note: the human ran all 4 codes from L1's REPORT, where L1 had posted them (all dead/altered by design; L1 to revoke the 2 h pin invite).
SIGNAL: v07-c-done L3 pin=checksum-refusal

### C7.12-d - part R revoke victim - IN PROGRESS (step a NOT RUN)
when: 2026-11-04 15:45 (local)
laptop: L3 (MOHIT), old node node_01M6GGZ7VNCSRKFAEDH3AX7QQY
commands:
    HUMAN (Administrator): Select-String C:\ProgramData\MeshForge\logs\agent.log -Pattern node_revoked
    HUMAN: meshforge-agent join --force CODE1; identity reset --yes; join CODE2; Restart-Service MeshForgeAgent
result: agent log `agent.session.stream_ended` code PERMISSION_DENIED details "node_revoked: this node was revoked from the fabric; ... identity reset --yes, then join <code>" at **2026-11-04T10:10:53.722709Z = 15:40:53.72 IST**; state ONLINE -> STOPPING reason node_revoked at 10:10:53.723Z; `agent.revoked` (console supervisor) at 10:10:56.86Z. L1's revoke was at 15:40:54.47 IST (L1 clock) -> L3 log is 0.75 s EARLIER than L1's revoke stamp: clock offset between L1 and L3, not causality (L3 was ~4 ms off time.windows.com).
  (a) join --force with code 1: NOT RUN - the human typed the placeholder `CODE1` literally -> "a join code starts with 'mf1-'". Identity was then reset, so the `node_revoked` refusal can no longer be tested on this identity. **SKIPPED (operator error)**.
  (b) `identity reset --yes` -> "identity reset: C:\ProgramData\MeshForge\identity" OK. `join CODE2` -> placeholder again, "a join code starts with 'mf1-'": not joined yet. Waiting for a real new code from L1's human.
  Restart-Service MeshForgeAgent: done (no output).

PAUSED 2026-11-04 15:53 (human: pause) - at C7.12-d part R: L3 revoked + identity reset, NOT rejoined yet (needs a new code from L1's human; script ready).

STOPPED 2026-11-04 16:05 (human: stop) - at C7.12-d (V0.7 part R). L3 revoked, identity reset, NOT rejoined. Next: rejoin with a new L1 code, then EMIT SIGNAL: rejoined L3 node=<new id>.

### C7.12-d - part R rejoin as NEW node - OK (step a SKIPPED earlier)
when: 2026-11-04 17:13 (local), recorded 2026-11-10 16:59
laptop: L3 (MOHIT)
commands:
    HUMAN (Administrator): rejoin.ps1 = meshforge-agent join <code 2 typed by the human>; identity show; console Settings (Run jobs ON, GPU off, Allow jobs without a container ON, name L3, Save); Restart-Service MeshForgeAgent (F5); firewall-sync; policy show
result: `joined fabric fab_01M6BMCGQBHTN059R0R5MY3WYW via 192.168.31.13:47101`, **NEW node_id node_01M6K0TZRNPEGBRN166GP6S61N** (old node_01M6GGZ7VNCSRKFAEDH3AX7QQY), enrolled True, cert valid until 2026-12-04. Service Running after restart. Peer rule MeshForge-Agent-Peer present (Private + Domain). policy: accept_jobs True, accept_gpu_sessions False, allow_unsandboxed True.
  Note: the human pasted code 2 into the agent chat once before using it (single-use, consumed by this join).
SIGNAL: rejoined L3 node=node_01M6K0TZRNPEGBRN166GP6S61N

### G9-up-a - download v0.17.1 - OK
when: 2026-11-10 16:59 (local)
commands:
    gh release download v0.17.1 --pattern MeshForge-Agent-Setup-* --pattern SHA256SUMS; Get-FileHash
result: MeshForge-Agent-Setup-0.17.1-1.exe 54.9 MB, SHA256 matches. Upgrade install: HUMAN next.

### G9-up-b - upgrade to v0.17.1 - OK
when: 2026-11-10 17:17 (local)
laptop: L3 (MOHIT)
commands:
    HUMAN: MeshForge-Agent-Setup-0.17.1-1.exe (launched by the agent 16:59:54; SmartScreen/UAC/wizard by the human), no rejoin
    type "C:\Program Files\MeshForge\current.txt"; Get-Service MeshForgeAgent
    HUMAN (Administrator): identity show; type config.toml; agent.log tail (peer|error)
result: current.txt 0.17.1-1; service Running; SAME node_id node_01M6K0TZRNPEGBRN166GP6S61N (upgrade kept the identity). config.toml kept `[agent.peer] bind = "192.168.31.71"`; log `agent.peer.listening` bind 192.168.31.71 port 47120 tls true at 11:31:20Z; peer rule MeshForge-Agent-Peer still present. `agent.probe.done` ok to 3 nodes (rtt 594 / 266 / 31 ms, 17.9 / 1.6 / 1.3 MB/s). Hardware: 10 cores / 12 threads, 7.7 GiB, gpu_count 0 (no NVIDIA, expected).
  Note: a non-admin `Get-NetTCPConnection -LocalPort 47120` showed nothing after the upgrade while the log shows it listening (visibility only, not a fault).
  Workarounds to remove on L3: none (L3 had no controller_address / index_url lines).
  Re-run table: no L3-only rows; L3 is used by L1's rows (G9-B c no_gpu, G9-B g --exclude-node L3, G9-D mc on L3) - waiting for L1's cues.
SIGNAL: upgraded L3 version=0.17.1-1 node=node_01M6K0TZRNPEGBRN166GP6S61N

### G9-D L3 - why the agent session drops during long jobs (L1 chat 20261110-200239) - investigated
when: 2026-11-10 20:14 (local)
laptop: L3 (MOHIT), v0.17.1-1, node node_01M6K0TZRNPEGBRN166GP6S61N
commands:
    Get-CimInstance Win32_OperatingSystem; Get-Process (grouped by name); netsh wlan show interfaces; powercfg (Wi-Fi power save)
    HUMAN (Administrator): agent.log* lines 13:xx-14:xx UTC (session|lease|state|error|warning|memory|heartbeat|attempt) -> evidence file
result: **Wi-Fi is NOT the cause**: JARVIS 5 GHz ch 157, signal 93 %, rx/tx 600.5 Mbit/s, Wi-Fi power saving off (AC index 0).
  **RAM is very tight**: free 0.65 of 7.69 GiB, commit 17.6 / 20.2 GiB. Top users: ChatGPT app x21 1001 MB, claude x12 984 MB, svchost x92 637 MB, msedge x14 468 MB, Memory Compression 304 MB, Defender 281 MB, node x31 231 MB, codex x6 170 MB. Human asked to close ChatGPT/Codex/Edge (not yet done at this check).
  **Agent log (UTC; IST = +5:30):**
    13:33:48 attempt_accepted job_01M72TMDAVRB8SEQ50E753TJX9 -> 13:50:57 (17.2 min) `stream_ended` details `lease_expired` (code ABORTED) -> reconnected 13:51:14 (17 s) -> cancel, attempt_released 13:51:21.
    14:08:41 attempt_accepted job_01M72WM9FA382SMEGZ8Z14KRJZ -> 14:24:01 (15.3 min) `stream_ended` `lease_expired` (ABORTED, lived_s 1967) -> reconnected 14:24:18 -> cancel, released 14:24:28.
    14:39:53 attempt_accepted job_01M72YDCB1R6V8E3ST71SMVDDW -> 14:40:03 (10 s) `stream_ended` code INTERNAL "Internal error from Core" (lived_s 945) -> reconnected 14:40:12 -> cancel, released.
  The agent was alive and connected the whole time: `agent.session.command_received` every ~20 s right up to each drop, and it reconnected within 7-17 s. The drop is the CONTROLLER ending the stream with lease_expired while L3 was still receiving its commands.
BUG? (for Lead): lease expires after ~15-17 min of a 1-core CPU job on L3 although the session stays up; suggests the agent's lease renewals (agent -> controller) are delayed or not sent while the job runs on this 8 GB laptop under heavy memory pressure (1212 ms ping seen by L1), or a renewal bug for long single chunks (G9-D topic). Third drop (gRPC "Internal error from Core") is a different error. Retest after closing the big apps to separate RAM pressure from an agent bug.
evidence: lab/reports/raw/2026-11-10-G9-D-L3-agent.txt (219 lines, no codes/tokens)

### G9-D L3 - memory freed for the retry - OK
when: 2026-11-10 20:16 (local)
commands:
    Stop-Process ChatGPT, codex, msedge (human asked the agent to end them)
result: RAM free 0.72 -> 2.14 GiB, commit 17.3 -> 9.96 / 20.2 GiB. L3 ready for the G9-D retry.
SIGNAL: g9d-retry-ready L3 free=2.14

### G9-D L3 - memory freed again - OK
when: 2026-11-11 13:44 (local)
commands:
    Stop-Process ChatGPT, codex, msedge, chrome, BlueStacksServices (human: keep only what is required)
result: before: 6.93/7.69 GiB used (90 %), commit 17.9/20.2 (ChatGPT + Edge had been reopened). After: 5.39 GiB used (70 %), 2.30 GiB free, commit 9.87/20.2. Kept: Claude, MeshForgeAgent (Running), Windows. Remaining big user: Memory Compression ~1.2 GB (Windows) + svchost.
SIGNAL: g9d-retry-ready L3 free=2.30

### G9-D L3 retry - PASS (by L1)
result: L1 chat 20261110-204514: job_01M72YVARE8WHZ0AY8GY4ZEEXR succeeded on L3 (~27 min, 1 chunk, no lease loss) with the big apps closed (2.1 GiB free).

### G10-up-a - download v0.17.2 - OK
when: 2026-11-11 13:51 (local)
result: MeshForge-Agent-Setup-0.17.2-1.exe 54.9 MB, SHA256 matches SHA256SUMS. Upgrade install: HUMAN next.

### G10-up-b - upgrade to v0.17.2 - OK
when: 2026-11-11 14:10 (local)
result: installer launched 13:51:43 (human clicked through); current.txt 0.17.2-1; service Running; no rejoin.
SIGNAL: upgraded L3 version=0.17.2-1

### G10 L3 row - G9-D with the big apps open - READY
when: 2026-11-11 14:10 (local)
result: low-RAM condition on purpose: RAM 7.08/7.69 GiB used (92 %), free 0.72 GiB, commit 15.1/20.2; open: Edge x20 1.2 GB, ChatGPT x19 0.9 GB, Claude 1.0 GB, codex. Waiting for L1 to run G9-D on L3.
SIGNAL: g10-g9d-ready L3 free=0.72
