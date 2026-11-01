# L2 TODO - round 1: this laptop alone (V0.2, Wi-Fi only)

Read `lab/coord/RULES.md` first. Work on branch `lab/L2`. Write results to `lab/coord/L2/REPORT.md`.
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
- **R1-5 Hardware scores (0.2.11).** `uv run python lab/spikes/s7_hardware.py run --quick --name L2` then
  `uv run python lab/spikes/s7_hardware.py run --name L2` (1-5 min; add `--skip-gpu` if no NVIDIA GPU).
  Report the exit code and the scores printed on stderr. If it prints `!! ON BATTERY` or `thermal throttling`,
  ask the human to fix it and run again (report both runs).
- **R1-6 Environments (0.2.10), only if Docker is installed.** `uv run python lab/spikes/s6_environments.py run --quick --name L2`
  then `uv run python lab/spikes/s6_environments.py run --name L2`. No Docker: run it anyway (venv cases still
  run) and report the exit code and which cases were skipped.
- **R1-7 Push** everything (`lab/reports/` + REPORT.md), write `## DONE`, push, tell the human.

Round 2 (pairs of laptops: network S1, cross-OS S2, transport S3) comes after the Lead has everybody's IP.

---

# L2 TODO - round 1b (Lead, after reading your round-1 report)

Good report. Lead decisions: Docker is SKIPPED on L2 for now (7.6 GB RAM; venv runtime is enough). Keep going:

- **R1b-1 Wi-Fi profile Private (REQUIRED for round 2).** Ask the human to set the Wi-Fi network profile to
  **Private** (Settings > Network & internet > Wi-Fi > <network> > Network profile type > Private). This is the
  human's action, not yours. Then record `Get-NetConnectionProfile` output. Round 2 cannot start while it is Public.
- **R1b-2 numpy for real CPU scores.** `uv sync --extra agent` then rerun
  `uv run python lab/spikes/s7_hardware.py run --name L2 --skip-gpu`. Report exit code + headline GFLOP/s and
  memory GB/s. (Before running, copy the existing JSON to `<name>__nonumpy.json` so it is not overwritten.)
- **R1b-3 OPTIONAL, ask the human first (~2.5 GB download): CUDA torch for a GPU score.**
  `uv pip install "torch==2.14.*" --index-url https://download.pytorch.org/whl/cu126`, then
  `uv run python -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"`
  and, if True, `uv run python lab/spikes/s7_hardware.py run --name L2` (copy the previous JSON aside first).
  If the human says no: SKIPPED. Note: a later `uv sync` may remove this torch; that is expected.
- **R1b-4** push, `## DONE round 1b`, tell the human.

---

# Full campaign (V0.2 -> V1.0)

Written by the Lead's planning session, 2026-11-01. Overview, signals, durations: `lab/coord/CAMPAIGN.md`.
L2 = this laptop: **Windows 11, AILESH**, i5-12450H, 7.6 GB RAM, RTX 2050, Wi-Fi 192.168.31.155 (5 GHz).
**No Docker on L2** (Lead decision): jobs run with "Allow jobs without a container". Role: **agent**; also the
Windows browser / Windows `compute` user, and the main Windows test laptop. L1 = controller,
192.168.31.13 (re-check `SIGNAL: L1-ready` on origin/lab/L1).

## How to read this campaign

- **Run it all on your own (RULES.md).** Start after round 1b is DONE. Phases in order; do not stop after a
  step or at a checkpoint. Talk to the human only for `HUMAN:` steps, a **blocking** failure, a WAIT > 30 min,
  or the end.
- **Blocking.** Every step says *[blocks: ...]* or *[non-blocking]*. Failed non-blocking: FAIL with the real
  output, push, go on. Failed blocking: FAIL + BUG, push, skip only the named steps, tell the human
  "Blocked at <step> - tell the Lead to read lab/L2".
- **HUMAN BATCH** steps collect a phase's admin/UAC/consent actions so the human is called once.
  **`LIVE:`** = the humans at L1 and L2 act at the same moment (they talk directly; git is too slow).
- **`WAIT: <condition>`**: `git fetch origin; git show origin/lab/L1:lab/coord/L1/REPORT.md | Select-String SIGNAL`
  (change L1 to the laptop named). Check every 2-3 min; do later independent steps meanwhile; tell the human
  after 30 min. **`EMIT SIGNAL: ...`** = that exact line (filled in) in your REPORT entry, commit, push at once.
