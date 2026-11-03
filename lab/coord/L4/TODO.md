# L4 TODO - round 1: this laptop alone (V0.2, Wi-Fi only)

Read `lab/coord/RULES.md` first. Work on branch `lab/L4`. Write results to `lab/coord/L4/REPORT.md`.
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
- **R1-5 Hardware scores (0.2.11).** `uv run python lab/spikes/s7_hardware.py run --quick --name L4` then
  `uv run python lab/spikes/s7_hardware.py run --name L4` (1-5 min; add `--skip-gpu` if no NVIDIA GPU).
  Report the exit code and the scores printed on stderr. If it prints `!! ON BATTERY` or `thermal throttling`,
  ask the human to fix it and run again (report both runs).
- **R1-6 Environments (0.2.10), only if Docker is installed.** `uv run python lab/spikes/s6_environments.py run --quick --name L4`
  then `uv run python lab/spikes/s6_environments.py run --name L4`. No Docker: run it anyway (venv cases still
  run) and report the exit code and which cases were skipped.
- **R1-7 Push** everything (`lab/reports/` + REPORT.md), write `## DONE`, push, tell the human.

Round 2 (pairs of laptops: network S1, cross-OS S2, transport S3) comes after the Lead has everybody's IP.

---

> **LEAD NOTE 2026-11-03:** L4 starts now. The L3 laptop was replaced at the same time: the new L3 also does
> P0 + CU, so your "L3 -> L4" S1 pair is with the NEW L3 (read only the new `origin/lab/L3` REPORT.md), and you
> run S1 against the new L3 too when its `s1-serve-up L3` appears.

# Full campaign (V0.2 -> V1.0)

Written by the Lead's planning session, 2026-11-01. Overview, signals, durations: `lab/coord/CAMPAIGN.md`.
L4 = an **agent** laptop that **arrives later**; OS from your round 1 (R1-1). Use the **Windows** (native
PowerShell) or **Linux** (bash) lines. L1 = controller, 192.168.31.13 (check `SIGNAL: L1-ready`).
Because L4 is late, it starts with a **catch-up** (its V0.2 pairs + install + join) and then joins the phase the
others are in: read the newest `SIGNAL: Vx.y-done` on origin/lab/L1 and start at the next phase below.

## How to read this campaign

The same rules as `lab/coord/L3/TODO.md` "How to read this campaign" (run it all on your own; *[blocks]* /
*[non-blocking]*; HUMAN BATCH; LIVE; WAIT with `git fetch origin` + `git show origin/lab/L1:lab/coord/L1/REPORT.md`
every 2-3 min, tell the human after 30 min; EMIT SIGNAL lines; secrets typed by the human only; Wi-Fi only;
LEAD CHECKPOINT = signal + `git merge origin/main` + continue). Read that section once now. Stop/start/hard-kill
commands and log locations are the ones listed there.

---

## Phase P0 - preparation (~15 min)

- **C0-a** `git fetch origin`, `git checkout lab/L4`, `git merge origin/main`, `git rev-parse --short HEAD`,
  `uv sync --extra agent`. *[blocks: everything]*
- **C0-b HUMAN BATCH:** as L3 C0-b (power, Windows Update paused, Windows profile Private, clock).
  *[Private blocks: CU-a+]*
