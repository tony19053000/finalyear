# >>> NEW L3 LAPTOP (2026-11-03) - READ THIS FIRST <<<

The team REPLACED the L3 laptop. The old L3 (Windows, TIWARI_JI) is retired; its results stay in git history
and under its own hostname in `lab/reports/` but no longer count as L3. You are the NEW L3. Start from zero:

1. **NL-0 Archive the old report** (on branch `lab/L3`): `git fetch origin`, `git checkout lab/L3` (create from
   `origin/lab/L3` if missing), `git merge origin/main`, then `git mv lab/coord/L3/REPORT.md
   lab/coord/L3/REPORT-old-TIWARI_JI.md`, create a fresh `lab/coord/L3/REPORT.md` with the line
   `# L3 REPORT (new laptop, replaces TIWARI_JI)`, commit `lab(L3): new laptop, old report archived`, push.
   Old SIGNAL lines must not be visible to the others any more - that is why.
2. **Round 1** below (R1-1 ... R1-7) on this laptop, then the round-1b items that apply (Wi-Fi Private, `uv sync
   --extra agent`, CUDA torch only if this laptop has an NVIDIA GPU and the human agrees).
3. **Catch-up for V0.2**: follow `lab/coord/L4/TODO.md` phases **P0 and CU** exactly, but with **L3** in every
   signal, file name and `--name` (e.g. `SIGNAL: L3-ready`, `SIGNAL: s1-serve-up L3`, `v02-raw-pushed L3`).
4. Then continue in THIS file from **Phase V0.17a** (install + join) onward. Skip every V0.2 step in this file
   (already covered by step 3). Steps here that say "done early" or WAITING refer to the old laptop - redo them.

L1, L2 and L4 have been told (Lead note in their TODOs) to run their S1 pairs against the new L3.

---

# L3 TODO - round 1: this laptop alone (V0.2, Wi-Fi only)

Read `lab/coord/RULES.md` first. Work on branch `lab/L3`. Write results to `lab/coord/L3/REPORT.md`.
These steps need no other laptop. Plugged in, performance power mode, close other apps before R1-5.
Windows: use PowerShell (native, not WSL). Linux: bash.

- **R1-1 Who am I.** Record in REPORT.md: OS + version, hostname, CPU, RAM, GPU (if any), Wi-Fi IP address
  (`ip -4 addr` / `ipconfig`), Wi-Fi band and signal if shown (`nmcli dev wifi` / `netsh wlan show interfaces`),
  whether it is in the same room as L1. Windows: is the Wi-Fi network profile **Private**? (Settings > Network >
  Wi-Fi > properties.) If it is Public, ask the human to switch it to Private and record that.
- **R1-2 Tools.** `git --version`, `uv --version`, `uv sync` in the repo root (report errors in full),
  `uv run python --version`. Docker: `docker version` (just report if missing, do not install).
- **R1-3 Inventory (0.2.2).** Linux: `uv run python lab/inventory.py --out "lab/reports/inventory/$(hostname).json"`
  Windows: `uv run python lab/inventory.py --out "lab/reports/inventory/$env:COMPUTERNAME.json"`
  Report the exit code and 3-4 headline lines (CPU, RAM, GPU, disk).
- **R1-4 GPU check (0.2.1 / 0.2.3), only if this laptop has an NVIDIA GPU.** `nvidia-smi` (paste the header table).
  Then `uv run python -c "import torch; print(torch.__version__, torch.cuda.is_available())"` (if torch is not
  installed, report that; do not install anything). If Docker exists: `docker run --rm --gpus all
  nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi` (ask the human first; it downloads ~100 MB). No NVIDIA GPU:
  write SKIPPED with the reason.
- **R1-5 Hardware scores (0.2.11).** `uv run python lab/spikes/s7_hardware.py run --quick --name L3` then
  `uv run python lab/spikes/s7_hardware.py run --name L3` (1-5 min; add `--skip-gpu` if no NVIDIA GPU).
  Report the exit code and the scores printed on stderr. If it prints `!! ON BATTERY` or `thermal throttling`,
  ask the human to fix it and run again (report both runs).
