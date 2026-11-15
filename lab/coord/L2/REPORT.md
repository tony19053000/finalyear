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
