# L2 REPORT (new laptop, replaces AILESH - old report: REPORT-old-AILESH.md)

### N1 - Who am I (new L2) - OK
when: 2026-11-15 17:24 (local)
laptop: L2 (Windows 11 Home Single Language build 26300, hostname LAPTOP-GGJ7F9EM, HP Victus 15-fa1xxx)
commands:
    Get-CimInstance Win32_Processor / Win32_ComputerSystem / Win32_OperatingSystem
    nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
    Get-PSDrive C
    netsh wlan show interfaces; Get-NetIPAddress -InterfaceAlias Wi-Fi; Get-NetConnectionProfile
    Test-Connection 192.168.31.13 -Count 4
result: CPU 12th Gen Intel Core i7-12650H (10 cores / 16 threads); RAM 15.6 GB; GPU NVIDIA GeForce RTX 3050 A Laptop GPU 4 GB (driver 592.82) + Intel UHD;
    disk C: 279.3 GB free of 475.8 GB. Wi-Fi IP 192.168.31.42, SSID JARVIS, 5 GHz ch 157, 802.11ax, signal 99%; ping L1 avg 47.5 ms (max 182 ms).
    Network profile: Public (needs Private - HUMAN Admin step pending).

### N2 - Prerequisites - OK
when: 2026-11-15 17:27 (local)
commands:
    Get-NetConnectionProfile; Test-Connection 192.168.31.13; nvidia-smi; Get-PSDrive C
    HUMAN (Admin): Set-NetConnectionProfile -InterfaceAlias "Wi-Fi" -NetworkCategory Private
result: Wi-Fi JARVIS now Private (was Public); ping L1 ok; RTX 3050 driver 592.82; 279 GB free.

### N3 - Agent install + join - OK
when: 2026-11-15 18:19 (local)
commands:
    gh release download v0.17.5 -p "*.exe" -p SHA256SUMS; Get-FileHash (sha256 matches SHA256SUMS)
    HUMAN (Admin): install MeshForge-Agent-Setup-0.17.5-1.exe, join with code from L1 (by voice)
result: service MeshForgeAgent Running; LEAD sees node_01M7FJY2AK0XVZZ9EDEV4CM0W7 online on 0.17.5, CPU score 508 GF; GPU column "-" (known display gap).
    Console check: Docker not installed (not needed - LEAD), GPU OK, CUDA 13.1 OK, disk 278 GiB, Firewall "could not be checked", peer sharing Off.

### N4 - Setup steps 1-4 (firewall, config, Defender, policy) - OK
when: 2026-11-15 18:46 (local)
commands: (HUMAN, Admin)
    python -I -m meshforge.agent.winservice firewall-sync; Get-NetFirewallRule -DisplayName "MeshForge*"
    create C:\ProgramData\MeshForge\config\config.toml with [agent.envs] extra_index_urls = [cu126]; Restart-Service MeshForgeAgent
    Add-MpPreference -ExclusionPath "C:\ProgramData\MeshForge"
    meshforge-agent policy show
result: peer rule removed (peer off), no MeshForge firewall rules; config created (none existed), service Running; exclusion listed;
    policy accept_jobs True, accept_gpu_sessions False, no ram/cpu/vram limits, unsandboxed True, max_cpu_jobs 1, max_gpu_jobs one per GPU.
QUESTIONS: accept_gpu_sessions False - asked L1/LEAD whether to turn it ON.

### N5 - Setup steps 5-7 (score, power, HighQoS) - OK
when: 2026-11-15 18:59 (local)
commands: (HUMAN, Admin)
    meshforge-agent hardware score   (cached, not --force)
    powercfg /change standby-timeout-ac 0; /change hibernate-timeout-ac 0; /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
    Select-String C:\ProgramData\MeshForge\logs\*.log qos_self
    (agent) NVML check with agent python; nvidia-smi
result: score score_01M7FJYPNTXCR15JBXR6ESD2QF (2026-11-15 12:29Z, on AC, load 11 %): cpu fp32 1t 95.1, all 508 GF, fp64 238 GF, mem triad 10.4 GB/s;
    GPU score unavailable (torch_not_installed in agent python - expected, ADR-0011). NVML in agent python sees RTX 3050 A Laptop GPU 4.0 GiB, driver 592.82.
    Power: Balanced plan, sleep AC Never, hibernate AC Never, plugged in; lid action set to 0 (could not read back - hidden setting).
    qos_self applied=true at 12:29:24Z and 12:55:18Z (after restart).

### N6 - Session restored + health check - OK
when: 2026-11-16 18:23 (local)
laptop: L2 (Windows 11, hostname LAPTOP-GGJ7F9EM)
commands:
    git clone / checkout lab/L2 / merge origin/main (repo now in $HOME\mf-lab-L2)
    Get-Service MeshForgeAgent; nvidia-smi; Win32_OperatingSystem free RAM; Get-PSDrive C; Win32_Battery
    HUMAN-approved UAC (Admin, read-only): Get-Content C:\ProgramData\MeshForge\config\config.toml; Get-MpPreference ExclusionPath;
        meshforge-agent policy show; Select-String qos_self in agent logs