- **C0-c EMIT SIGNAL: L4-ready ip=<Wi-Fi IP> os=<windows|linux> host=<hostname>** *[blocks: the others' L4 pairs]*

---

## Phase CU - catch-up for V0.2 (~45 min)

- **CU-0 HUMAN BATCH (S1 firewall):** Windows Administrator:
  ```powershell
  New-NetFirewallRule -DisplayName "MeshForge S1 TCP" -Group "MeshForge lab" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 47201 -RemoteAddress 192.168.31.0/24 -Profile Private
  New-NetFirewallRule -DisplayName "MeshForge S1 UDP" -Group "MeshForge lab" -Direction Inbound -Action Allow -Protocol UDP -LocalPort 47201 -RemoteAddress 192.168.31.0/24 -Profile Private
  ```
  Linux (only if `sudo ufw status` is active): `sudo ufw allow from 192.168.31.0/24 to any port 47201`. *[blocks: CU-b]*
- **CU-a (0.2.4) Network facts:** Windows `netsh wlan show interfaces`, `Get-NetAdapter | ft Name,LinkSpeed`,
  `Get-NetConnectionProfile`; Linux `nmcli dev wifi`, `ip -4 addr` -> `lab/reports/raw/<date>-V0.2-network-facts/L4.txt`.
  *[non-blocking]*
- **CU-b (0.2.5) S1 server for the three pairs to L4:**
  `uv run python lab/spikes/s1_network.py serve --bind <L4-IP> --idle 3600 --duration 14400`.
  EMIT SIGNAL: s1-serve-up L4 ip=<L4-IP>. L1, L2, L3 each run `s1_network.py client --link wifi --server <L4-IP>`
  one after another: if their TODO already passed that step, their humans run it now (one at a time; the L1
  human coordinates; each laptop's lab agent pushes its JSON and writes `SIGNAL: s1-client-done <Lx>-to-L4`).
  WAIT: those three signals (or the ones whose laptop exists). Ctrl-C; HUMAN removes the rules (Windows
  `Remove-NetFirewallRule -DisplayName "MeshForge S1 TCP","MeshForge S1 UDP"`; Linux
  `sudo ufw delete allow from 192.168.31.0/24 to any port 47201`). *[non-blocking]*
- **CU-c (0.2.3)** GPU stack as L3 C2.3-a (only with an NVIDIA GPU). *[non-blocking]*
- **CU-d** push raw files (S1/S6/S7/inventory/facts). EMIT SIGNAL: v02-raw-pushed L4 (L1 then re-runs its
  report builds, C2.r-a/-b). *[non-blocking]*

---

## Phase V0.17a - install and join (~30 min)

- **C17-a Download + check** exactly as L3 C17-a. *[blocks: V0.17a+]*
- **C17-b WAIT: `SIGNAL: controller-up`** (L1); ask L1's human for an invite code. *[blocks: C17-c]*
- **C17-c HUMAN: install + join.** Windows: run `MeshForge-Agent-Setup-0.17.0-1.exe` (SmartScreen More info ->
  Run anyway, UAC Yes, Finish with Open MeshForge), join in the console with the human-typed code. If L3 was not
  Windows, **this is the timed criterion-1 run**: do it exactly as L3 C17-c (stopwatch, no terminal, recording).
  Linux: `sudo apt install ./meshforge-agent_0.17.0_amd64.deb`, app menu > MeshForge > join.
  *[blocks: every later phase]*
- **C17-d HUMAN: owner settings** as L3 C17-d (Run jobs on; Docker access or "Allow jobs without a container";
  GPU on if NVIDIA). *[blocks: jobs on L4]*
- **C17-e Check** as L3 C17-e. EMIT SIGNAL: installed L4 and SIGNAL: joined L4 node=<node_id>. *[blocks: later phases]*
- **C17-f (0.3.2, Windows only)** `uv sync --extra agent; uv run pytest tests/unit/agent -q -p no:cacheprovider`
  -> `passed`. *[non-blocking]*
- **LEAD CHECKPOINT V0.17a (L4).** EMIT SIGNAL: V0.17a-done, merge `origin/main`, continue at the phase the
  others are in.

---

## Phases V0.3 - V0.10 (only the ones not yet done by the others when L4 joined)

For each phase, do the matching L3 step with `L3` replaced by `L4` in the commands and signals, **except** the
L3-only roles (V0.17a timed run unless L3 was Linux, V0.7 parts C/R refusal + revoke victim, demo 05):
- V0.3: L3 C3.7-a/b (LIVE Wi-Fi drop / agent restart; L1 runs `watch --node <L4 host>`). *[non-blocking]*
- V0.4: L3 C4.0 (EMIT `clock-offset L4`), C4.15-g (Windows). *[non-blocking]*
- V0.5: L3 C5.11-a; **L4 is the part H victim** (L3 C5.11-g procedure, cache-folder rule included) and, on
  Windows, part I (L3 C5.11-h). *[non-blocking]*
- V0.6: L3 C6.13-c/d if the long job lands on L4. *[non-blocking]*
- V0.7: L3 C7.12-a (clock), C7.12-w/-x on L4 if Windows and L3 did not do them. *[non-blocking]*
- V0.8: optional E4 (a fifth machine join) is **not** done with L4 (it needs an identity reset). *[non-blocking]*
- V0.9: L3 C9.10-b (Linux only), C9.10-g (EMIT `policy-ok L4`). *[blocks: L1's C9.10-h]*
- V0.10: L3 C10.10-a (EMIT `policy-ok L4`). *[blocks: L1's C10.10-a]*
Write each step as `C<phase>-L4-<letter>` in REPORT.md. After the phase: EMIT SIGNAL: Vx.y-done, merge, continue.

## Phases V0.14, V0.15 - stay online, unused, on mains during L1's campaigns.

## Phase V0.17b - as L3's V0.17b (Windows W4.1 / Linux L3.2 + L4.1 + L5.1) on this laptop, only if L3 did
not already cover that OS. EMIT SIGNAL: v017b-done L4. *[non-blocking]*

## Phase V1.0 - soak, evaluation, demo

Exactly L3's V1.0 steps C10-0 to C10-g with `L3` -> `L4` (labels, file names `samples.L4.jsonl`,
`chaos-log.l4.jsonl`, signals `soak-facts L4`, `soak-sampler-up L4`, `soak-chaos-up L4`, `soak-files-pushed L4`,
`soak-cleanup-done L4`). **Not** C10-h (the demo laptop is L3). *[C10-0..C10-d block the soak start]*

- **LEAD CHECKPOINT V1.0.** EMIT SIGNAL: V1.0-done. `## DONE`, push, tell the human "L4 finished - tell the Lead".
