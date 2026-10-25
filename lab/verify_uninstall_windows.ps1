# verify_uninstall probe (windows)
$ErrorActionPreference = 'SilentlyContinue'
function P($k, $v) { Write-Output ("{0}`t{1}" -f $k, $v) }
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
P 'elevated' $admin
Get-CimInstance Win32_Service | Where-Object { $_.Name -like '*MeshForge*' -or $_.DisplayName -like '*MeshForge*' } | ForEach-Object { P 'service' ("{0} {1} {2}" -f $_.Name, $_.State, $_.PathName) }
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and ($_.CommandLine -match 'MeshForge|meshforge[.-]agent') -and ($_.CommandLine -notmatch 'verify_uninstall') } | ForEach-Object { P 'process' ("{0} {1}" -f $_.ProcessId, $_.CommandLine) }
Get-ScheduledTask | Where-Object { $_.TaskName -like '*MeshForge*' } | ForEach-Object { P 'task' ("{0}{1} {2}" -f $_.TaskPath, $_.TaskName, $_.State) }
if ($admin) { Get-NetFirewallRule | Where-Object { $_.DisplayName -like '*MeshForge*' -or $_.Name -like '*MeshForge*' } | ForEach-Object { P 'firewall' ("{0} ({1})" -f $_.DisplayName, $_.Profile) } }
$pf = Join-Path $env:ProgramFiles 'MeshForge'
if (Test-Path -LiteralPath $pf) { P 'program' $pf }
Get-ChildItem (Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs') -Filter 'MeshForge*' | ForEach-Object { P 'program' $_.FullName }
$key = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\__APPID___is1'
if (Test-Path $key) { P 'uninstall_entry' $key }
$path = [Environment]::GetEnvironmentVariable('Path', 'Machine')
foreach ($item in ($path -split ';')) { if ($item -match 'MeshForge') { P 'path' $item } }
$members = & net localgroup docker-users 2>$null
foreach ($line in $members) { if ($line.Trim() -ieq 'NT SERVICE\__SERVICE__' -or $line.Trim() -ieq '__SID__') { P 'docker_member' $line.Trim() } }
$pd = Join-Path $env:ProgramData 'MeshForge'
if (Test-Path -LiteralPath $pd) { P 'data' $pd }
Get-ChildItem 'C:\Users\*\AppData\Local\MeshForge' -Directory -Filter 'identity.migrated-*' | ForEach-Object { P 'user_identity' $_.FullName }
P 'done' 'ok'
