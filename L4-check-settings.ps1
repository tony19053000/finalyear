# L4 settings check. Run in an admin PowerShell window:
#   powershell -ExecutionPolicy Bypass -File L4-check-settings.ps1
$out = "$env:USERPROFILE\L4-check-out.txt"; Remove-Item $out -ea 0
function L($s){ $s | Tee-Object -FilePath $out -Append }
L "== host: $env:COMPUTERNAME  time: $(Get-Date -f 'yyyy-MM-dd HH:mm:ss')"
L "== power plan:"; L (powercfg /getactivescheme)
L "== on AC power: $((Get-CimInstance -Namespace root/wmi -ClassName BatteryStatus -ea 0 | select -First 1).PowerOnline)"
L "== agent versions installed:"; L ((Get-ChildItem 'C:\Program Files\MeshForge' -Directory -ea 0 | % Name) -join ', ')
L "== service:"; L ((Get-Service MeshForgeAgent -ea 0 | % { "$($_.Name) $($_.Status)" }))
L "== policy show:"; L ((& 'C:\Program Files\MeshForge\bin\meshforge-agent.cmd' policy show 2>&1) -join "`n")
L "== last qos lines:"; L ((Select-String -Path C:\ProgramData\MeshForge\logs\agent.log -Pattern 'qos' -ea 0 | select -Last 5 | % Line) -join "`n")
L "== done. Output saved to $out"