- **R1-6 Environments (0.2.10), only if Docker is installed.** `uv run python lab/spikes/s6_environments.py run --quick --name L3`
  then `uv run python lab/spikes/s6_environments.py run --name L3`. No Docker: run it anyway (venv cases still
  run) and report the exit code and which cases were skipped.
- **R1-7 Push** everything (`lab/reports/` + REPORT.md), write `## DONE`, push, tell the human.

Round 2 (pairs of laptops: network S1, cross-OS S2, transport S3) comes after the Lead has everybody's IP.

---

# Full campaign (V0.2 -> V1.0)

Written by the Lead's planning session, 2026-11-01. Overview, signals, durations: `lab/coord/CAMPAIGN.md`.
L3 = an **agent** laptop; OS from your round 1 (R1-1). Each step gives **Windows** (native PowerShell) and
**Linux** (bash) lines: use the ones for this laptop. L1 = controller, 192.168.31.13 (check `SIGNAL: L1-ready`).
Special roles of L3: the **timed fresh install** (V0.17 criterion 1, if L3 is Windows), the **V0.7 revoke
victim** (comes back as a NEW node), and the **demo laptop for 05-install-join**.

## How to read this campaign

- **Run it all on your own (RULES.md).** Start after round 1 is DONE (and any round 1b the Lead adds). Phases
  in order; do not stop after a step or at a checkpoint. Talk to the human only for `HUMAN:` steps, a
  **blocking** failure, a WAIT > 30 min, or the end.
- **Blocking.** Each step says *[blocks: ...]* or *[non-blocking]*. Failed non-blocking: FAIL + real output,
  push, go on. Failed blocking: FAIL + BUG, push, skip only the named steps, tell the human
  "Blocked at <step> - tell the Lead to read lab/L3".
- **HUMAN BATCH** steps collect a phase's admin/consent actions. **`LIVE:`** = humans at two laptops act at the
  same moment (they talk directly). **`WAIT:`** check with `git fetch origin` +
  `git show origin/lab/L1:lab/coord/L1/REPORT.md` (grep / `Select-String` for `SIGNAL`), every 2-3 min; do
  independent later steps meanwhile; tell the human after 30 min. **`EMIT SIGNAL: ...`** = that line (filled in)
  in your REPORT entry, commit, push at once.
- **Secrets:** join codes, passwords, tokens are typed by the human only, never into a file/REPORT/chat.
- **L4 S1 catch-up (any time, 0.2.5).** When origin/lab/L4 shows `SIGNAL: s1-serve-up L4`, finish your
  current step, then (not during another measurement; after `SIGNAL: s1-client-done L2-to-L4`; laptops not present are skipped):
  `uv run python lab/spikes/s1_network.py client --link wifi --server <L4-IP>`, push the JSON,
  EMIT SIGNAL: s1-client-done L3-to-L4. Then go back to where you were. *[non-blocking]*
- **Wi-Fi only (D425):** `--link wifi`; Ethernet halves NOT RUN. Subnet `192.168.31.0/24`.
- **LEAD CHECKPOINT** after each version: push, `EMIT SIGNAL: Vx.y-done`, `git fetch origin && git merge
  origin/main`, continue at once. Also merge `origin/main` every ~30 minutes.
- **Installed agent (from V0.17a):** stop/start = Windows `Stop-Service|Start-Service MeshForgeAgent`
  (Administrator); Linux `sudo systemctl stop|start meshforge-agent`. **Hard kill:** Windows (Administrator)
  `$a = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*meshforge.agent.winservice*child*" } | Select-Object -First 1; Stop-Process -Id $a.ProcessId -Force`
  (as `lab/chaos_win.ps1` `kill_agent`); Linux `sudo systemctl kill -s KILL meshforge-agent`. Agent log:
  Windows `Get-Content C:\ProgramData\MeshForge\logs\agent.log -Tail 50`; Linux `journalctl -u meshforge-agent -n 50`.
- Dates: `$D = Get-Date -Format yyyy-MM-dd` / `D=$(date +%F)`; evidence `lab/reports/raw/<date>-<topic>/`.

---

## Phase P0 - preparation (~15 min)