- **Secrets.** Join codes, passwords, API tokens: the **human types them** into the console / prompt. Never put
  them in a command line you run, a file, REPORT.md or chat.
- **Shell:** native PowerShell (not WSL). "Administrator PowerShell" = the human opens it (UAC); you tell
  them the exact command, they run it and you read the output they paste or the file it writes.
- **L4 S1 catch-up (any time, 0.2.5).** When origin/lab/L4 shows `SIGNAL: s1-serve-up L4`, finish your
  current step, then (not during another measurement; after `SIGNAL: s1-client-done L1-to-L4`; laptops not present are skipped):
  `uv run python lab/spikes/s1_network.py client --link wifi --server <L4-IP>`, push the JSON,
  EMIT SIGNAL: s1-client-done L2-to-L4. Then go back to where you were. *[non-blocking]*
- **Wi-Fi only (D425).** Every `--link` is `wifi`; Ethernet halves are NOT RUN (D425). Subnet `192.168.31.0/24`.
- **LEAD CHECKPOINT** after each version: push, `EMIT SIGNAL: Vx.y-done`, `git fetch origin; git merge
  origin/main`, continue at once. Also merge `origin/main` every ~30 minutes.
- **The product under test** from V0.17a on is the installed v0.17.0 agent (`C:\Program Files\MeshForge`,
  service `MeshForgeAgent`). Lab tools run from this clone with `uv run`. Stop/start the agent =
  `Stop-Service MeshForgeAgent` / `Start-Service MeshForgeAgent` (Administrator PowerShell). **Hard kill** (a
  crash, no goodbye; same process match as `lab/chaos_win.ps1` `kill_agent`), Administrator PowerShell:
  `$a = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*meshforge.agent.winservice*child*" } | Select-Object -First 1; Stop-Process -Id $a.ProcessId -Force`
  (the service may restart its child by itself: record when it came back). Agent log:
  `Get-Content C:\ProgramData\MeshForge\logs\agent.log -Tail 50` (`-Wait` to follow).
