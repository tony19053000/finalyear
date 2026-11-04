# Lab announcement - from the Lead (2026-11-03)

Every lab agent reads this file after each `git merge origin/main`. Newest message on top.

## 2026-11-05 (4) - L4: V0.6 G2 agent hard-kill - DO IT NOW (job ends ~15:06 IST)

G1 done. Job `job_01M6JYC715XDXRTJ18WFBH238D` runs on L4 since 15:01:23. **L4 human, Administrator PowerShell:**
1. Hard-kill the agent child:
```
$a = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*meshforge.agent.winservice*child*" } | Select-Object -First 1; Stop-Process -Id $a.ProcessId -Force
```
2. BEFORE restarting, check no orphan job process is left (must print nothing):
```
Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object CommandLine -like '*main.py 300*'
```
3. `Start-Service MeshForgeAgent` if it is not running again by itself.
Record times + outputs, EMIT `SIGNAL: v06-g2-done L4 orphan=<yes|no>`. Details: origin/lab/L1 REPORT last entry.

## 2026-11-05 (3) - L4: V0.6 G1 Wi-Fi drop - DO IT NOW (time-critical)

L1 runs job `job_01M6JY167XY1FVXB1VTAD60AT1` on L4 (started 14:55:24 IST, ends ~15:00:24). **L4 agent:** tell your
human at once to run this in an **Administrator PowerShell** (it turns L4's Wi-Fi off for 60 s, then on):
```
$w=(Get-NetAdapter | Where-Object {$_.Name -like '*Wi*Fi*' -and $_.Status -eq 'Up'}).Name; Disable-NetAdapter -Name $w -Confirm:$false; Start-Sleep 60; Enable-NetAdapter -Name $w -Confirm:$false
```
Then record the off/on times (clock) and EMIT `SIGNAL: v06-g1-wifi-done L4`. Details: origin/lab/L1 REPORT.md last entry
(`SIGNAL: v06-g1-go L4`). If the job already ended when you read this, still do it, record the times, and say so.

## 2026-11-05 (2) - CONTINUE WITH L1, L3, L4 - L2 ON HOLD

**L2 (AILESH) is unavailable for a few hours.** Continue the campaign with **L1, L3, L4** now:
1. **L3, L4:** HUMAN runs the peer firewall command from the 2026-11-05 note below (Administrator PowerShell,
   `firewall-sync`), confirm the peer rule exists and the console Checks page is green, record it, emit
   `SIGNAL: peer-rule-ok <Ln>`. L4 keeps its `controller_address` line.
2. **L1:** resume V0.6 at C6.13-a with **L1, L3, L4** only. Every step that names L2: run it without L2 and write
   `L2: on hold (not present)` in the report - do NOT wait for L2. Steps that need exactly 4 laptops: run with 3,
   note it, and mark "L2 part pending".
3. **L2 (when back):** pull main, read this note, run `firewall-sync` (HUMAN, Admin), then do only the steps
   marked "L2 part pending" in L1's REPORT plus your own TODO steps you skipped, then rejoin the phase the others are in.
4. Everything else unchanged: one test at a time on the network, HUMAN for admin/physical steps, push after each step.

## 2026-11-05 - CORRECTION: the 47120 peer firewall rule is made by an admin command, not at service start

My 2026-11-04 note was wrong (Lead's mistake): the Windows service does NOT create the peer rule at start. By design
(D389) the rule is made by the admin command `firewall-sync`; the MeshForge console **Checks** page shows a warning
"Peer file sharing is on, but the firewall rule ... is missing" with the exact command to copy. Workaround, HUMAN,
on each Windows laptop with `[agent.peer] bind` set, **Administrator** PowerShell:
```
& "C:\Program Files\MeshForge\0.17.0-1\python\python.exe" -I -m meshforge.agent.winservice firewall-sync
```
(or copy the command from the console's Checks page - it has the right path). Then check
`Get-NetFirewallRule -DisplayName *MeshForge*` shows the peer rule (Private/Domain) and the Checks page is green.
Record it, mark the earlier "BUG: no firewall rule" as **UX gap F7, not a defect** (Lead follow-up: the rule should
follow the setting without a manual admin command). Then continue V0.6.

## 2026-11-04 - Answer (SUPERSEDED, see 2026-11-05): no 47120 firewall rule after install (L3, L4 C5.11-a)

**Not an installer bug - by design.** The peer data server (port 47120) is **off until the owner sets
`[agent.peer] bind`** (agent/settings.py PeerSettings). The Windows service creates the inbound 47120 rule only
when peer serving is on (`sync_firewall`, at service start). For the V0.5 peer-transfer steps on a Windows laptop:
HUMAN, Administrator: add to `C:\ProgramData\MeshForge\config\config.toml`
```
[agent.peer]
bind = "<this laptop's Wi-Fi IP>"
```
then `Restart-Service MeshForgeAgent`, then re-check: `Get-NetFirewallRule -DisplayName *MeshForge*` must now show
the peer rule (Private profile). **If the rule is still missing after that, THAT is a bug - report it.** Keep the
`controller_address` line on L4. Known product gap (Lead follow-up): there is no console switch for peer sharing yet;
and the IP must be updated if DHCP changes it.

## 2026-11-03 (5) - "Run jobs" needs an agent restart (bug F5, fix coming)

All 4 nodes are online. But a node only accepts jobs if **Run jobs was ON when its agent connected**: turning
it on later is sent to the controller but not applied (bug F5, Lead is fixing it). L4 works (it restarted).
**Workaround, HUMAN, once per laptop after Run jobs is ON in its console:**
- Windows (L2, L3): Administrator PowerShell `Restart-Service MeshForgeAgent`
- L1: first turn **Run jobs** (and GPU) on in L1's MeshForge console, then `sudo systemctl restart meshforge-agent`
Then L1 reruns C17-k (hello job to each laptop). Record the restart in your REPORT.

## 2026-11-03 (4) - JOIN NOW (all four together)

L1 has 4 invites ready (`SIGNAL: invites-ready L1 count=4`). Everyone is installed. **Join now:**
- Each laptop: open MeshForge (console) -> the HUMAN types that laptop's invite code -> Join -> owner settings:
  Run jobs on; GPU on for L1/L2/L4, off for L3; "Allow jobs without a container" on for L2/L3/L4; name = L1..L4;
  Save -> Checks page. L1 also: "Give Docker access".
- Then record your join step from your TODO (identity show, doctor, Checks screenshot) and
  EMIT `SIGNAL: joined <Ln> node=<node_id>`. L1 then checks `compute nodes` shows all 4 connected (C17-j).
- Continue your TODO from there (V0.3 onward) on your own.
- **Answer to the firewall QUESTION (L3, L4):** no MeshForge firewall rule after a Windows install is **by design**:
  the agent only dials out to the controller; the inbound rule appears only when peer serving is on (V0.5). Check it there.
- **Answer to L2's policy QUESTION:** `accept_gpu_sessions` = "Share the GPU with programs on other laptops"
  (Mode 2/4 remote/combined GPU, deferred) - **False is correct**. GPU *jobs* are allowed by "Let jobs use this
  laptop's GPU" = `max_gpu_jobs` >= 1. `max_cpu_percent` 50 is the documented console default
  (docs/user/owner-settings.md) - leave it.

## 2026-11-03 (3) - S1 matrix DONE - joining may start (after the firewall cleanup)

All 6 S1 pairs are done (L1 report `lab/reports/2026-11-03-S1-network.md`, every cell n=5). Thank you.
1. **L3, L4 (and L1/L2 if they still have one): HUMAN removes the "MeshForge S1 TCP/UDP" firewall rules now**
   (Administrator PowerShell: `Remove-NetFirewallRule -DisplayName "MeshForge S1 TCP","MeshForge S1 UDP"`).
   L2 also removes the leftover "MeshForge S2" rule. Record it.
2. **joining may start**: resume your install/join steps where you put them on hold (L1 C17-h then C17-i invites,
   L2 join, L3/L4 V0.17a install + join). The human types every invite code.
3. L3/L4: your other V0.2 catch-up items (S2/S3 if your TODO has them) may run after joining; they are
   non-blocking.

## 2026-11-03 (2) - PRIORITY CHANGE (team): network tests first, joining later

**Hold every install/join step for now** (L1 C17-h/C17-i, L2 agent install + join, L3/L4 install). First we finish
the **complete Wi-Fi network test (S1) between all four laptops**, every pair, both directions:

| pair | who runs the client |
|---|---|
| L1-L2 | done 2026-11-01 |
| L1-L3 (new), L1-L4 | L1 -> L3, L1 -> L4 |
| L2-L3, L2-L4 | L2 -> L3, L2 -> L4 |
| L3-L4 | L3 -> L4 |

(S1 measures both directions in one run, so one client run per pair is enough.)

- **L3 and L4:** finish round 1 + catch-up P0, then start your S1 server (`SIGNAL: s1-serve-up L3` / `L4`, firewall
  rule by the human) and keep it running until every pair to you is done.
- **L1 and L2:** start your S1 server again too (HUMAN re-adds the 47201 rule on Windows), emit
  `s1-serve-up L1` / `L2` in your CURRENT report, and run your client runs from the table above. One client
  at a time on the whole network: before a run, check no other laptop's report says a client run is in progress
  (write `S1 client RUNNING <you>-to-<peer>` before you start, `s1-client-done <you>-to-<peer>` after).
- When the six pairs are done: every laptop stops its server, HUMAN removes the 47201 rules, push raw files,
  emit `s1-matrix-done <you>`. **L1 then builds the S1 matrix report** for L1-L4.
- Only after the Lead posts "joining may start" here do the install/join steps continue.

## 2026-11-03 - the lab after the 2-day break

**The laptops now**

| id | host | OS | IP (Wi-Fi, DHCP - re-check) | GPU | role / state |
|---|---|---|---|---|---|
| L1 | JARVISM3 | Linux | 192.168.31.13 | RTX 3050 4 GB | **controller installed + fabric created + running** (`https://192.168.31.13:47110/`, agent gRPC 47100, enrolment 47101). Admin user `admin` (password: human only). V0.2 done. |
| L2 | AILESH | Windows 11 | 192.168.31.155 | RTX 2050 4 GB, CUDA torch | V0.2 done. Agent installer downloaded; install + join next. |
| L3 | **MOHIT (NEW laptop)** | Windows 11 | 192.168.31.71 | none (Intel UHD) | **Replaced the old L3 (TIWARI_JI, retired; backup branch `lab/L3-old-TIWARI_JI`, ignore it).** Doing round 1 + V0.2 catch-up. |
| L4 | new, arrived | (from its round 1) | (from its `L4-ready`) | (from its round 1) | Starting round 1 + V0.2 catch-up. |

Old L3 results (TIWARI_JI) never count for L3. Only signals in the CURRENT `REPORT.md` of a branch count.

**What happens next, in order**
1. **L3 and L4:** round 1, then V0.2 catch-up (L4 TODO phases P0 + CU, L3 uses its own name). They emit
   `L3-ready` / `L4-ready` and `s1-serve-up L3` / `s1-serve-up L4`.
2. **L1 and L2:** when those signals appear, run your S1 client against L3 and L4 (Lead note at the end of your
   TODO), push, emit `s1-client-done <you>-to-L3` / `-to-L4`. L3 and L4 also measure each other.
3. **L1 (meanwhile):** C17-h install L1's own agent (HUMAN sudo), then C17-i invites: **L2's invite first**
   (L2 is ready now), L3's and L4's when they reach their install step.
4. **L2 (meanwhile):** install the agent and join with L1's invite as soon as L1 has created it (human types the
   code). Remove the leftover "MeshForge S2" firewall rule when your TODO says so.
5. Then V0.3 onward with all laptops joined. Phases that "need L4" also wait for the new L3's `joined`.

**Known issues / fixes**
- L3 found that PowerShell `-EncodedCommand` hangs on some Windows laptops (Defender). Lab tools only, not the
  product. Fix "fix(lab): run PowerShell via temp .ps1 -File" is coming on main; rerun the affected steps after it
  (inventory R1-3, S7 R1-5). L2/L4 on Windows: if inventory or S7 times out, it's this - report and continue.
- IPs come from the router (no reservations): re-check your IP before each phase and re-emit `<Ln>-ready` if it
  changed.
- Wi-Fi only (D425). Keep laptops on charger, sleep off, Windows profile Private.
- Never type or write passwords, invite codes or tokens; the human does.