- **C0-a** `git fetch origin`, `git checkout lab/L3`, `git merge origin/main`, `git rev-parse --short HEAD`,
  `uv sync --extra agent`. *[blocks: everything]*
- **C0-b HUMAN BATCH:** plugged in, sleep off (Windows Administrator: `powercfg /change standby-timeout-ac 0`,
  `powercfg /change hibernate-timeout-ac 0`, pause Windows Update 1 week; Ubuntu: Settings > Power > Automatic
  Suspend Off). Windows: `Get-NetConnectionProfile` must be `Private` (the human switches it: Settings > Network
  & internet > Wi-Fi > Network profile type). Clock: Windows `w32tm /query /status`, Linux `timedatectl`.
  *[Private blocks: V0.2+; rest non-blocking]*
- **C0-c EMIT SIGNAL: L3-ready ip=<Wi-Fi IP> os=<windows|linux> host=<hostname>** *[blocks: others' WAITs]*

---

## Phase V0.2 - spikes (~2 h)

- **C2.0-h HUMAN BATCH 1 (S1 firewall).** Windows, Administrator:
  ```powershell
  New-NetFirewallRule -DisplayName "MeshForge S1 TCP" -Group "MeshForge lab" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 47201 -RemoteAddress 192.168.31.0/24 -Profile Private
  New-NetFirewallRule -DisplayName "MeshForge S1 UDP" -Group "MeshForge lab" -Direction Inbound -Action Allow -Protocol UDP -LocalPort 47201 -RemoteAddress 192.168.31.0/24 -Profile Private
  ```
  Linux, only if `sudo ufw status` is active: `sudo ufw allow from 192.168.31.0/24 to any port 47201`.
  Python "Windows Security Alert": Private only, Allow. *[blocks: C2.5-a]*
- **C2.4-a (0.2.4) Network facts.** Windows `netsh wlan show interfaces`, `Get-NetAdapter | ft Name,LinkSpeed`,
  `Get-NetConnectionProfile`; Linux `nmcli dev wifi`, `ip -4 addr`. Same room as L1? ->
  `lab/reports/raw/<date>-V0.2-network-facts/L3.txt`. *[non-blocking]*
- **C2.5-a (0.2.5) S1 server:** `uv run python lab/spikes/s1_network.py serve --bind <L3-IP> --idle 3600 --duration 14400`
  (own window). EMIT SIGNAL: s1-serve-up L3 ip=<L3-IP>. *[blocks: L1/L2 clients]*
- **C2.5-b S1 client L3 -> L4.** WAIT: `SIGNAL: s1-client-done L2` and `SIGNAL: s1-serve-up L4`. If L4 is not
  present: SKIPPED (L4's catch-up does it later). `uv run python lab/spikes/s1_network.py client --link wifi --server <L4-IP>`.
  EMIT SIGNAL: s1-client-done L3. Keep the L3 server only until L1's and L2's `s1-client-done`; then Ctrl-C and
  HUMAN removes the rules (Windows `Remove-NetFirewallRule -DisplayName "MeshForge S1 TCP","MeshForge S1 UDP"`;
  Linux `sudo ufw delete allow from 192.168.31.0/24 to any port 47201`). *[non-blocking]*
- **C2.6-a (0.2.6, optional, Windows only) second S2 pair with L1** - only if L1's TODO asks (its C2.6 note):
  client first with no inbound rule
  (`uv run python lab/spikes/s2_crossos.py client --server 192.168.31.13 --link wifi --name L3 --no-inbound-rule --note "<prompt?>"`,
  EMIT `s2-done L3-to-L1`), then HUMAN adds the "MeshForge S2" rule (L2 TODO C2.6-b line), `serve --bind <L3-IP> --name L3`,
  EMIT `s2-serve-up L3`, WAIT `s2-done L1-to-L3`, Ctrl-C, HUMAN removes the rule. *[non-blocking]*
- **C2.3-a (0.2.1, 0.2.3) GPU stack** (only with an NVIDIA GPU): from round 1 R1-4; if torch was missing and the
  human agrees (~2.5 GB): `uv pip install "torch==2.14.*" --index-url https://download.pytorch.org/whl/cu126` and
  the R1b-3 check; with Docker: the container test of R1-4. *[non-blocking]*
- **C2.r-a Push all raw files** (S1, S6, S7, inventory, network facts). EMIT SIGNAL: v02-raw-pushed L3.
  *[blocks: L1's C2.r-a]*
- **LEAD CHECKPOINT V0.2.** EMIT SIGNAL: V0.2-done, merge `origin/main`, continue.

---

## Phase V0.17a - install and join (~45 min; `V0.17-install.md` W1 or L2, `V1.0-campaign.md` L1.2)

- **C17-a Download + check.** Windows: `gh release download v0.17.0 --repo tony19053000/finalyear --pattern "MeshForge-Agent-Setup-*" --pattern SHA256SUMS --dir $HOME\mf-pkgs`,
  `Get-FileHash` vs `SHA256SUMS`. Linux: `gh release download v0.17.0 --repo tony19053000/finalyear --pattern "meshforge-agent_*" --pattern SHA256SUMS --dir ~/mf-pkgs`,
  `cd ~/mf-pkgs && sha256sum -c SHA256SUMS --ignore-missing`. *[blocks: V0.17a+]*
- **C17-b WAIT: `SIGNAL: controller-up`** (L1). Ask L1's human for an invite code on the dashboard **now**
  (the timed run needs it ready). *[blocks: C17-c]*
- **C17-c Windows: (0.17.8 W1, criterion 1) the TIMED install, no terminal.** HUMAN (if possible a person who has
  not done it before); screen recording on (Win+Alt+R). Stopwatch starts when the installer file is on disk
  (it was downloaded in C17-a: start the clock at the double-click and write that down). Double-click ->
  SmartScreen **More info -> Run anyway** -> UAC **Yes** -> licence -> folder -> share page -> install -> "MeshForge
  is installed and running." -> Finish with **Open MeshForge** -> console: Start -> type the code -> Join ->
  policy (safe defaults) -> Save and continue -> checks -> Done. Stop the clock when L1's `compute nodes` /
  dashboard shows L3 CONN `yes` (L1's human says). **No terminal opened during the run.**
  Record: time per stage, total, "terminal opened: no", recording file name (the video is not committed).
  Note in REPORT: **L3 is not a fresh machine** (uv/Python/git from round 1), so this is weaker evidence than a
  fresh VM. **OK when:** total < 10:00. *[non-blocking for the campaign]*
  **Linux instead:** HUMAN `sudo apt install ./meshforge-agent_0.17.0_amd64.deb` (expect `is running version
  0.17.0`, `has not joined a fabric yet`), app menu > MeshForge > join with the code (`V0.17-install.md` L2.1).
- **C17-d HUMAN: owner settings after the timed run:** **Run jobs** on; Docker present ->
  Windows Start menu "MeshForge - give Docker access" / Linux app menu "Give Docker access"; no Docker ->
  **Allow jobs without a container** on; NVIDIA GPU -> **Let jobs use this laptop's GPU** on. *[blocks: V0.4+ jobs]*
- **C17-e Check.** Windows (Administrator): `meshforge-agent identity show`, `sc.exe queryex MeshForgeAgent`,
  `type "C:\Program Files\MeshForge\current.txt"`, `meshforge-agent policy show`. Linux: `meshforge-agent identity show`,
  `meshforge-agent doctor`, `systemctl status meshforge-agent`. EMIT SIGNAL: installed L3 and
  SIGNAL: joined L3 node=<node_id>. *[blocks: V0.3+]*
- **LEAD CHECKPOINT V0.17a.** EMIT SIGNAL: V0.17a-done, merge `origin/main`, continue.

---

## Phase V0.3 - fabric core (driven from L1)

- **C3.2-a (0.3.2) Windows only:** `uv sync --extra agent; uv run pytest tests/unit/agent -q -p no:cacheprovider`
  -> `passed`, no `failed`. Linux: SKIPPED (CI covers it). *[non-blocking]*
- **C3.7-a/b LIVE:** Wi-Fi off/on × 5 on L1's cue (C3.7-a), then agent stop/start × 5 (C3.7-b, the stop/start
  commands above); `meshforge-agent identity show` before and after. *[non-blocking]*
- **C3.13-a** NOT RUN - dev task superseded by the installed service (`V1.0-campaign.md` L1.4). *[non-blocking]*
- **LEAD CHECKPOINT V0.3.** EMIT SIGNAL: V0.3-done, merge `origin/main`, continue.

## Phase V0.4 - remote execution

- **C4.0 HUMAN:** clock sync - Windows Administrator `w32tm /resync` then
  `w32tm /stripchart /computer:time.windows.com /samples:5 /dataonly`; Linux
  `chronyc tracking 2>/dev/null || timedatectl timesync-status`. EMIT SIGNAL: clock-offset L3 ms=<n>
  (> 200 ms off L1: FAIL, fix time sync). *[blocks: L1's latency step for L3]*
- **C4.15-g (Windows only) part G:**
  `uv sync --extra agent --extra controller --group dev; uv run pytest tests/unit/common/test_bundle_case_insensitive.py tests/component/agent/test_docker_sandbox.py -q -rs`
  (say whether Docker tests ran or skipped). EMIT SIGNAL: v04-g-done L3. *[non-blocking]*
- **LEAD CHECKPOINT V0.4.** EMIT SIGNAL: V0.4-done, merge `origin/main`, continue.

## Phase V0.5 - data plane

- **C5.11-a** peer port check: Windows `Get-NetFirewallRule | Where-Object DisplayName -like '*MeshForge*'`;
  Linux `sudo ufw status` (HUMAN; if active and 47120 missing: `sudo ufw allow from 192.168.31.0/24 to any port 47120 proto tcp`).
  *[non-blocking]*
- **C5.11-f** L1's reader runs here (part G first half): save the agent log lines `agent.cas.*` to
  `lab/reports/raw/<date>-V0.5-data/L3-agent.txt`. *[non-blocking]*
- **C5.11-g (part H victim) only if L4 is NOT joined** (otherwise L4 is the victim): empty L3's cache first:
  HUMAN stops the agent; list the data dir (Windows `Get-ChildItem C:\ProgramData\MeshForge`; Linux
  `sudo ls /var/lib/meshforge`); delete only the `cas` and `gc` folders the checklist names (Windows
  `Remove-Item -Recurse "<dir>\cas", "<dir>\gc"`; Linux `sudo rm -rf /var/lib/meshforge/cas /var/lib/meshforge/gc`).
  **If those folders do not exist there: do not delete anything; SKIPPED + QUESTION.** Start the agent. LIVE:
  follow the log; ~20 s after `staging`, stop the agent, wait 5 s, start it (L1 resubmits). *[non-blocking]*
- **C5.11-h (part I GC, optional)** Windows only, on the same laptop as H: HUMAN edits
  `C:\ProgramData\MeshForge\config\config.toml` (the service config, `lab/spikes/SP1.md` §4) with part I's
  `[agent.gc]` block, restarts the service; after the test restores it. File missing: SKIPPED + QUESTION. *[non-blocking]*
- **LEAD CHECKPOINT V0.5.** EMIT SIGNAL: V0.5-done, merge `origin/main`, continue.

## Phase V0.6 - placement

- **C6.13-c/d LIVE** if L1 says the long job runs on L3: Wi-Fi off 60 s (G1) / hard kill (G2) and before the
  restart no `main.py 120` process (Windows `Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object CommandLine -like '*main.py 120*'`;
  Linux `pgrep -af "main.py 120"`). *[non-blocking]*
- **LEAD CHECKPOINT V0.6.** EMIT SIGNAL: V0.6-done, merge `origin/main`, continue.

## Phase V0.7 - security (L3 = the code-refusal laptop and the revoke victim)

- **C7.0 HUMAN BATCH:** the human types every code L1's human gives; Administrator PowerShell / sudo open.
  *[blocks: C7.12-c, -d]*
- **C7.12-a** clock: `[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()` / `date -u +%s`; EMIT SIGNAL: clock-unix L3 t=<n>.
- **C7.12-c (part C) refusals** with `meshforge-agent join --force` (Windows: Administrator PowerShell; Linux: if
  refused for permissions, `sudo meshforge-agent join --force` and say so): reuse of L3's own used code ->
  `token_used`; the 1-minute code after 70 s -> `token_expired`; the revoked invite -> `token_revoked`; a code with
  one character changed in the CA-pin part (the human edits it while typing) -> `pin_mismatch`. After each,
  `meshforge-agent identity show` = still the old node, still online. Never record a code. *[non-blocking]*
- **C7.12-d (part R) revoke victim.** L1 starts the sleep-300 job here; follow the agent log; LIVE: L1 revokes.
  Record the local time of the `node_revoked` line. Then `meshforge-agent join --force` with a new code ->
  refused `node_revoked`; `meshforge-agent identity reset --yes`; `meshforge-agent join` with another new code
  (or the console) -> `identity show`: a **NEW node_id**. Re-apply the owner settings (C17-d) if they were reset.
  EMIT SIGNAL: rejoined L3 node=<new id>. *[blocks: L3's part in V0.8+ until rejoined]*
- **C7.12-w (part W, Windows only) owner caps:** owner settings "Most memory one job may use" 512 MB,
  "Most CPU one job may use" 25 %, "Allow jobs without a container" on (HUMAN, console); L1 runs the 1 GiB job and
  the busy loop: watch Task Manager CPU %, record; restore the settings. *[non-blocking]*
- **C7.12-x (part D, Windows + Docker only):** "Allow jobs without a container" off; L1 runs a normal job; `docker info`
  -> paste `Security Options` and `Cgroup Version`. No Docker: SKIPPED. *[non-blocking]*
- **LEAD CHECKPOINT V0.7.** EMIT SIGNAL: V0.7-done, merge `origin/main`, continue.

## Phase V0.8 - dashboard

- **C8.10-d LIVE (E1):** Wi-Fi off on L1's cue, back on when asked. *[non-blocking]*
- **LEAD CHECKPOINT V0.8.** EMIT SIGNAL: V0.8-done, merge `origin/main`, continue.

## Phase V0.9 - benchmarks

- **C9.10-b (C1 netem) Linux L3 only:** follow `lab/netem/README.md` (sudoers rule + `[agent.conditions]` keys: the
  human does the sudo parts), then `sudo -n /usr/sbin/tc qdisc show dev <iface>`; L1 runs the netem campaign;
  after: no `netem` line; part C1.4 crash recovery with the hard kill. Windows: SKIPPED. *[non-blocking]*
- **C9.10-g Before the night:** `meshforge-agent policy show` -> `accept_jobs: True`, `pause_benchmarks: False`;
  mains, sleep off. EMIT SIGNAL: policy-ok L3. HUMAN: nobody uses L3 tonight. *[blocks: L1's C9.10-h]*
- **LEAD CHECKPOINT V0.9.** EMIT SIGNAL: V0.9-done, merge `origin/main`, continue.

## Phase V0.10 - Mode 3

- **C10.10-a** `meshforge-agent policy show` (V0.10 0.2 lines); EMIT SIGNAL: policy-ok L3. *[blocks: L1's C10.10-a]*
- **LEAD CHECKPOINT V0.10.** EMIT SIGNAL: V0.10-done, merge `origin/main`, continue.

## Phases V0.14, V0.15 - stay online, unused, on mains during L1's campaigns. EMIT SIGNAL: V0.15-done when L1's is there.

---

## Phase V0.17b - upgrade / rollback / uninstall (`V0.17-install.md`)

WAIT: "this build" in `lab/coord/CAMPAIGN.md` §Builds. None: SKIPPED.
- **C17-l Windows: W4.1** (uninstall data kept, install 0.17.0, then this build: same node_id, current.txt, binPath).
  **Linux: L3.2 + L4.1 + L5.1** exactly as written on this laptop (VA role). *[non-blocking, L3 must be joined
  again afterwards]*. EMIT SIGNAL: v017b-done L3.
- **LEAD CHECKPOINT V0.17b.** EMIT SIGNAL: V0.17b-done, merge `origin/main`, continue.

---

## Phase V1.0 - soak, evaluation, demo

- **C10-0 HUMAN BATCH:** mains, sleep Never, updates paused 4 days (Windows) / Linux
  `sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target`.
  *[blocks: C10-c..-f]*
- **C10-b Facts:** Windows `Get-NetAdapter | Format-Table Name,Status`; Linux `ip -br link`; hostname, node_id.
  EMIT SIGNAL: soak-facts L3 os=<os> iface=<name> node=<node_id>. **Linux L3 also:** `V1.0-soak.md` A4 (L1's human
  sets up SSH from L1, HUMAN) and A5 (`lab/mf-chaos` + sudo rule, HUMAN). *[blocks: L1's C10-c]*
- **C10-c WAIT: `SIGNAL: soak-plan`** (L1); `git fetch origin; git checkout origin/lab/L1 -- lab/reports/raw/<date>-V1.0-soak-plan`;
  copy `plan.json` + `chaos-schedule.json` into the run folder (Windows `C:\mf-soak\RUN\`, Linux `~/mf-soak/RUN/`).
- **C10-d Samplers + chaos (before t0).** Windows: sampler
  `uv run --extra agent python lab\soak.py run --dir C:\mf-soak\RUN --label L3 --dir-watch logs=C:\ProgramData\MeshForge\logs --dir-watch cache=C:\ProgramData\MeshForge\cache@51200`;
  chaos (HUMAN, Administrator): `powershell -ExecutionPolicy Bypass -File lab\chaos_win.ps1 -RunDir C:\mf-soak\RUN -Label L3`.
  Linux: `uv run --extra agent python lab/soak.py run --dir ~/mf-soak/RUN --label L3 --interval 60 --dir-watch cas=/var/lib/meshforge/cas@20480`
  (chaos comes over SSH from L1). EMIT SIGNAL: soak-sampler-up L3 and SIGNAL: soak-chaos-up L3. After a `reboot`
  action: log in, run the same commands again (they resume). *[blocks: C10-e]*
- **C10-e (72 h + 30 min)** check twice a day; `>>> MANUAL revoke_rejoin on L3`: Leave + Join in the console
  with a human-typed code, report the new node_id. Outside events -> L1's human (`notes.json`). *[non-blocking]*
- **C10-f After the end:** copy `samples.L3.jsonl` (+ Windows `chaos-log.l3.jsonl`) to
  `lab/reports/raw/<date>-V1.0-soak-L3/`, push, EMIT SIGNAL: soak-files-pushed L3. Cleanup: Windows
  `powershell -ExecutionPolicy Bypass -File lab\chaos_win.ps1 -RunDir C:\mf-soak\RUN -Label L3 -Cleanup`, adapters
  enabled, no fill file; Linux: L1 runs `chaos.py cleanup`, then HUMAN `sudo rm /etc/sudoers.d/mf-chaos /usr/local/sbin/mf-chaos`
  and `sudo systemctl unmask ...`. Power/updates back. EMIT SIGNAL: soak-cleanup-done L3. *[non-blocking]*
- **C10-g Evaluation nights:** online, unused, on mains. *[non-blocking]*
- **C10-h (1.0.7 demo 05) before L1's rehearsals, Windows only:** HUMAN uninstall with **Yes** (remove all data)
  (= V0.17 W5.1 second half): Administrator `uv run python lab\verify_uninstall.py --os windows --data-removed` ->
  `CLEAN`; `Get-NetFirewallRule | Where-Object DisplayName -like '*MeshForge*'` prints nothing. EMIT SIGNAL:
  demo-laptop-ready L3. The demo itself installs and joins it again (L1's human drives `05-install-join.sh`).
  Linux L3: `sudo apt purge meshforge-agent` + `sudo python3 lab/verify_uninstall.py --os linux --data-removed`. *[non-blocking]*
- **LEAD CHECKPOINT V1.0.** EMIT SIGNAL: V1.0-done. `## DONE`, push, tell the human "L3 finished - tell the Lead".

---
## LEAD NOTE (2026-11-01, after C3.2-a / C4.15-g FAILs)
Both are test bugs (Lead fixing on main): symlink tests must skip without the Windows symlink privilege; the
component/agent conftest must not abort collection. C4.15-g command now installs `--extra controller`.
After the next `git merge origin/main` shows commit "test: skip symlink tests without privilege", re-run
C3.2-a and C4.15-g once and report (expect 0 failed). Until then: non-blocking, continue.