- `compute` on L2 = `uv run compute` from this clone (record whether the installer also put `compute` on PATH:
  `Get-Command compute`). Dates: `$D = Get-Date -Format yyyy-MM-dd`; evidence `lab\reports\raw\$D-<topic>\`.

---

## Phase P0 - preparation (~15 min)

- **C0-a Same commit.** `git fetch origin; git checkout lab/L2; git merge origin/main; git rev-parse --short HEAD;
  uv sync --extra agent`. *[blocks: everything]*
- **C0-b HUMAN BATCH: power, updates, profile.** Plugged in; Administrator PowerShell:
  `powercfg /change standby-timeout-ac 0` and `powercfg /change hibernate-timeout-ac 0`; Settings > Windows Update >
  Pause updates (1 week, renew later for the soak). You check: `Get-NetConnectionProfile` -> `NetworkCategory :
  Private` (round 1b). `w32tm /query /status` (read only) and `[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()`.
  *[Private profile blocks: V0.2 and later; the rest non-blocking]*
- **C0-c EMIT SIGNAL: L2-ready ip=192.168.31.155 os=windows host=AILESH** (re-check with `ipconfig`).
  *[blocks: other laptops' WAITs]*

---

## Phase V0.2 - spikes on Wi-Fi (~3 h)

Commands from `lab/README.md` and `lab/spikes/S2.md` §3 / `S3.md` §2. Never two measurements at once.

- **C2.0-h HUMAN BATCH 1 (S1 firewall), Administrator PowerShell:**
  ```powershell
  New-NetFirewallRule -DisplayName "MeshForge S1 TCP" -Group "MeshForge lab" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 47201 -RemoteAddress 192.168.31.0/24 -Profile Private
  New-NetFirewallRule -DisplayName "MeshForge S1 UDP" -Group "MeshForge lab" -Direction Inbound -Action Allow -Protocol UDP -LocalPort 47201 -RemoteAddress 192.168.31.0/24 -Profile Private
  ```
  If a "Windows Security Alert" for Python appears: tick **Private networks** only, **Allow** (never Cancel).
  *[blocks: C2.5-a]*
- **C2.4-a (0.2.4) Network facts.** `netsh wlan show interfaces` (band, channel, signal, receive/transmit rate),
  `Get-NetAdapter | ft Name,LinkSpeed`, `Get-NetConnectionProfile`. HUMAN: same room as L1? DHCP reservation
  done (L1's human asks the router)? **Evidence:** `lab\reports\raw\$D-V0.2-network-facts\L2.txt`. *[non-blocking]*
- **C2.5-a (0.2.5) S1 server** (own window, keep open):
  `uv run python lab/spikes/s1_network.py serve --bind 192.168.31.155 --idle 3600 --duration 14400`.
  EMIT SIGNAL: s1-serve-up L2 ip=192.168.31.155. *[blocks: L1's C2.5-b]*
- **C2.5-b (0.2.5) S1 client L2 -> L3, L4.** WAIT: `SIGNAL: s1-client-done L1` and `s1-serve-up` from L3 (L4).
  `uv run python lab/spikes/s1_network.py client --link wifi --server <L3-IP> --server <L4-IP>` (only present
  ones; none present: SKIPPED). EMIT SIGNAL: s1-client-done L2. Then Ctrl-C the L2 server. *[non-blocking]*
- **C2.5-c HUMAN: remove the S1 rules** (Administrator):
  `Remove-NetFirewallRule -DisplayName "MeshForge S1 TCP","MeshForge S1 UDP"`. Check (read only):
  `Get-NetFirewallRule -Direction Inbound -Action Block -Enabled True | Where-Object DisplayName -like "*python*"`
  must print nothing. *[blocks: C2.6-a (the client must have no inbound rule)]*
- **C2.6-a (0.2.6) S2 client L2 -> L1** (no inbound rule here). WAIT: `SIGNAL: s2-serve-up L1`.
  `uv run python lab/spikes/s2_crossos.py client --server 192.168.31.13 --link wifi --name L2 --no-inbound-rule --note "<Defender prompt: none / what it said>"`
  (~25-35 min). A Defender prompt on the client is a finding: note it, allow nothing. EMIT SIGNAL: s2-done
  L2-to-L1. *[non-blocking]*
- **C2.6-b HUMAN BATCH 2 (S2 server rule), Administrator:**
  `New-NetFirewallRule -DisplayName "MeshForge S2" -Group "MeshForge lab" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 47202-47203 -RemoteAddress 192.168.31.0/24 -Profile Private`
  *[blocks: C2.6-c]*
- **C2.6-c (0.2.6) S2 server L2.** `uv run python lab/spikes/s2_crossos.py serve --bind 192.168.31.155 --name L2`.
  Once, while it runs, a second `serve` with the same `--bind` in another window must exit `1` with
  `cannot listen` (port exclusivity on Windows): record. EMIT SIGNAL: s2-serve-up L2. WAIT: `SIGNAL: s2-done
  L1-to-L2`. Ctrl-C. HUMAN: `Remove-NetFirewallRule -DisplayName "MeshForge S2"`. *[non-blocking]*
- **C2.7-a (0.2.7) S3 client L2 -> L1** (one run, both directions). WAIT: `SIGNAL: s3-serve-up L1`.
  `uv run python lab/spikes/s3_transport.py client --server 192.168.31.13 --link wifi --max-size 256MiB --name L2`
  EMIT SIGNAL: s3-done L2. *[non-blocking]*
- **C2.3-a (0.2.3) GPU exception.** Write into REPORT: L2's GPU stack = `nvidia-smi` + torch result from R1-4 /
  R1b-3; Docker GPU container: **not tested - no Docker on L2 (Lead decision)**; CPU/venv fallback.
  S6 (0.2.10) and S7 (0.2.11) were run in rounds 1/1b: nothing new. *[non-blocking]*
- **C2.r-a Push all raw files** (`lab/reports/raw/*S1*`, `*S2*`, `*S3*`, `*network-facts*`, inventory, S6/S7).
  EMIT SIGNAL: v02-raw-pushed L2. *[blocks: L1's C2.r-a]*
- **LEAD CHECKPOINT V0.2.** EMIT SIGNAL: V0.2-done, merge `origin/main`, continue.

---

## Phase V0.17a - install and join (~45 min; `V0.17-install.md` W2 with L2 as LW, `V1.0-campaign.md` L1.2)

L2 is not a fresh machine (uv, Python, a checkout): it is **not** the timed criterion-1 install (that is L3).

- **C17-a Download + check.**
  `gh release download v0.17.0 --repo tony19053000/finalyear --pattern "MeshForge-Agent-Setup-*" --pattern SHA256SUMS --dir $HOME\mf-pkgs`;
  `Get-FileHash $HOME\mf-pkgs\MeshForge-Agent-Setup-0.17.0-1.exe` and compare with its line in
  `$HOME\mf-pkgs\SHA256SUMS`. *[blocks: V0.17a and later]*
- **C17-b WAIT: `SIGNAL: controller-up`** on origin/lab/L1 (note the url and fp). *[blocks: C17-c]*
- **C17-c HUMAN BATCH (~15 min): install + join + policy.** Double-click the installer: SmartScreen **More info
  -> Run anyway** (screenshot `windows-smartscreen.png`), UAC **Yes**, licence, folder, the "What this computer
  will share" page (`windows-installer-share.png`), the last page (`windows-installer-finished.png`), Finish with
  **Open MeshForge**. In the console: Start -> the human types the join code L1's human gives -> Join -> owner
  settings: **Run jobs** on, **Allow jobs without a container** on (no Docker), **Let jobs use this laptop's GPU**
  on, name `L2` -> Save -> Checks page (screenshot `L2-console-checks.png`). Screenshots into
  `lab\reports\raw\$D-V0.17-install\`. EMIT SIGNAL: installed L2 (before the join, so L1's human makes the
  invite). *[blocks: every later phase]*
- **C17-d Check, Administrator PowerShell (the human pastes the output):** `meshforge-agent identity show`,
  `sc.exe qc MeshForgeAgent`, `sc.exe queryex MeshForgeAgent`, `type "C:\Program Files\MeshForge\current.txt"`,
  `meshforge-agent policy show`, `meshforge-agent --version`; also `Get-Content
  C:\ProgramData\MeshForge\logs\setup-result.json`. **OK when:** RUNNING, current.txt `0.17.0-1`, enrolled.
  EMIT SIGNAL: joined L2 node=<node_id>. *[blocks: every later phase]*
- **C17-e (0.17.1, SP1 §4 items 1, G4 lab step 5) HUMAN: reboot, sign in, wait 2 min;** then (Administrator)
  `sc.exe queryex MeshForgeAgent` (RUNNING, note PID) and the agent.log tail (an `agent.started` after the boot).
  HUMAN signs out and in again; `sc.exe queryex MeshForgeAgent` = **same PID**. *[non-blocking]*
- **C17-f (G4 lab step 6) Checks page** lines for Docker (absent), GPU, CUDA, firewall: paste.
  `Get-NetFirewallRule | Where-Object DisplayName -like '*MeshForge*' | ft DisplayName,Enabled,Profile`.
  *[non-blocking]*
- **C17-g (G4 lab step 8, optional) second non-admin Windows user** "request access" flow: only if the human has
  such a user; else SKIPPED. *[non-blocking]*
- **LEAD CHECKPOINT V0.17a.** EMIT SIGNAL: V0.17a-done, merge `origin/main`, continue.

---

## Phase V0.3 - fabric core (~1.5 h, driven from L1)

- **C3.2-a (0.3.2) Windows identity tests:** `uv sync --extra agent; uv run pytest tests/unit/agent -q -p no:cacheprovider`.
  **OK when:** `passed`, no `failed`. (A later `uv sync` may remove CUDA torch: expected.) *[non-blocking]*
- **C3.7-a LIVE (0.3.7): Wi-Fi drop × 5** when L1 runs `watch --node AILESH --link wifi`: on L1's "now" the
  human turns L2's Wi-Fi off; on "plug" back on. Do not touch the agent. Record what you saw. *[non-blocking]*
- **C3.7-b LIVE: agent restart × 5** when L1 runs `--action agent`: Administrator `Stop-Service MeshForgeAgent`
  / `Start-Service MeshForgeAgent` on L1's cue. Before and after: `meshforge-agent identity show` (same node_id).
  *[non-blocking]*
- **C3.13-a (0.3.13) logoff check:** NOT RUN - the dev scheduled task is superseded by the installed service;
  its sign-out check is C17-e (`V1.0-campaign.md` L1.4). *[non-blocking]*
- **LEAD CHECKPOINT V0.3.** EMIT SIGNAL: V0.3-done, merge `origin/main`, continue.

---

## Phase V0.4 - remote execution (~1 h on L2; `V0.4-remote-exec.md` A4, F, G)

- **C4.0 HUMAN BATCH, Administrator PowerShell:** `w32tm /resync` then
  `w32tm /stripchart /computer:time.windows.com /samples:5 /dataonly` (paste). Be ready for C4.15-e. *[non-blocking]*
- **C4.15-a Clock offset.** From the stripchart: L2's offset in ms (mean of the 5). EMIT SIGNAL: clock-offset
  L2 ms=<n>. If more than 200 ms from L1's (L1 REPORT): FAIL, tell the human to fix time sync. *[blocks: L1's C4.15-d]*
- **C4.15-e LIVE (c3): cancel / timeout jobs from L1 run here.** When L1's human says the sleeper was cancelled:
  `Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Select-Object ProcessId, CommandLine` - no
  `main.py` of the sleeper left. *[non-blocking]*
- **C4.15-g (c5, D62-4) Windows tests (part G):**
  `uv sync --extra agent --group dev; uv run pytest tests/unit/common/test_bundle_case_insensitive.py tests/component/agent/test_docker_sandbox.py -q -rs`
  **OK when:** the case-insensitive tests pass; the Docker tests skip "Docker not available". EMIT SIGNAL:
  v04-g-done L2. *[non-blocking]*
- **LEAD CHECKPOINT V0.4.** EMIT SIGNAL: V0.4-done, merge `origin/main`, continue.

---

## Phase V0.5 - data plane (~1.5 h; `V0.5-data-plane.md` D2, F)

- **C5.0 HUMAN BATCH:** `winget install iperf3` (or the iperf.fr zip; run `iperf3.exe` from its folder); free
  disk >= 20 GB (Explorer). *[blocks: C5.11-c]*
- **C5.11-a Peer port.** `Get-NetFirewallRule | Where-Object DisplayName -like '*MeshForge*'` (does the installer
  open TCP 47120 on Private?). Record. If none: QUESTION (the peer parts may then show no `from <L2>` line).
  *[non-blocking]*
- **C5.11-c (c2 reference) iperf3 Wi-Fi.** WAIT: `SIGNAL: iperf3-server-up` (L1).
  `iperf3 -c 192.168.31.13 -t 30 -P 4 -R` then `iperf3 -c 192.168.31.13 -t 30 -P 1 -R`. Record the
  **receiver** Bitrate lines. EMIT SIGNAL: iperf3-done L2 p4=<Mbit/s> p1=<Mbit/s>. *[non-blocking]*
- **C5.11-e** L1's 5 GB reader runs here (`--node <L2-ID>`): from the agent log, the `agent.cas.staged` /
  `agent.cas.input_staged` lines -> `lab\reports\raw\$D-V0.5-data\L2-agent.txt`. *[non-blocking]*
- **LEAD CHECKPOINT V0.5.** EMIT SIGNAL: V0.5-done, merge `origin/main`, continue.

---

## Phase V0.6 - placement (~1 h; `V0.6-scheduling.md` D, G)

- **C6.13-b (c4) Part D from L2.** HUMAN: `uv run compute login --controller https://192.168.31.13:47110 --fingerprint <fp> -u labadmin`
  (types the password). Then exactly part D's PowerShell lines (`$HOME\mf-lab\c4-tiny`, 300 MB `blob.bin`,
  `main.py`, `compute.yaml`) with `uv run compute`: step 1 history on L2 only, step 2 `uv run compute run .` +
  `uv run compute explain <JOB_ID>`, step 3 `c4-heavy` (60 s CPU loop) pinned on L2 and on the strongest
  laptop, then automatic + explain. **OK when:** step 2 chooses L2 / `local`; step 3 the smallest predicted
  compute. Save both `explain` outputs. EMIT SIGNAL: v06-d-done. *[non-blocking]*
- **C6.13-c/d LIVE** if L1 says the long job runs on L2: Wi-Fi off 60 s (G1) / the hard kill (G2, see "How to
  read") and before restarting: `Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object CommandLine -like '*main.py 120*'`
  must print nothing. *[non-blocking]*
- **LEAD CHECKPOINT V0.6.** EMIT SIGNAL: V0.6-done, merge `origin/main`, continue.

---

## Phase V0.7 - security (~1.5 h on L2; `V0.7-security.md` 0, P.3, X, S.5; G4 A, D)

- **C7.0 HUMAN BATCH:** install Wireshark (wireshark.org, with Npcap); be ready to type alice's password
  (C7.12-g) and to do the capture start/stop. *[blocks: C7.12-e]*
- **C7.12-a Clock.** `[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()`; EMIT SIGNAL: clock-unix L2 t=<n>. *[non-blocking]*
- **C7.12-b (P.3) identity ACLs.** Administrator: `meshforge-agent identity show`; the identity folder of the
  installed service: find it with `Get-ChildItem -Recurse -Force C:\ProgramData\MeshForge -Filter node.key`
  (do not open the file), then `icacls "<that path>"` and the same for `node.crt`. **OK when:** only SYSTEM /
  the service / Administrators - no `Users`, `Everyone`. EMIT SIGNAL: v07-icacls-done. *[non-blocking]*
- **C7.12-e (X) Capture.** WAIT: `SIGNAL: capture-start` (L1). HUMAN starts Wireshark on the Wi-Fi adapter,
  capture filter `tcp port 47120 or tcp port 47100 or tcp port 47110`. Stop when L1 writes `SIGNAL:
  capture-stop`; save as `lab\reports\raw\$D-V0.7-security\l2.pcapng`. Push. EMIT SIGNAL: capture-pushed.
  *[non-blocking]*
- **C7.12-h (S.5) Windows secret folder** while L1's `sleep 60` secret job runs here: find the folder with
  `Get-ChildItem -Recurse -Force -Directory -Filter "meshforge-secrets-*" C:\ProgramData\MeshForge` (the
  checklist's `$env:LOCALAPPDATA` path is the dev agent's), then a-f of S.5 (listing, `icacls`, a non-admin
  user's `type` = "Access is denied" only if that user exists, gone after the job, the hard kill + start-up
  sweep, OneDrive check). Never print a secret file's content. *[non-blocking]*
- **C7.12-g (G4 A) login from Windows:** HUMAN `uv run compute login --controller https://192.168.31.13:47110 --fingerprint "<a WRONG fingerprint>" --user alice`
  (expect "Nothing was sent"), then with the right one; `uv run compute whoami`;
  `icacls $env:APPDATA\MeshForge\compute\login.json` (only you); five wrong passwords then the right one
  (6th answer `429`). **G4 D rotation:** while a long job runs on L2 (ask L1), Administrator
  `meshforge-agent identity show` (cert_serial), `meshforge-agent rotate-key`, `identity show` again: new serial,
  same node_id; the job `succeeded`. *[non-blocking]*
- **LEAD CHECKPOINT V0.7.** EMIT SIGNAL: V0.7-done, merge `origin/main`, continue.

---

## Phase V0.8 - dashboard (~45 min; `V0.8-visual.md` D, E2, G, H)

- **C8.0 HUMAN BATCH:** import the CA file from L1 (`meshforge-controller ca trust-helper --os windows` output
  on L1) as that helper says; never click through a certificate warning. *[blocks: C8.10-d]*
- **C8.10-d (D1-D5) Windows browser:** open `https://192.168.31.13:47110/`, sign in as alice (human types),
  Overview, submit a folder from the Windows disk, download outputs and compare with the CLI
  (`uv run compute get <J>`; `Get-FileHash`), Settings > Secrets set + delete. Screenshots. *[non-blocking]*
- **C8.10-e (E2) LIVE:** start a heavy program (a video) while L1 watches L2's sparkline. *[non-blocking]*
- **C8.10-g (G) PowerShell completion:** only if `Get-Command compute` finds the installed `compute`; else
  SKIPPED ("compute not on PATH on Windows") + QUESTION. Commands exactly as part G. *[non-blocking]*
- **C8.10-h (H) port change:** keep the Jobs page open; record what it shows at L1's steps 3-5 (screenshots
  `H3.png`, `H5.png`). *[non-blocking]*
- **LEAD CHECKPOINT V0.8.** EMIT SIGNAL: V0.8-done, merge `origin/main`, continue.

---

## Phase V0.9 - benchmarks (~1.5 h + night; `V0.9-campaign.md` P1, C2, C3, R1, R2, B1)

- **C9.10-a (P1) bench folders on Windows:**
  `uv sync --all-extras; uv run pytest tests/component/bench -m component -q -p no:cacheprovider;
  uv run pytest tests/unit/bench/test_v09_g3a_publish.py -q -p no:cacheprovider;
  uv run python -c "import sys, platform; print(sys.platform, platform.version())"`. EMIT SIGNAL: v09-p1-done.
  *[non-blocking]*
- **C9.10-c (C2) LIVE cpu_load:** while L1's `cpu_load:2` run runs here: the two `Get-CimInstance ... conditions.burn`
  / `Get-Process ... PriorityClass` lines of part C2 (Idle), and again after the campaign (nothing). *[non-blocking]*
- **C9.10-d (C3)** if L1's plan says L2 offers `gpu_load`: `nvidia-smi` during a run. *[non-blocking]*
- **C9.10-e (R1, R2):** R1 HUMAN Administrator `meshforge-agent bench pause`, wait 60 s, `meshforge-agent bench resume`
  on L1's cue; agent log lines `owner_preempted` / `agent.bench.guard`. R2 (`[agent.bench] idle_threshold_s = 60`):
  HUMAN edits `C:\ProgramData\MeshForge\config\config.toml` (the service config, `lab/spikes/SP1.md` §4) as
  admin and restarts the service; if the file or the key is not there: SKIPPED + QUESTION. *[non-blocking]*
- **C9.10-f (B1) Benchmarks page** in L2's browser: the screenshots + the two Parquet row counts of part B1.
  *[non-blocking]*
- **C9.10-g Before the night:** Administrator `meshforge-agent policy show` -> `accept_jobs: True`,
  `pause_benchmarks: False`; mains power; sleep off; Windows Update paused. EMIT SIGNAL: policy-ok L2.
  HUMAN: nobody uses L2 tonight. *[blocks: L1's C9.10-h]*
- **LEAD CHECKPOINT V0.9.** EMIT SIGNAL: V0.9-done, merge `origin/main`, continue.

---

## Phase V0.10 - Mode 3 (~2.5 h, driven from L1; L2 = the C4 victim)

- **C10.10-a** Administrator `meshforge-agent policy show` (both lines as V0.10 0.2); EMIT SIGNAL: policy-ok L2.
  *[blocks: L1's C10.10-a]*
- **C10.10-d LIVE (c4) victim:** on L1's "now" (30-70 % done, L2's lane has a chunk), the **hard kill** (see "How
  to read"); note the time; **do not restart** until L1 says K ended: then `Start-Service MeshForgeAgent` (if the
  service already restarted its child, record that - it changes the test; QUESTION). *[non-blocking]*
- **C10.10-f (E3) owner guard:** Administrator `meshforge-agent bench pause` while L2's lane runs, `bench resume`
  after the job ends; the agent.log lines. *[non-blocking]*
- **LEAD CHECKPOINT V0.10.** EMIT SIGNAL: V0.10-done, merge `origin/main`, continue.

## Phases V0.14 and V0.15 - nothing to do on L2 except staying online

- **C14-a** keep L2 on mains, online, not used, during L1's evaluation campaigns (V0.15 E latency campaign
  1-2 h). EMIT SIGNAL: V0.14-done and V0.15-done when L1's REPORT shows them. *[non-blocking]*

---

## Phase V0.17b - upgrade, rollback, uninstall on Windows (~1.5 h; `V0.17-install.md` W2.10, W3, W4, W5)

WAIT: Lead's "this build" in `lab/coord/CAMPAIGN.md` §Builds (merge `origin/main`). None: SKIPPED.

- **C17-l (W2.10) upgrade 0.17.0 -> this build** (HUMAN runs the new installer): same node_id
  (`meshforge-agent identity show`), `type "C:\Program Files\MeshForge\current.txt"` = `<this>-1`,
  `sc.exe qc MeshForgeAgent` binPath. *[non-blocking]*
- **C17-m (W3) forced rollback** exactly as written (needs the revision-2 installer; without it SKIPPED).
  *[non-blocking, but the service must be RUNNING and joined afterwards]*
- **C17-n (W5.1 first half) uninstall keep data -> verify -> reinstall:** HUMAN Settings > Apps > MeshForge Agent >
  Uninstall > "also remove all data?" **No** (screenshot `windows-uninstall-data.png`); Administrator
  `uv run python lab\verify_uninstall.py --os windows` -> `CLEAN`; reinstall -> same node_id, online. The
  remove-all-data half is C10-z. EMIT SIGNAL: v017b-done L2. *[non-blocking, but L2 must be joined again
  before V1.0]*
- **LEAD CHECKPOINT V0.17b.** EMIT SIGNAL: V0.17b-done, merge `origin/main`, continue.

---

## Phase V1.0 - 72 h soak + evaluation + demo (`V1.0-soak.md`, `V1.0-campaign.md`)

- **C10-0 HUMAN BATCH (once, ~15 min):** mains power; Settings > System > Power: screen and sleep **Never**;
  Windows Update paused 1 week (renew if needed); the Administrator PowerShell for the chaos script stays open
  for 3 days. *[blocks: C10-c..-f]*
- **C10-a** L1's part Q stops L1 for ~4 h: L2 shows its controller offline: expected, do nothing. *[non-blocking]*
- **C10-b Facts for targets.json:** `Get-NetAdapter | Format-Table Name,Status` (A6; e.g. `Wi-Fi`), hostname,
  node_id. EMIT SIGNAL: soak-facts L2 iface=<name> node=<node_id>. *[blocks: L1's C10-c]*
- **C10-c WAIT: `SIGNAL: soak-plan`** (L1). Take only the plan files from L1's branch:
  `git fetch origin; git checkout origin/lab/L1 -- lab/reports/raw/<date>-V1.0-soak-plan` and copy its
  `plan.json` + `chaos-schedule.json` to `C:\mf-soak\RUN\` (RUN = the name in the signal). *[blocks: C10-d]*
- **C10-d B3 sampler** (own window, before t0):
  `uv run --extra agent python lab\soak.py run --dir C:\mf-soak\RUN --label L2 --dir-watch logs=C:\ProgramData\MeshForge\logs --dir-watch cache=C:\ProgramData\MeshForge\cache@51200`
  EMIT SIGNAL: soak-sampler-up L2. **B7 chaos** (HUMAN, Administrator PowerShell, repo root):
  `powershell -ExecutionPolicy Bypass -File lab\chaos_win.ps1 -RunDir C:\mf-soak\RUN -Label L2`
  (first line `cleanup L2: adapter ... enabled, fill file absent`). EMIT SIGNAL: soak-chaos-up L2.
  After a `reboot` action: HUMAN logs in, re-runs both commands (they resume). *[blocks: C10-e]*
- **C10-e (72 h + 30 min)** look twice a day (morning/evening): both windows alive; push a one-line REPORT.
  HUMAN `revoke_rejoin` if the chaos window prints `>>> MANUAL revoke_rejoin on L2` (V1.0-soak "Manual chaos
  actions": Leave + Join in the console with a code typed by the human; report the new node_id).
  Outside events (Windows update restart...) -> tell L1's human for `notes.json`. *[non-blocking]*
- **C10-f After the end:** the sampler prints `done: N ticks`. Copy `C:\mf-soak\RUN\samples.L2.jsonl` and
  `chaos-log.l2.jsonl` to `lab\reports\raw\<date>-V1.0-soak-L2\`, push. EMIT SIGNAL: soak-files-pushed L2.
  Then G3 cleanup: `powershell -ExecutionPolicy Bypass -File lab\chaos_win.ps1 -RunDir C:\mf-soak\RUN -Label L2 -Cleanup`
  (Administrator), `Get-NetAdapter` all enabled, no `C:\ProgramData\MeshForge\mf-chaos-fill.bin`; G4 power/updates
  back to normal (HUMAN). EMIT SIGNAL: soak-cleanup-done L2. *[non-blocking]*
- **C10-g Evaluation nights (L1's N2/M2) and demo:** stay online, unused, plugged in. *[non-blocking]*
- **C10-z (0.17.6 W5.1 second half) only when the Lead closes the campaign:** HUMAN uninstall with **Yes**
  (remove all data); Administrator `uv run python lab\verify_uninstall.py --os windows --data-removed` -> `CLEAN`;
  `Get-NetFirewallRule | Where-Object DisplayName -like '*MeshForge*'` prints nothing. *[last step]*
- **LEAD CHECKPOINT V1.0.** EMIT SIGNAL: V1.0-done. `## DONE`, push, tell the human "L2 finished - tell the Lead".

---
## LEAD NOTE (2026-11-01, after reading C2.5-a)
- **uv rebuilt .venv** from the human's PowerShell (managed-Python link problem, same as R1-2). After your S1
  measurements, and BEFORE any step that needs numpy/torch/the agent: run
  `uv python install 3.12 --reinstall` (report the output), then `uv sync --extra agent`, then
  `uv run python -c "import numpy, sys; print(numpy.__version__, sys.executable)"`. If the "Missing expected
  target directory" error appears again but the import works, record it and continue (non-blocking).
  Run all later `uv` commands from YOUR shell, not the human's, so one .venv is used.
- R1b-3 CUDA torch: do it after S1 as you planned, then `uv sync --extra agent` must not be rerun afterwards
  (it would remove torch) - if a later step needs `uv sync`, re-install torch after it.
- Windows Update pause ends 2026-11-07: remind the human to extend it before the V1.0 soak.
