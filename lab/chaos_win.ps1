<#
.SYNOPSIS
  V1.0 lab kit (roadmap 1.0.2, D411): the soak's chaos actions on a Windows laptop.

.DESCRIPTION
  Reads the run's chaos-schedule.json (written by `soak.py plan` on the
  controller laptop and copied here), does every action whose target is this
  laptop's label at its time, and appends each to chaos-log.<label>.jsonl in
  the same format as lab/chaos.py (a "start" line before, an "end" line after).
  Copy that log back into the run folder before `soak.py report`.

  Run in an **Administrator** PowerShell (netsh, fsutil and killing the
  service's processes need it), from the repository root:

    powershell -ExecutionPolicy Bypass -File lab\chaos_win.ps1 -RunDir C:\mf-soak\RUN -Label L3

  Resumable: run the same command again (after a reboot action, after a
  crash); finished actions are skipped, one that was running is logged
  "interrupted", one more than -LateMin late is logged "missed".

  Kinds done here: kill_agent, kill_job_tree, reboot, net_down_short,
  net_down_long, disk_fill (the catalogue in lab/_soak/chaos.py). netem is never
  scheduled on Windows; revoke_rejoin asks the operator. disk_fill leaves
  params.leave_free_mib (512 MiB = half the agent's 1 GiB free-disk floor) free on
  the fill file's drive, so the agent must refuse work disk_full.

  Cleanup: every start first undoes what a killed run may have left (the
  adapter re-enabled, the fill file removed) - lasting faults also undo
  themselves in try/finally. `-Cleanup` does only that and exits:

    powershell -ExecutionPolicy Bypass -File lab\chaos_win.ps1 -RunDir C:\mf-soak\RUN -Label L3 -Cleanup
#>
param(
    [Parameter(Mandatory = $true)][string]$RunDir,
    [Parameter(Mandatory = $true)][string]$Label,
    [double]$LateMin = 10,
    [switch]$SkipManual,
    [switch]$Cleanup
)

$ErrorActionPreference = "Stop"
$schedule = Get-Content -Raw -Path (Join-Path $RunDir "chaos-schedule.json") | ConvertFrom-Json
$target = $schedule.targets | Where-Object { $_.label -eq $Label }
if (-not $target) { throw "no target '$Label' in chaos-schedule.json" }
$logPath = Join-Path $RunDir ("chaos-log." + $Label.ToLower() + ".jsonl")
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Write-Log([hashtable]$record) {
    $line = ($record | ConvertTo-Json -Compress -Depth 4) + "`n"
    [System.IO.File]::AppendAllText($logPath, $line, $utf8)
}

function Get-Var([string]$name, [string]$default) {
    if ($target.vars -and $target.vars.PSObject.Properties[$name]) { return [string]$target.vars.$name }
    return $default
}

$fillFile = Get-Var "fill_file" "C:\ProgramData\MeshForge\mf-chaos-fill.bin"

function Invoke-Cleanup {
    # Idempotent: an enabled adapter stays enabled, a missing file stays missing.
    # Returns $true when everything is clean.
    $ok = $true
    $iface = [string]$target.iface
    $adapter = "n/a"
    if ($iface) {
        try { netsh interface set interface name="$iface" admin=enabled | Out-Null } catch { }
        if ($LASTEXITCODE -eq 0) { $adapter = "enabled" }
        else {
            # netsh fails for an adapter that is already enabled on some builds: ask Windows.
            $state = (Get-NetAdapter -Name $iface -ErrorAction SilentlyContinue).AdminStatus
            if ([string]$state -eq "Up") { $adapter = "enabled" }
            else { $adapter = "FAILED (netsh exit $LASTEXITCODE, admin status '$state')"; $ok = $false }
        }
    }
    Remove-Item -Force -ErrorAction SilentlyContinue "$fillFile"
    if (Test-Path "$fillFile") { $left = "STILL PRESENT"; $ok = $false } else { $left = "absent" }
    Write-Host "cleanup ${Label}: adapter '$iface' $adapter, fill file $left"
    return $ok
}

function Get-AgentProcess {
    Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -like "*meshforge.agent.winservice*child*" } |
        Select-Object -First 1
}

function Get-Descendants([int]$parentId) {
    $all = Get-CimInstance Win32_Process
    $out = @()
    $queue = @($parentId)
    while ($queue.Count -gt 0) {
        $p = $queue[0]; $queue = @($queue | Select-Object -Skip 1)
        foreach ($child in ($all | Where-Object { $_.ParentProcessId -eq $p })) {
            $out += $child; $queue += [int]$child.ProcessId
        }
    }
    return $out
}

function Invoke-Action($action) {
    $duration = [double]$action.duration_s
    switch ($action.kind) {
        "kill_agent" {
            $agent = Get-AgentProcess
            if (-not $agent) { return @("failed", "no meshforge agent process") }
            Stop-Process -Id $agent.ProcessId -Force
            return @("done", "killed pid $($agent.ProcessId)")
        }
        "kill_job_tree" {
            $agent = Get-AgentProcess
            if (-not $agent) { return @("failed", "no meshforge agent process") }
            $kids = Get-Descendants ([int]$agent.ProcessId)
            foreach ($k in $kids) { Stop-Process -Id $k.ProcessId -Force -ErrorAction SilentlyContinue }
            return @("done", "killed=$($kids.Count)")
        }
        { $_ -in "net_down_short", "net_down_long" } {
            $iface = [string]$target.iface
            if (-not $iface) { return @("failed", "targets.json has no 'iface' for $Label") }
            netsh interface set interface name="$iface" admin=disabled | Out-Null
            try { Start-Sleep -Seconds $duration }
            finally { netsh interface set interface name="$iface" admin=enabled | Out-Null }
            return @("done", "$iface down $duration s")
        }
        "disk_fill" {
            $file = $fillFile
            $leaveMib = [long]$action.params.leave_free_mib
            if ($leaveMib -le 0) { return @("failed", "schedule has no params.leave_free_mib") }
            $drive = New-Object System.IO.DriveInfo ([System.IO.Path]::GetPathRoot($file))
            $need = [long]($drive.AvailableFreeSpace - $leaveMib * 1MB)
            if ($need -le 0) { return @("done", "already under $leaveMib MiB free") }
            try {
                fsutil file createnew "$file" $need | Out-Null
                $drive = New-Object System.IO.DriveInfo ([System.IO.Path]::GetPathRoot($file))
                $leftMib = [long]($drive.AvailableFreeSpace / 1MB)
                Start-Sleep -Seconds $duration
            }
            finally { Remove-Item -Force -ErrorAction SilentlyContinue "$file" }
            return @("done", "filled $need bytes, $leftMib MiB free, for $duration s")
        }
        "revoke_rejoin" {
            if ($SkipManual) { return @("skipped", "manual action skipped") }
            Write-Host ">>> MANUAL revoke_rejoin on $Label - see lab/checklists/V1.0-soak.md. Enter = done, 'skip' = skip"
            $answer = Read-Host
            if ($answer -eq "skip") { return @("skipped", "operator skipped") }
            return @("done", "operator confirmed")
        }
        default { return @("skipped", "kind $($action.kind) is not done on Windows") }
    }
}

$clean = Invoke-Cleanup
if ($Cleanup) { if ($clean) { exit 0 } else { exit 3 } }
if (-not $clean) { Write-Host "warning: cleanup incomplete - check the adapter / fill file by hand (checklist C3)" }

$done = @{}
$started = @{}
if (Test-Path $logPath) {
    foreach ($line in Get-Content $logPath) {
        if (-not $line.Trim()) { continue }
        try { $r = $line | ConvertFrom-Json } catch { continue }
        if ($r.type -eq "end") { $done[$r.id] = $true }
        if ($r.type -eq "start") { $started[$r.id] = $true }
    }
}

foreach ($action in ($schedule.actions | Where-Object { $_.target -eq $Label })) {
    if ($done[$action.id]) { continue }
    $at = [DateTimeOffset]::Parse($action.at)
    $base = @{ type = "end"; schema = "meshforge.chaos-log/1"; id = $action.id; kind = $action.kind;
               target = $Label; scheduled_at = $action.at; executor = "windows"; exit_code = $null;
               job_id = $null }
    if ($started[$action.id]) {
        $now = [DateTimeOffset]::UtcNow.ToString("o")
        Write-Log ($base + @{ started_at = $now; ended_at = $now; effect_until = $now;
                              outcome = "interrupted"; detail = "the script stopped while this action ran" })
        continue
    }
    while (($left = ($at - [DateTimeOffset]::UtcNow).TotalSeconds) -gt 0) { Start-Sleep -Seconds ([Math]::Min($left, 5)) }
    if (([DateTimeOffset]::UtcNow - $at).TotalMinutes -gt $LateMin) {
        $now = [DateTimeOffset]::UtcNow.ToString("o")
        Write-Log ($base + @{ started_at = $now; ended_at = $now; effect_until = $now;
                              outcome = "missed"; detail = "the script was not running at its time" })
        continue
    }
    $start = [DateTimeOffset]::UtcNow
    Write-Log @{ type = "start"; id = $action.id; kind = $action.kind; target = $Label; at = $start.ToString("o") }
    Write-Host "$($start.ToString('o')) $($action.id) $($action.kind) ($($action.duration_s) s)"
    if ($action.kind -eq "reboot") {
        $until = $start.AddSeconds([double]$action.duration_s).ToString("o")
        Write-Log ($base + @{ started_at = $start.ToString("o"); ended_at = $start.ToString("o");
                              effect_until = $until; outcome = "done"; detail = "Restart-Computer" })
        Restart-Computer -Force
        exit 0
    }
    try { $result = Invoke-Action $action } catch { $result = @("failed", $_.Exception.Message) }
    $end = [DateTimeOffset]::UtcNow
    $until = $start.AddSeconds([double]$action.duration_s)
    if ($until -lt $end) { $until = $end }
    Write-Log ($base + @{ started_at = $start.ToString("o"); ended_at = $end.ToString("o");
                          effect_until = $until.ToString("o"); outcome = $result[0];
                          detail = [string]$result[1] })
    Write-Host "  -> $($result[0]) $($result[1])"
}
Write-Host "no more actions for $Label"