result: previous lab session lost (laptop went offline); restored. Service Running. config.toml has [agent.envs] extra_index_urls = cu126.
    Defender exclusion C:\ProgramData\MeshForge listed. Policy: accept_jobs True, accept_gpu_sessions False, no RAM/CPU/VRAM limits,
    unsandboxed True, max_cpu_jobs 1, max_gpu_jobs one per GPU. qos_self applied=true (latest 2026-11-16T12:21:40Z).
    GPU RTX 3050 A Laptop 4 GB, driver 592.82, idle 44 C. RAM free 5.4 / 15.6 GB (close big apps before runs). Disk C: 272.7 GB free.
    Power: Win32_Battery status 1 (on battery?) - human asked to check charger.

### N7 - L3 Free RAM tool installed - OK
when: 2026-11-16 18:26 (local)
commands:
    script from lab/chat/20261116-173625-L3-to-L2.md saved as $HOME\mf-tools\Free-RAM.ps1 (unchanged); desktop shortcut "Free RAM.lnk" created
result: installed, no admin. Not run yet (it also closes the Claude app - human uses it before/during runs).

### R2A-1 - Run 2A stuck L2 lane - agent restarted - OK
when: 2026-11-18 03:29 (local)
commands:
    HUMAN-approved UAC (Admin): tail -80 C:\ProgramData\MeshForge\logs\agent.log; Restart-Service MeshForgeAgent; Get-Service MeshForgeAgent
result: MeshForgeAgent Running after restart (cue lab/chat/20261118-032647-L1-to-L2.md).
evidence: lab/coord/L2/evidence/run2a-stuck-chunk-agent.log
BUG: job_01M7NQBC371DG0E565FBW03W0A att_01M7NQBC7VVP94Z3YDNQ48VNF0: agent logged cancel_chunk -> agent.lane.chunk_cancel item 10313
    running=true reason=superseded, then no_work -> agent.lane.no_work_now reason=drained stopped_chunk=10313, then NO lane-finished /
    attempt-done event (only probes after). Slot stayed full, next job queued until restart.

### R3-1 - GPU power limit check (Run 3 L2 ~15-20% slower) - OK
when: 2026-11-18 20:21 (local)
commands:
    nvidia-smi -q -d POWER
    nvidia-smi --query-gpu=name,power.limit,power.default_limit,power.max_limit,clocks.max.graphics --format=csv
    Win32_Battery; powercfg /getactivescheme
result: RTX 3050 A Laptop GPU, driver 592.82: current/requested power limit 48.96 W, default 40 W, min 5 W, max 50 W
    (query power.limit [N/A], known). clocks.max.graphics 3105 MHz. Idle draw 1-6 W. On AC (BatteryStatus 2, 100%),
    ORIGINAL HP charger (human confirmed). Power plan High performance. -> no power cap (unlike L4's 30 W).
    Reading: likely the "3050 A" GPU variant itself (different chip from L1/L4 RTX 3050) - not verified.

### R2A-2 - Run 2A kill test (stop L2 agent mid-job) - OK
when: 2026-11-18 23:46-23:55 (local); recorded late 2026-11-22 17:34 (session ended before the report)
commands:
    HUMAN-approved UAC (Admin) timer script $HOME\mf-tools\killtest.ps1 (cue lab/chat/20261118-233720-L1-to-L2.md):
    Stop-Service MeshForgeAgent -Force at 23:51:00; Start-Service MeshForgeAgent at 23:55:00
result: armed 23:46:09.554; stopping 23:51:00.057; Stopped 23:51:02.912; starting 23:55:00.195; Running 23:55:00.915.
    "ready" sent 23:46:24. Stop/start times were NOT sent to L1 at the time (sent now). Job result: see L1 report.

STOPPED 2026-11-22 17:34 (human "stop") - last step R2A-2; open: LEAD ML per-part gate task (new session, not this one)

### V-1 - Video join scene: identity reset - OK
when: 2026-11-22 17:37 (local)
commands: HUMAN-approved UAC (Admin): meshforge-agent identity reset (refused without --yes); identity reset --yes; identity show; Get-Service
result: old revoked node_01M7Z6SD26ZBNYKR65JB9YDFPB removed; new local node_01M81JE9JRJ7BGYYDZH2WKTM3V, enrolled False, service Running.
    Cue lab/chat/20261122-173534-LEAD-to-L2.md. Next: human opens Hive join screen + recorder, then join on Aayush's GO.

### V-2 - Video join scene: re-join on camera - OK
when: 2026-11-22 17:56 (local)
commands: HUMAN joined in Hive console (code typed by human, never seen by agent), recorded with Snipping Tool;
    HUMAN-approved UAC (Admin): identity reset --yes (2nd, video retake); identity show; agent.log
result: take 1 joined node_01M81JE9JRJ7BGYYDZH2WKTM3V 17:47 (video not good, identity reset -> needs LEAD revoke);
    take 2 joined node_01M81K4K8DGSYFMT04YRQWGD5H, enrolled True, cert until 2026-12-22T12:25Z, session ONLINE (hello_accepted), service Running.
    Video Desktop\presentation\L2-join.mp4 (9.4 MB). Note: after a service restart the console link shows "not signed in" - open Hive from Start menu.

### V-3 - Policy after re-join (console screenshot) - OK
when: 2026-11-22 18:20 (local)
result: Hive console "What this laptop shares" on node_01M81K4K8DGSYFMT04YRQWGD5H: run jobs ON, CPU limit none, RAM limit none, jobs at once 1,
    GPU jobs ON, share GPU (Mode 2/4) OFF, VRAM limit none, without container ON, network OFF, pause benchmarks OFF -> same as team setup.
