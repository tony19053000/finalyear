# L3 REPORT

### R1-1 - Who am I - OK
when: 2026-11-01 10:20 (local)
laptop: L3 (Windows 11 Home Single Language 10.0.26300, hostname TIWARI_JI)
commands:
    Get-CimInstance Win32_OperatingSystem / Win32_Processor / Win32_VideoController
    netsh wlan show interfaces
    Get-NetIPAddress -AddressFamily IPv4 ; Get-NetConnectionProfile
result: CPU AMD Ryzen 7 5800HS (8c/16t), RAM 15.4 GB, GPU AMD Radeon integrated only - NO NVIDIA GPU (this is the no-GPU laptop). Wi-Fi IP 192.168.31.236, MediaTek MT7921, 5 GHz ch 157, 802.11ac, signal 87% (RSSI -37), link 866.7 Mbps.
  Wi-Fi profile: Public. Human was asked to switch to Private and chose to keep it Public (may block inbound connections in round 2).
  Same room as L1: not answered yet.
evidence: this entry
QUESTIONS: Network profile left Public by human decision - Lead please note for round 2.

R1-1 addendum (2026-11-01 10:21): human confirmed L3 IS in the same room as L1.

### R1-2 - Tools - FAIL
when: 2026-11-01 10:21 (local)
laptop: L3 (Windows 11, hostname TIWARI_JI)
commands:
    git --version ; uv --version ; uv sync ; uv run python --version ; docker version
result: git 2.52.0.windows.1 OK. uv NOT INSTALLED ("The term 'uv' is not recognized"), so uv sync / uv run did not run. Docker NOT INSTALLED. System Python 3.12 exists (AppData\Local\Programs\Python\Python312).
evidence: this entry
BUG: not a code bug - uv missing on L3 blocks R1-2..R1-6. Asking human whether to install uv (official installer).
