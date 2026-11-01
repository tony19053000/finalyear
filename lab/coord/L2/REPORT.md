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
