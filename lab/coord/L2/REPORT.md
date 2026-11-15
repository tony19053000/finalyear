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
