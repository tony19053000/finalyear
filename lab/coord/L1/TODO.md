# L1 TODO - round 1: this laptop alone (V0.2, Wi-Fi only)

Read `lab/coord/RULES.md` first. Work on branch `lab/L1`. Write results to `lab/coord/L1/REPORT.md`.
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
- **R1-5 Hardware scores (0.2.11).** `uv run python lab/spikes/s7_hardware.py run --quick --name L1` then
  `uv run python lab/spikes/s7_hardware.py run --name L1` (1-5 min; add `--skip-gpu` if no NVIDIA GPU).
  Report the exit code and the scores printed on stderr. If it prints `!! ON BATTERY` or `thermal throttling`,
  ask the human to fix it and run again (report both runs).
- **R1-6 Environments (0.2.10), only if Docker is installed.** `uv run python lab/spikes/s6_environments.py run --quick --name L1`
  then `uv run python lab/spikes/s6_environments.py run --name L1`. No Docker: run it anyway (venv cases still
  run) and report the exit code and which cases were skipped.
- **R1-7 Push** everything (`lab/reports/` + REPORT.md), write `## DONE`, push, tell the human.

Round 2 (pairs of laptops: network S1, cross-OS S2, transport S3) comes after the Lead has everybody's IP.

---

# Full campaign (V0.2 -> V1.0)

Written by the Lead's planning session, 2026-11-01. Overview, signals and durations: `lab/coord/CAMPAIGN.md`.
L1 = this laptop: Linux (Ubuntu), hostname `JARVISM3`, Wi-Fi `wlp0s20f3` 192.168.31.13/24, RTX 3050 Laptop GPU,
Docker installed. **L1 is the controller** (controller `.deb`) **and an agent** (agent `.deb`), and runs every
`compute` / `fabric_check.py` / report command unless a step says otherwise.

## How to read this campaign

- **Run it all on your own (RULES.md).** Start when round 1 (above) is DONE. Do the phases in order, without
  stopping after a step or at a checkpoint. Stop and talk to the human only for `HUMAN:` steps, a failure that
  the step marks as **blocking**, a WAIT longer than 30 minutes, or the end.
- **Blocking.** Every step says *[blocks: ...]* or *[non-blocking]*. A failed non-blocking step: write FAIL with
  the real output, push, go on. A failed blocking step: FAIL + BUG, push, skip only the steps it names (do the
  rest), and tell the human "Blocked at <step> - tell the Lead to read lab/L1".
- **HUMAN batches.** Each phase starts with one `HUMAN BATCH` step that collects the sudo / install / consent
  actions of that phase, so the human is called once. `LIVE:` steps still need the humans at that moment.
- **Separate clone.** The Lead session works in the main checkout on this laptop. Run this lab session in its
  **own clone** (`lab/coord/README.md` setup step 3 into a second folder), so `git checkout lab/L1` never
  switches the Lead's files.
- **Step format:** `id (roadmap task)` - commands - **Evidence** (where to save) - **OK when** (what passes).
  Write every step into REPORT.md (RULES.md format), commit and push after every step.
- **`HUMAN:`** = a physical, admin (`sudo`), password, browser, or policy action. Ask the human, wait for "done".
  Never type a password, join code, API token or Wi-Fi key yourself, never write them into any file.
- **`WAIT: <condition>`** = another laptop must be ready first. Check it with
  `git fetch origin && git show origin/lab/L2:lab/coord/L2/REPORT.md | grep SIGNAL` (change L2 to the laptop
  named). If the line is not there yet: check again every 2-3 minutes, meanwhile do later steps that do not
  depend on it; tell the human only after 30 minutes.
- **`EMIT SIGNAL: ...`** = write that exact line (filled in) on its own line in your REPORT.md entry, commit,
  push at once. Other laptops wait for it.
- **`LIVE:`** = a step where two humans act at the same moment (Wi-Fi off, agent stop, "now!"). Git is too
  slow for that: the humans talk to each other (same room / phone). The lab agents only run the tools.
- **L4 S1 catch-up (any time, 0.2.5).** When origin/lab/L4 shows `SIGNAL: s1-serve-up L4`, finish your
  current step, then (not during another measurement; first; laptops not present are skipped):
  `uv run python lab/spikes/s1_network.py client --link wifi --server <L4-IP>`, push the JSON,
  EMIT SIGNAL: s1-client-done L1-to-L4. Then go back to where you were. *[non-blocking]*
  After `SIGNAL: v02-raw-pushed L4`: repeat C2.r-a (with lab/L4) and C2.r-b, push.
- **Wi-Fi only (D425).** Every `--link` is `wifi`. Every Ethernet half of a checklist is **NOT RUN (D425)**:
  write that, never a pass. Lab subnet: `192.168.31.0/24`.
- **Laptop count.** L4 arrives later. Until `origin/lab/L4` has `SIGNAL: joined`, run every phase with the
  laptops you have and write `L4: not present` in the report. Phases marked **needs L4** wait for it.
- **LEAD CHECKPOINT** after each version: push, `EMIT SIGNAL: Vx.y-done`, `git fetch origin && git merge
  origin/main` (picks up Lead fixes), and **continue at once** - no message to the human, no waiting. The Lead
  patches this TODO when a fix is needed. Also merge `origin/main` every ~30 minutes.
- **The product under test** from phase V0.17a on is the **installed v0.17.0 package** (`compute`,
  `meshforge-agent`, `meshforge-controller` from `/usr/bin` / `/opt/meshforge`); the lab tools (`lab/*.py`)
  run from this clone with `uv run`. Older checklists were written for a dev checkout: their "start the
  fabric" part B is replaced by the installed fabric (rule from `lab/checklists/V1.0-campaign.md` Day L2):
  stop/start an agent = `sudo systemctl stop|start meshforge-agent` (Linux) or
  `Stop-Service|Start-Service MeshForgeAgent` (Windows, Administrator PowerShell); kill the controller =
  `sudo systemctl kill -s KILL meshforge-controller` (systemd restarts it after 5 s).
- Dates: `D=$(date +%F)` in every new terminal; evidence goes to `lab/reports/raw/$D-<topic>/`.

---

## Phase P0 - preparation (all laptops, ~30 min)

- **C0-a (all) Same commit, prerequisites.** *[blocks: everything (FAIL = stop, tell the human)]*
  ```bash
  git fetch origin && git checkout lab/L1 && git merge origin/main
  git rev-parse --short HEAD
  uv sync --extra agent --extra controller
  ```
  **Evidence:** REPORT.md. **OK when:** sync exit 0.
- **C0-b HUMAN: power and clock.** *[non-blocking (record it; fix before V0.3)]* Plugged in, Settings > Power > Automatic Suspend = Off, lid stays open.
  `timedatectl` must say `System clock synchronized: yes` (if not, HUMAN: `sudo timedatectl set-ntp true`).
  Print `date -u +%s` and record it. **OK when:** synced.
- **C0-c Firewall state.** *[non-blocking]* `sudo ufw status` (HUMAN types the sudo password). Record `active` / `inactive`.
  Every later "open port" step on L1 is needed **only if active**.
- **C0-d EMIT SIGNAL: L1-ready ip=192.168.31.13 os=linux host=JARVISM3** *[blocks: every WAIT on other laptops]* (re-check the IP with `ip -4 addr`
  first; if it changed, use the new one and tell the human: every other laptop uses this IP).

---

## Phase V0.2 - feasibility spikes on Wi-Fi (~5 h, source checkout, nothing installed)

Roadmap 0.2.1-0.2.7, 0.2.10, 0.2.11. Commands from `lab/README.md` and `lab/spikes/S*.md`.
Never run S1, S3 or S7 at the same time as another measurement.

- **C2.0-h HUMAN BATCH (once, ~5 min):** (1) agree to the downloads: CUDA torch ~2.5 GB (C2.1-a), S6 Docker
  images incl. the ~3.7 GB PyTorch image (C2.10-a); (2) only if `sudo ufw status` was `active`: type
  `sudo ufw allow from 192.168.31.0/24 to any port 47201`,
  `sudo ufw allow from 192.168.31.0/24 to any port 47202:47203 proto tcp`,
  `sudo ufw allow from 192.168.31.0/24 to any port 47301:47303 proto tcp`; (3) router: DHCP reservations yes/no
  (C2.4-a). The steps below then skip their own HUMAN parts. *[non-blocking: a "no" just marks that case SKIPPED]*

- **C2.1-a (0.2.1, 0.2.3) GPU stack on L1.** *[non-blocking (a FAIL is 0.2.3 evidence; C4.8-b may then fail too)]*
  ```bash
  nvidia-smi
  uv sync --extra agent --extra controller --extra gpu
  uv run python -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
  docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
  ```
  (`--extra gpu` downloads CUDA torch, ~2.5 GB: HUMAN agrees first.) **Evidence:**
  `lab/reports/raw/$D-V0.2-gpu/L1.txt` (all output). **OK when:** `nvidia-smi` shows the RTX 3050,
  torch prints `True`, the container prints the GPU table.
- **C2.4-a (0.2.4) Network facts.** *[non-blocking]* `nmcli dev wifi` (band, signal), `iw dev wlp0s20f3 link` (link speed),
  `ip -4 addr`. HUMAN: does the router allow DHCP reservations? If yes, the human reserves an address for
  every laptop (router admin page; never write its password anywhere). **Evidence:**
  `lab/reports/raw/$D-V0.2-network-facts/L1.txt`. **OK when:** band, signal, speed, IP, reservation yes/no
  recorded.
- **C2.10-a (0.2.10) S6 environments** *[non-blocking]* (Docker is here; S6.md §2; needs ~11 GB free for `docker-gpu-torch`,
  check `df -h /var/lib/docker`; HUMAN agrees to the downloads):
  ```bash
  uv run python lab/spikes/s6_environments.py run --quick --name L1
  uv run python lab/spikes/s6_environments.py run --name L1
  ```
  **Evidence:** the JSON under `lab/reports/raw/<date>-S6-environments/` (the tool names it). **OK when:**
  exit 0, or exit 3 with each skipped case typed (e.g. `insufficient_disk`) - write which.
- **C2.11-a (0.2.11) S7 again with CUDA torch** *[non-blocking]* (round 1 had no torch). Copy the old JSON aside first
  (`<name>__nonumpy.json` style, as L2 round 1b), then
  `uv run python lab/spikes/s7_hardware.py run --name L1`. **OK when:** exit 0, GPU fp32/fp16 TFLOP/s printed,
  no `!! ON BATTERY` / `!! thermal throttling suspected`.
- **C2.5-a (0.2.5) S1 server.** *[blocks: C2.5-b on the other laptops]* WAIT: `SIGNAL: L2-ready` (and L3-ready, L4-ready if they exist) - write their
  IPs into REPORT.md. If ufw is active, HUMAN:
  `sudo ufw allow from 192.168.31.0/24 to any port 47201`. Then, in its own terminal (keep it open):
  ```bash
  uv run python lab/spikes/s1_network.py serve --bind 192.168.31.13 --idle 3600 --duration 14400
  ```
  EMIT SIGNAL: s1-serve-up L1 ip=192.168.31.13.
- **C2.5-b (0.2.5) S1 client L1 -> all.** *[non-blocking for later phases]* WAIT: `SIGNAL: s1-serve-up` from L2 (and L3, L4 if present).
  ```bash
  uv run python lab/spikes/s1_network.py client --link wifi --server <L2-IP> --server <L3-IP> --server <L4-IP>
  ```
  (only the servers that exist; ~5 min each). **OK when:** exit 0, JSON in
  `lab/reports/raw/<UTC date>-S1-network/`. Push it. EMIT SIGNAL: s1-client-done L1.
  Then Ctrl-C the L1 server (no run targets L1: the six runs are L1 -> L2, L3, L4; L2 -> L3, L4; L3 -> L4) and
  HUMAN removes the 47201 rule if one was added. L4's pairs: the "L4 S1 catch-up" rule above.
- **C2.6-b (0.2.6) S2 L1 as server, L2 as client - do this one FIRST** (L2's client run must happen while L2 has no inbound rule). *[non-blocking]* If ufw active, HUMAN:
  `sudo ufw allow from 192.168.31.0/24 to any port 47202:47203 proto tcp`.
  ```bash
  uv run python lab/spikes/s2_crossos.py serve --bind 192.168.31.13 --name L1
  ```
  EMIT SIGNAL: s2-serve-up L1. WAIT: `SIGNAL: s2-done L2-to-L1` (L2's REPORT). Then Ctrl-C the server and HUMAN removes the 47202:47203 rule (C2.x-a line) **before** C2.6-a (L1 is then a client with no inbound rule).
  If L3 is Windows, repeat both with L3 after C2.6-a (signals `s2-serve-up L3`, `s2-done L1-to-L3`, `s2-done L3-to-L1`)
  - optional, a second Windows data point.
- **C2.6-a (0.2.6) S2 L1 as client, L2 as server** *[non-blocking]* (S2.md §3; L1 gets **no** inbound rule).
  WAIT: `SIGNAL: s2-serve-up L2` on origin/lab/L2 (L2 serves after its own client run, C2.6-b first).
  ```bash
  uv run python lab/spikes/s2_crossos.py client --server <L2-IP> --link wifi --name L1 --no-inbound-rule --note "<what you saw; no Defender prompt on Linux>"
  ```
  (~25-35 min). Push. EMIT SIGNAL: s2-done L1-to-L2.
- **C2.7-a (0.2.7) S3 L1 server, L2 client (Wi-Fi ladder up to 256 MiB, S3.md §2).** *[non-blocking]* Not while S1/S2 run.
  If ufw active, HUMAN: `sudo ufw allow from 192.168.31.0/24 to any port 47301:47303 proto tcp`.
  ```bash
  uv run python lab/spikes/s3_transport.py serve --bind 192.168.31.13
  ```
  EMIT SIGNAL: s3-serve-up L1. WAIT: `SIGNAL: s3-done L2` (one client run measures both directions).
  Ctrl-C the server. The Ethernet run: NOT RUN (D425).
- **C2.x-a Remove the spike firewall rules** *[non-blocking]* (if ufw active), HUMAN:
  `sudo ufw delete allow from 192.168.31.0/24 to any port 47201`,
  `sudo ufw delete allow from 192.168.31.0/24 to any port 47202:47203 proto tcp`,
  `sudo ufw delete allow from 192.168.31.0/24 to any port 47301:47303 proto tcp`. Record `sudo ufw status`.
- **C2.r-a Collect every laptop's raw files.** *[blocks: C2.r-b]* WAIT: `SIGNAL: v02-raw-pushed` from L2 (L3, L4 if present).
  ```bash
  git fetch origin
  git checkout origin/lab/L2 -- lab/reports/raw lab/reports/inventory      # repeat for lab/L3, lab/L4
  ```
  Never edit those files. Commit ("lab(L1): C2.r-a collect V0.2 raw").
- **C2.r-b Build the reports** *[non-blocking]* (each prints which hand sections it kept; `<date>` = the folder's date; label
  hostnames from REPORT signals):
  ```bash
  uv run python lab/spikes/s1_network.py matrix --results lab/reports/raw/<date>-S1-network --out lab/reports/<date>-S1-network.md --labels L1=JARVISM3,L2=<host2>,L3=<host3>,L4=<host4>
  uv run python lab/spikes/s2_crossos.py report --results lab/reports/raw/<date>-S2-crossos --out lab/reports/<date>-S2-crossos.md --hosts L1=L1,L2=L2
  uv run python lab/spikes/s3_transport.py report --results lab/reports/raw/<date>-S3-transport --out lab/reports/<date>-S3-transport.md
  uv run python lab/spikes/s6_environments.py report --help      # then run report as lab/spikes/S6.md §2 says
  uv run python lab/spikes/s7_hardware.py report --results lab/reports/raw/<date>-S7-hardware --out lab/reports/<date>-S7-hardware.md --labels L1=JARVISM3,L2=<host2>,L3=<host3>,L4=<host4>
  ```
  Do not write Goal/Conclusion/ADR text: the Lead does that (0.2.12). **OK when:** every report written; list
  every `INVALID`/`TBD` cell in REPORT.md (Ethernet columns are TBD by D425).
- **LEAD CHECKPOINT V0.2.** Push. EMIT SIGNAL: V0.2-done. Merge `origin/main`, continue (Lead: 0.2.12 consolidation;
  V0.17a does not depend on it).

---

## Phase V0.17a - install the fabric from the v0.17.0 release (~1.5 h)

Roadmap 0.17.2, 0.17.3, 0.17.4, 0.17.5 lab parts; `lab/checklists/V0.17-install.md` L1, L2 and
`V1.0-campaign.md` L1.2. Here **VC = L1** (not a fresh machine: say so in the report).

- **C17-0 HUMAN BATCH (~20 min, the human stays for C17-c..-h):** `sudo apt install` of both packages (C17-c,
  C17-h), the setup page + admin password (C17-d), ufw ports if active (C17-e), the `compute login` password
  (C17-f), the L1 join code + policy + "Give Docker access" (C17-h), then one invite per other laptop (C17-i).
  *[blocks: C17-c..-k]*
- **C17-a Download and check the packages.** *[blocks: all of V0.17a and later (a bad checksum = stop)]*
  ```bash
  mkdir -p ~/mf-pkgs && cd ~/mf-pkgs
  gh release download v0.17.0 --repo tony19053000/finalyear --dir ~/mf-pkgs
  sha256sum -c SHA256SUMS --ignore-missing
  ```
  **Evidence:** `lab/reports/raw/$D-V0.17-install/L1-download.txt`. **OK when:** every line `OK`.
- **C17-b Stop the old dev services.** *[blocks: C17-c]* The dev controller/agent/Compose DB must not run beside the packages:
  stop any `uv run meshforge-*` window (Ctrl-C), `systemctl --user stop meshforge-agent meshforge-controller`
  (ignore "not loaded"), `docker compose -f deploy/docker-compose.dev.yml down` (keeps its volume).
  **Q21 dev-DB import: NOT done** (default; it would put old runs into the planner's data - ask the Lead to
  change this step if wanted).
- **C17-c (0.17.3) Controller install.** *[blocks: every later phase (stop, BUG, tell the human)]* HUMAN: `sudo apt install ./meshforge-controller_0.17.0_amd64.deb`
  (the exact file name from `ls ~/mf-pkgs`). **OK when:** last lines contain `is running version 0.17.0` and
  `Create your fabric`. Save the last 15 lines.
- **C17-d (0.17.4) Create the fabric.** *[blocks: every later phase]* HUMAN: `sudo meshforge-controller setup url`, open the link in Firefox
  **on L1**, take screenshot `controller-setup.png` (before "Create the fabric") into
  `lab/reports/raw/$D-V0.17-install/`, create admin `labadmin` with a 12+ character password (the human keeps
  it; never in a file). The page shows the dashboard address and the CA fingerprint (not secret). Do **not**
  use the shown join code yet.
  **OK when:** "Laptops join at" = `https://192.168.31.13:47110/` (if it says `127.0.0.1`: follow
  `docs/user/create-a-fabric.md` §2 note, HUMAN edits `/etc/meshforge/controller.toml`).
- **C17-e Firewall** *[non-blocking (only needed if ufw is active)]* (only if ufw active), HUMAN:
  `sudo ufw allow from 192.168.31.0/24 to any port 47100,47101,47110 proto tcp` (ports from V0.7-security §0.3).
- **C17-f compute login + doctor.** *[blocks: every `compute` step]* HUMAN types the password at the prompt:
  `/opt/meshforge/controller/current/bin/compute login --controller https://192.168.31.13:47110 --fingerprint <fp> -u labadmin`;
  `sudo meshforge-controller doctor`. **OK when:** login OK, doctor `all checks passed`.
- **C17-g EMIT SIGNAL: controller-up url=https://192.168.31.13:47110 fp=<CA fingerprint>** *[blocks: the joins on L2-L4]*
- **C17-h (0.17.2) L1's own agent.** *[blocks: steps that use L1's agent (`--local`); not the others]* HUMAN: `sudo apt install ./meshforge-agent_0.17.0_amd64.deb`. Before:
  `uv run meshforge-agent identity show` (old dev node_id, if any). After: `meshforge-agent identity show`.
  App menu > **MeshForge** opens the console; HUMAN: dashboard Settings > Invites > New invite, type the code into
  the L1 console (never paste it into a file or chat). Policy: **Run jobs** on, **Let jobs use this laptop's GPU**
  on; then app menu > MeshForge > **Give Docker access** (HUMAN password). Screenshots of the console's
  Settings page and the Checks page -> `lab/reports/raw/$D-V0.17-install/L1-console-*.png`.
  **Evidence:** install's last 6 lines, `meshforge-agent identity show`, `meshforge-agent doctor`,
  `journalctl -u meshforge-agent -n 50 > lab/reports/raw/$D-V0.17-install/L1-agent-journal.txt`.
  **OK when:** node_id the same as the dev one (the package migrates it) or, if L1 had none, a new one;
  `compute nodes` shows L1 CONN `yes`. EMIT SIGNAL: joined L1 node=<node_id>.
- **C17-i Invites for the others.** *[blocks: the joins on L2-L4]* For each other laptop that has `SIGNAL: installed` in its REPORT, HUMAN
  creates a new invite on the dashboard and gives the code to that laptop's human (spoken or shown on screen).
- **C17-j Fabric up.** *[blocks: V0.3+ (with fewer laptops: continue, write which are missing)]* WAIT: `SIGNAL: joined` from L2 (and L3, L4 if present).
  `compute nodes > lab/reports/raw/$D-V0.17-install/fabric.txt`. **OK when:** every laptop `online`, CONN `yes`,
  version `0.17.0`. EMIT SIGNAL: fabric-up nodes=<n>.
- **C17-k (0.17.4/0.17.5) Job from L1 to each agent** *[non-blocking per laptop (a failed laptop is left out of V0.4+ until the Lead fixes it)]* (`docs/user/run-a-workload.md` `hello` project; same as
  V0.17-install L2.2): `compute run ./hello --node <Lx> -f` per laptop. **OK when:** each succeeds.
- **LEAD CHECKPOINT V0.17a.** Push, EMIT SIGNAL: V0.17a-done, merge `origin/main`, continue.

---

## Phase V0.3 - fabric core on real laptops (~1.5 h; `lab/checklists/V0.3-fabric.md` on the installed fabric)

Part B of the checklist is replaced by the installed fabric. All commands in one L1 terminal "C":
`export MESHFORGE_CLI__CONTROLLER_URL=https://192.168.31.13:47110` (fabric_check uses the `compute login`).

- **C3.15-a (0.3.15 c1, 0.3.3) Inventory - checklist part C.** *[non-blocking]*
  `uv run python lab/fabric_check.py --name L1 inventory`. Then write into
  `lab/reports/<date>-V0.3-fabric-inventory.md` "Expected vs actual" one line per laptop vs its
  `lab/reports/inventory/<host>.json` ("matches" or the exact difference). **OK when:** every row compared.
- **C3.7-a (0.3.7, 0.3.15 c2) Wi-Fi drop - checklist part D with Wi-Fi off instead of the cable.** *[non-blocking (exit 3 = FAIL recorded, keep going)]* For L2, L3,
  L4 one at a time (never L1):
  `uv run python lab/fabric_check.py --name L1 watch --node <HOSTNAME> --link wifi`
  LIVE: at `>>> PULL ...` the L1 human counts "3, 2, 1, now"; on "now" the other laptop's human **turns its Wi-Fi
  off** (tray icon) and the L1 human presses Enter; at `>>> PLUG ...` Wi-Fi on + Enter. 5 repetitions per laptop.
  **OK when:** exit 0 (OFFLINE <= 8 s, ONLINE <= 10 s, "Agent process restarted" = no). Wi-Fi reconnect can take
  longer than a cable: whatever is measured is the result.
- **C3.7-b (0.3.15 c4) Agent restart keeps node_id - part E.** *[non-blocking; a node_id change is a BUG but keep going]* For all laptops (L1 too):
  `uv run python lab/fabric_check.py --name L1 watch --node <HOSTNAME> --action agent --link wifi`
  LIVE: `>>> STOP` = that laptop's human runs `sudo systemctl stop meshforge-agent` (Linux) or
  `Stop-Service MeshForgeAgent` (Windows, Administrator PowerShell) + Enter on L1; `>>> START` = `start` /
  `Start-Service MeshForgeAgent` + Enter. **OK when:** every repetition back under the same node_id, restarted = yes.
- **C3.15-b (0.3.15 c3) Controller kill - part F.** *[non-blocking]*
  `uv run python lab/fabric_check.py --name L1 restart-check --link wifi`; at each `>>> KILL` HUMAN runs
  `sudo systemctl kill -s KILL meshforge-controller` in another terminal and does nothing else (systemd restarts it).
  **OK when:** exit 0, all nodes back <= 15 s, node_id kept, agent restarted = none.
- **C3.15-c (0.3.15 c5) 24 h soak: NOT RUN by default** *[non-blocking]* - superseded by the 72 h soak of phase V1.0
  (`V1.0-campaign.md` L3.2 "the 72 h run supersedes the 24 h one"). Write SKIPPED with that reason.
- **C3.x-a Evidence.** *[non-blocking]* Commit `lab/reports/<date>-V0.3-fabric-*.md` and `lab/reports/raw/<date>-V0.3-*/`.
- **LEAD CHECKPOINT V0.3.** EMIT SIGNAL: V0.3-done, merge `origin/main`, continue (0.3.2 / 0.3.13 are on the Windows laptops).

---

## Phase V0.4 - remote execution (~2 h; `lab/checklists/V0.4-remote-exec.md` parts C-G, installed fabric)

`<L1-ID>`... from `compute nodes`. Run from the clone's root (the example paths are relative).

- **C4.0 HUMAN BATCH:** agree to the ~4 GB image pull (C4.8-a); be ready for the LIVE checks with L2 (C4.15-e).
  *[non-blocking]*
- **C4.8-a (0.4.8) Pull the pinned images ahead** *[non-blocking (without the images C4.8-b may fail env_error)]* (checklist A3; ~4 GB, HUMAN agrees):
  `uv run python -c "from meshforge.agent.settings import ExecutionImages as I; i=I(); print(i.python_cpu); print(i.cuda_pytorch)"`
  then `docker pull <python_cpu digest>` and `docker pull <cuda_pytorch digest>`. Check `df -h` first (needs
  ~12 GB free). Note: the installed agent pulls with its own Docker access; this pre-pull only saves time.
- **C4.15-a Clock offsets** *[blocks: C4.15-d only]* (A4): `timedatectl`, `chronyc tracking 2>/dev/null || timedatectl timesync-status`.
  Record L1's offset in ms. WAIT: `SIGNAL: clock-offset` from L2 (L3, L4). Offset of Lx vs L1 = Lx - L1.
- **C4.15-b (c1) CPU example on every laptop - part C** *[non-blocking]* exactly as written (`--local`, then `--node <Lx-ID>`;
  the `compute jobs --json | python3 ...` line; `compute get` + `sha256sum outputs/<J>/results/result.json`).
  **Evidence:** `lab/reports/raw/$D-V0.4-exec/C.txt`. **OK when:** every digest
  `d85fca4c255d086a52b98aa22bc5aaa1ec00eb62f27b40cd1c0986e29b01c598`.
- **C4.15-c (c1) PyTorch on the CPU - part D1** *[non-blocking]* as written (`/tmp/pytorch-cpu` copy, `--local`, `--node` each,
  `compare.py`). **OK when:** `IDENTICAL` / `WITHIN TOLERANCE`; a laptop without Docker and without torch in the
  agent may end `failed (env_error)`: record it exactly (QUESTION for the Lead), not a pass.
- **C4.8-b (0.4.8) PyTorch on the GPU - part D2.** *[non-blocking]* L1: `compute run workloads/examples/pytorch-train --local -f`
  twice + `compare.py` (expect `IDENTICAL`); the same with `--node <L2-ID>` (RTX 2050; no Docker there, so the
  subprocess runtime needs torch in the agent: record what happens). **OK when:** L1's log ends `done on cuda:`
  and `run.json` names the RTX 3050.
- **C4.15-d (c2) Log latency - part E** *[non-blocking]* for L2, L3, L4:
  `J=$(compute run lab/workloads/log-ticker --node <Lx-ID>)` then
  `uv run python lab/exec_check.py latency $J --name Lx --clock-offset-ms <offset>`. **OK when:** `max_ms` <= 1000.
- **C4.15-e (c3) Cancel + timeout - part F** *[non-blocking]* on L2 (F1 `exec_check.py cancel`, F2 the 20 s timeout, exit 126).
  LIVE: L2's human checks Task Manager for a leftover python (L2 reports it). **OK when:** cancelled <= 5 s,
  timeout overshoot <= 2 s.
- **C4.x-a Evidence:** *[non-blocking]* `lab/reports/raw/<date>-V0.4-exec-check.jsonl` + the C/D/F outputs. Part G runs on the
  Windows laptops (WAIT: `SIGNAL: v04-g-done` from L2, and L3 if Windows).
- **LEAD CHECKPOINT V0.4.** EMIT SIGNAL: V0.4-done, merge `origin/main`, continue.

---

## Phase V0.5 - data plane on Wi-Fi (~2.5 h; `lab/checklists/V0.5-data-plane.md`)

Criterion 2 is an **Ethernet** criterion: **NOT RUN (D425)**; the Wi-Fi number is recorded. Part B config edits
are replaced: check `compute nodes <Lx-ID>` shows a `peer_endpoint` for each agent; if one has none, write a
QUESTION (the installed agent's peer `bind` setting) and skip parts G/H for it.

- **C5.0 HUMAN BATCH:** `sudo apt install iperf3`; if ufw active `sudo ufw allow from 192.168.31.0/24 to any port 5201 proto tcp`
  and `... port 47120 proto tcp`; ~15 GB of disk for the 5 GB dataset copies is OK; LIVE kill in C5.11-g.
  *[non-blocking]*
- **C5.11-a A2 + A3.** *[blocks: C5.11-c]* `df -h ~` (>= 20 GB free). HUMAN: `sudo apt install iperf3`; if ufw active:
  `sudo ufw allow from 192.168.31.0/24 to any port 5201 proto tcp` and port 47120 for the peer server.
- **C5.11-b Part C** *[blocks: C5.11-d..-h]* (the 5 GB dataset + reader job) exactly as written.
- **C5.11-c Part D2 (iperf3 Wi-Fi).** *[non-blocking]* `iperf3 -s` in its own terminal. EMIT SIGNAL: iperf3-server-up.
  WAIT: `SIGNAL: iperf3-done` from L2 (L2 runs the client and records the receiver Bitrate). Ctrl-C.
- **C5.11-d (c1) Part E** *[blocks: C5.11-e..-h (no dataset registered = stop V0.5, continue with V0.6)]* (push twice, change one file, push). **OK when:** the expected `uploaded ... skipped`
  and `registered ds5g@1/@2` lines.
- **C5.11-e (c2, Wi-Fi only) Part F** *[blocks: C5.11-f (L2 must hold the data)]* with `--node <L2-ID>`; compute throughput / iperf3 ratio. Record; no pass mark.
- **C5.11-f (c6) Part G first half** *[non-blocking]* (reader on L3, L2 holds the data): expect `transfer` lines from the
  controller and from L2. The second half (L1 on Wi-Fi, others on Ethernet): NOT RUN (D425).
- **C5.11-g (c3) Part H** *[non-blocking]* on L4 (or L3 if no L4; its cache must be empty first - the victim's TODO says how).
  LIVE: ~20 s after the victim's agent log shows `staging`, the victim's human stops its agent (Stop-Service /
  systemctl stop), waits 5 s, starts it. Then submit `J2` as written. **OK when:** `resumed_chunks=` > 0.
- **C5.11-h Part I (GC)** *[non-blocking]* only if the victim's TODO step for the config file succeeded; else SKIPPED + reason.
- **C5.11-i Part J** *[non-blocking]* (`compute node prepare --all ...` twice). **OK when:** second run all `hit`; a laptop
  without Docker typed `failed (env_error/docker_unavailable ...)`.
- **LEAD CHECKPOINT V0.5.** EMIT SIGNAL: V0.5-done, merge `origin/main`, continue.

---

## Phase V0.6 - placement and failures on Wi-Fi (~1.5 h; `lab/checklists/V0.6-scheduling.md` C, D, G)

Parts E (100 jobs + kills) and F (controller kill with 20 running) are **folded into the 72 h soak** of phase
V1.0 (`V1.0-campaign.md` Day L3: the soak load is a superset; it decides V0.6-c1). Write them SKIPPED with that
reason. The ledger workload of E/F needs writes outside the job folder, which the installed service does not allow.

- **C6.13-a (0.6.6) Part C network matrix.** *[non-blocking]* Wait 5 min after all agents are online, then
  `compute network` and `compute network --json > lab/reports/raw/$D-V0.6-sched/C-network.json`.
  **OK when:** every off-diagonal cell has a value; compare with the S1 Wi-Fi matrix (same order of magnitude).
- **C6.13-b (c4) Part D** *[non-blocking]* is submitted **from L2** (L2's TODO). WAIT: `SIGNAL: v06-d-done` from L2.
- **C6.13-c (G3) Part G1 partition.** *[non-blocking]* `~/mf-lab/v06-long` as written (`lab/workloads/v06-ledger/main.py`);
  `J=$(compute run ~/mf-lab/v06-long)`; `compute jobs $J` -> node `<X>`. LIVE: X's human turns Wi-Fi off 60 s,
  then on. The `execution_overlap` grep: `journalctl -u meshforge-controller --since "-30 min" | grep execution_overlap`.
  **OK when:** attempt 1 `lost`, attempt 2 elsewhere, job `succeeded` once.
- **C6.13-d Part G2 kill, no orphan.** *[non-blocking]* Same job; LIVE: hard-kill the agent where it runs (Linux:
  `sudo systemctl kill -s KILL meshforge-agent`; Windows: see that laptop's TODO). Before restarting: no
  `main.py 120` process left (`pgrep -af "main.py 120"` on Linux). **OK when:** no orphan, the job retries and succeeds.
- **LEAD CHECKPOINT V0.6.** EMIT SIGNAL: V0.6-done, merge `origin/main`, continue.

---

## Phase V0.7 - security (~2.5 h; `lab/checklists/V0.7-security.md` + G3/G4 extras)

Roles here: **L1** controller + tcpdump; **L2** Windows agent with Wireshark (the checklist's L2 "Linux agent"
parts that need Linux run on L1's own agent); **L3** = the **revoke victim** (part R; it comes back as a NEW node).
Section 0.4 (fabric create, users) is already done by V0.17a: only create `alice`.

- **C7.0 HUMAN BATCH:** `sudo apt install tcpdump tshark`; create `alice` on the dashboard and log in as alice /
  labadmin when asked (passwords typed by the human); the tcpdump `sudo` lines of C7.12-e/-h; owner-setting
  changes of C7.12-f; give invite codes to L3's human (C7.12-c, -d). *[blocks: C7.12-a..-i]*
- **C7.12-a Section 0.** *[blocks: C7.12-c..-i (needs alice)]* HUMAN: `sudo apt install tcpdump tshark`. Clocks: `date -u +%s`; WAIT:
  `SIGNAL: clock-unix` from the others (within 30 s of L1). Users: create a normal user `alice` on the
  dashboard (Settings > Users; HUMAN sets the password). Log in as alice in a second shell profile only when a
  part says "as alice" (`compute login ... -u alice`, HUMAN types the password). Record `compute whoami`.
- **C7.12-b Part P** *[non-blocking]* is done (V0.17a joins). Record `compute nodes` and each laptop's `identity show` from
  their REPORTs. WAIT: `SIGNAL: v07-icacls-done` from L2.
- **C7.12-c Part C (reuse, expiry, revoked, pin_mismatch)** *[non-blocking]* with the joins done on **L3** (its TODO). L1 makes
  the invites: `compute fabric invite` (dashboard also fine), the 1-minute one with the TTL option the
  dashboard/`compute fabric invite --help` offers (if none: SKIPPED + QUESTION), `compute fabric invite`
  list/revoke as `--help` names them. HUMAN gives codes to L3's human. **OK when:** the four typed refusals.
- **C7.12-d (c1) Part R revoke the busy L3.** *[non-blocking (L3 must come back as a new node before V0.8: its TODO)]* The sleep-300 project pinned to L3; LIVE: L1
  `date +%s.%N; compute node revoke <L3-ID>` while L3 watches its agent log. **OK when:** < ~2 s, attempt `lost`
  (`node_revoked`), job retried elsewhere; L3 then rejoins as a NEW node (its TODO). Update `<L3-ID>`.
- **C7.12-e (c3) Part X captures.** *[non-blocking; any marker count > 0 is a security BUG: report, keep going]* Markers and project on L1 as written. L1:
  `sudo tcpdump -i wlp0s20f3 -s0 -w l1.pcap 'tcp port 47100 or tcp port 47101 or tcp port 47110'`.
  EMIT SIGNAL: capture-start (L2 starts Wireshark: `l2.pcapng`, filter
  `tcp port 47120 or tcp port 47100 or tcp port 47110`). 2a enrolment capture: do the join on **L1** with a
  copy of its agent config pointing at another data dir (as written); if the installed `meshforge-agent join`
  has no `--config`, write SKIPPED + QUESTION. 2b/2c as written (`--node <L2-ID>`, then `--node <L3-ID>`, the
  sleep job + `compute cancel`). WAIT: `SIGNAL: capture-pushed` from L2 (its `l2.pcapng` under
  `lab/reports/raw/$D-V0.7-security/`). Run the step-3 loop over `l1.pcap` and `l2.pcapng` (the
  `login.json` path: the installed `compute` may store it elsewhere - `compute login --help`; adjust and say so).
  **OK when:** every count `0`, no `http`, TLS 1.3. **Never commit `/tmp/val`**; the pcaps go into the raw
  folder only if every marker count is 0.
- **C7.12-f Part L on L1's installed agent:** *[non-blocking]* `journalctl -u meshforge-agent | grep cpu_cgroup`;
  `systemctl --version | head -1`; with owner settings "Most CPU one job may use" = 25 % and "Allow jobs
  without a container" on (HUMAN, console), a 60 s busy-loop job pinned to L1 + `top`. **OK when:**
  `cpu_cgroup_ready` and ~25 %; or `cpu_cgroup_unavailable` with its reason recorded. Restore the settings.
- **C7.12-g Part E wheels-only** *[non-blocking]* once on L1 (Docker) and once on L2 (venv) - as written; the
  `[agent.envs] only_binary` change needs the agent config: SKIPPED + QUESTION if the installed config path is
  not known.
- **C7.12-h Part S 1-4 from L1** *[non-blocking]*, the job on L2 (`secrets: [LAB_KEY, {name: LAB_KEY2, as_env: true}]`);
  `tcpdump -i any -w g6.pcap ...` and the four greps. S5 runs on L2 (its TODO). **OK when:** all counts 0.
- **C7.12-i G4 extras on L1:** *[non-blocking]* `curl -k https://192.168.31.13:47110/v1/nodes` (expect 401) and `.../healthz` (200);
  part B owner rule (alice vs labadmin jobs) as written; G4 part 0.1 (dev DB password) NOT RUN (dev-only).
- **LEAD CHECKPOINT V0.7.** EMIT SIGNAL: V0.7-done, merge `origin/main`, continue.

---

## Phase V0.8 - dashboard visual review (~2 h; `lab/checklists/V0.8-visual.md` B-H)

A2 (fresh frontend build) does not apply: the installed controller serves its own build - record its version.

- **C8.0 HUMAN BATCH:** the whole team for part B (~45 min); CA trust in the browsers (C8.10-a); the two env
  variables of C8.10-e; the `controller.toml` edit + restarts of C8.10-g. *[non-blocking]*
- **C8.10-a A3/A4.** *[blocks: C8.10-c..-g (browser must trust the CA)]* `compute nodes`; HUMAN: `meshforge-controller ca trust-helper` (and `--os windows`, give
  the `--pem > ca.crt` file to L2's human). Make the browsers trust the CA - never click through a warning.
- **C8.10-b HUMAN: Part B** *[non-blocking (the team may do it another day)]* - the whole team reviews against brief §24 (~45 min). The lab agent writes the
  team's answers into `lab/reports/<date>-V0.8-visual.md` (template at the end of the checklist).
- **C8.10-c Part C Firefox on L1** *[non-blocking]* (C1-C7, keyboard only, as alice).
- **C8.10-d Part E1** *[non-blocking]* (Wi-Fi off on L3 instead of the cable; LIVE), **E2** (L2 sparkline; L2's human starts a
  heavy program).
- **C8.10-e (c2) Part E3 ui_perf on L1.** *[non-blocking]* `uv run playwright install chromium`; HUMAN sets
  `export MF_PASSWORD=...` and `export MF_TOKEN=...` (token from Settings > Tokens) in the terminal **without
  showing them** (no `echo`); run the `scripts/ui_perf` command with `--ca <the ca.crt from A4>` and
  `--out lab/reports/raw/<date>-V0.8-visual/ui-perf.json`. HUMAN revokes the token afterwards.
  **OK when:** `VERDICT: pass` (exit 0). It seeds 10 400 jobs into the lab DB: fine (planned).
- **C8.10-f Part E4** *[non-blocking]* optional (needs a fifth machine): SKIPPED unless L4 is new and not yet joined.
- **C8.10-g Part H** *[non-blocking, but the controller MUST be back on 47110 before going on]* port change: HUMAN edits the HTTP bind line in `/etc/meshforge/controller.toml` to port
  47120, `sudo systemctl restart meshforge-controller`, L2's browser checks 3/4; then back to 47110 and restart
  (step 5). If any agent's gRPC breaks, restore at once and record.
- **C8.10-h Part F projector** *[non-blocking]*: HUMAN, in the evaluation room - may be done later; record NOT RUN until then.
- **LEAD CHECKPOINT V0.8.** EMIT SIGNAL: V0.8-done, merge `origin/main`, continue.

---

## Phase V0.9 - benchmark campaigns (~2 h + the night; `lab/checklists/V0.9-campaign.md` + V1.0 N1)

**One overnight campaign serves V0.9, V0.14 and V0.15** (`V1.0-campaign.md` Day L4, N1 `lab-v1-data`).
`cpu.sort` and `split.csv_aggregate` are **held out**: never run them before phase V0.15's training. V0.9's own
`overnight.yaml` (O1) is **not** run separately.

- **C9.10-a P1** *[non-blocking]* runs on L2 (WAIT: `SIGNAL: v09-p1-done`).
- **C9.10-b C1 netem** *[non-blocking]* needs a Linux agent that is not L1: only if L3 or L4 is Linux (its TODO, with
  `lab/netem/README.md`); L1 then runs the `netem.yaml` campaign as written with `<NODE>` = that laptop.
  No such laptop: NOT RUN (no WSL agent in this lab).
- **C9.10-c C2 cpu_load on L2** *[non-blocking]* (`cpuload.yaml`, `nodes`/`origin` = L2). L2 runs the PowerShell checks (LIVE).
- **C9.10-d C3 gpu_load on L1** *[non-blocking]* (RTX 3050), `nvidia-smi` during a run; on L2 too if its agent advertises
  `gpu_load` (`compute bench plan` says so).
- **C9.10-e R1 owner pause** *[non-blocking]* (campaign on L1+L2; L2 runs `meshforge-agent bench pause|resume` in an
  Administrator PowerShell). **R3** Linux idle source on L1 (the `loginctl` command; the agent line from
  `journalctl -u meshforge-agent | grep owner_activity`). R2 is on L2.
- **C9.10-f B1** *[non-blocking]* Benchmarks page screenshots from L2's browser (L2's TODO).
- **C9.10-g (V1.0 L4.0.1-L4.0.2) Before the night.** *[`can train False` blocks: V0.15 and C10-h; not V0.9/V0.10]* `compute nodes` (all `online`); WAIT: `SIGNAL: policy-ok`
  from every agent laptop (`accept_jobs: True`, `pause_benchmarks: False`, mains power, sleep off). The ML check
  line of L4.0.2 (`compute planner ml models --json | python3 -c ...`): if `can train False` -> write it, **STOP
  phases V0.15 and V1.0 eval** and tell the Lead; V0.9's campaign still runs.
- **C9.10-h (0.9.10, V1.0 N1.1) Create and start `lab-v1-data`** *[blocks: C9.10-i, V0.14, V0.15]* - **needs all laptops present** (L4 if it
  will be part of V1.0; ask the Lead if L4 is still missing). `~/mf-eval/lab-v1-data.yaml` exactly as in
  V1.0-campaign N1.1 (`origin: <L1-ID>`, `session_label: wifi`), then `compute bench plan`, `create`,
  `start <CAMP_N> --per-node 1 --cooldown 2 --retries 2`. EMIT SIGNAL: night-1-started camp=<CAMP_N>.
  Overnight: **~5-7 h** with 4 laptops (plan estimate ~18 h for one). HUMAN: nobody uses the laptops.
- **C9.10-i (V1.0 D1.1 = V0.9 O3) Morning after.** *[non-blocking (record the numbers; V0.14/V0.15 still use the data)]* V0.9-campaign O3 with `<camp_id>` = `<CAMP_N>`.
  **OK when:** `done (complete)`, done >= 1000, `infra_failure_rate` < 0.02, `strict exit 0`, equal hashes.
- **LEAD CHECKPOINT V0.9.** EMIT SIGNAL: V0.9-done, merge `origin/main`, continue.

---

## Phase V0.10 - Mode 3 task splitting (~2.5 h; `lab/checklists/V0.10-mode3.md` 0, A-E)

- **C10.10-a Part 0.2** *[blocks: C10.10-b..-f]* `compute nodes`, SESSION_START; WAIT: `SIGNAL: policy-ok` from every agent laptop
  (fresh one for this phase). **0.3** the two `mode3_check.py build` folders.
- **C10.10-b (c1) Part A** *[non-blocking]* (`c1.yaml`, all laptops under `nodes`).
- **C10.10-c (c2, c3) Part B** *[non-blocking]* (single runs per laptop, the split run, `report`) - long: ~1 h.
- **C10.10-d (c4) Part C** *[non-blocking]* victim = **L2** (not L1). LIVE: when `compute watch K` is between 30 % and 70 %, L2's
  human hard-kills its agent (L2's TODO says how) and restarts it only after K ends.
- **C10.10-e (c5) Part D** *[non-blocking]* (`hf`, `compute network`, `explain H`) on Wi-Fi.
- **C10.10-f Part E1** *[non-blocking]* straggler on L1: the installed agent's chunk runs as the `meshforge` user, so HUMAN runs
  `sudo pkill -STOP -f run_chunk.py` / `sudo pkill -CONT -f run_chunk.py`; controller lines via
  `journalctl -u meshforge-controller | grep speculat`. **E2** hostile link on L1 (Docker) as written.
  **E3** owner guard on L2 (L2 runs pause/resume).
- **LEAD CHECKPOINT V0.10.** EMIT SIGNAL: V0.10-done, merge `origin/main`, continue.

---

## Phase V0.14 - planner on real laptops (~2 h; `lab/checklists/V0.14-planner.md` via V1.0 D1)

- **C14.8-a (V1.0 D1.2 = V0.14 A1) Calibrate:** *[blocks: C15.11-d]* `compute planner calibrate`, `compute planner calibration`.
- **C14.8-b V0.14 part D** *[non-blocking]* (one `local`, one `mode1`, one `mode3` job from `<CAMP_N>`: `compute explain` vs the
  dashboard) and **D2** Planner page screenshot.
- The V0.14 criteria 1-3 evaluation and the unseen workloads (0.14.8, part E) run **after ML training** (phase
  V0.15 steps C15.11-d/-f), because `split.csv_aggregate` must stay unseen until then.
- **LEAD CHECKPOINT V0.14 (partial).** EMIT SIGNAL: V0.14-done, merge `origin/main`, continue.

---

## Phase V0.15 - ML planner (~4 h + offline compare; `V0.15-ml-planner.md` via V1.0 D1.3-D1.9)

- **C15.11-a (V1.0 D1.3)** *[a held-out leak blocks: C15.11-b..-f (stop, tell the human); a rejected set does not]* V0.15 parts B1, C1, C2, C3, C4 with `lab-v015.*` names -> `lab-v1-data.*`.
  **STOP and tell the Lead** if the dataset lists `cpu.sort` or `split.csv_aggregate` rows.
- **C15.11-b (D1.4)** *[non-blocking]* `compute planner ml pin <SET>` (or "no ML set": write it).
- **C15.11-c (D1.5)** *[non-blocking]* V0.15 part E (the `lab-v1-latency` campaign; 1-2 h; all laptops online).
- **C15.11-d (D1.6, decides V0.14 c1-c3 and V0.15 c1-c2)** *[non-blocking]* the `compute planner eval` commands of D1.6 as
  written, then V0.14 part C1 and V0.15 part D2 on `lab-v1-eval.json`. One evaluation at a time.
- **C15.11-e (D1.7)** *[non-blocking]* V0.15 part D3 offline compare (`--out lab-v1-compare`; slow; fabric idle) + part F.
- **C15.11-f (D1.8 = 0.14.8)** *[non-blocking]* V0.14 part E1-E3 with `split.csv_aggregate` and `split.hash_files`, `<CAMP_A>` =
  `<CAMP_N>`.
- **C15.11-g (D1.9)** *[non-blocking]* copy `~/mf-eval/*.md`, `*.json`, `lab-v1-data.parquet.json`, `lab-v1-compare/` into
  `lab/reports/raw/<date>-V1.0-L4-eval/` (not the Parquet files). `record-lab` is **not** run here: the evidence is
  on v0.17.0, not on a release candidate; the Lead decides (CAMPAIGN.md questions).
- **LEAD CHECKPOINT V0.15.** EMIT SIGNAL: V0.15-done, merge `origin/main`, continue.

---

## Phase V0.17b - upgrade, rollback, uninstall (~3 h; `lab/checklists/V0.17-install.md` L3-L5, W2-W5)

WAIT: the Lead has written **"this build"** (a release newer than v0.17.0, or CI artefacts) into
`lab/coord/CAMPAIGN.md` §Builds (`git merge origin/main`, read it). Without it: SKIPPED, continue with V1.0.

- **C17-l L3.1 controller upgrade** *[blocks: C17-o]* (HUMAN `sudo apt install ./meshforge-controller_<this>_amd64.deb`; backup
  `pre-<this>.dump`; `compute jobs` still lists the old jobs).
- **C17-m L3.2 agent upgrade on L1** *[blocks: C17-n]* (same node_id, `meshforge-agent policy show`, no `.previous`).
- **C17-n L4.1 forced agent rollback on L1** *[non-blocking, but L1's agent must end healthy (`doctor` exit 0) before V1.0]* exactly as written (HUMAN runs the sudo lines).
- **C17-o L4.2 forced controller rollback** *[non-blocking, but `compute nodes` must show every laptop again before V1.0]* - **HUMAN decides first**: it restores the lab DB from the
  pre-upgrade backup. Only after `sudo meshforge-controller db backup` has made a fresh copy (create-a-fabric.md).
- **C17-p L5.1 on L1's agent: remove -> verify -> reinstall same node** *[non-blocking, but L1's agent must be joined again before V1.0]* (`sudo python3 lab/verify_uninstall.py
  --os linux` -> `CLEAN`, data kept). **Purges are NOT done now** (they would destroy the fabric before V1.0):
  they are step C10-z at the very end.
- Windows parts W2-W5 run on L2/L3 (their TODOs). WAIT: `SIGNAL: v017b-done` from L2 and L3.
- **LEAD CHECKPOINT V0.17b.** EMIT SIGNAL: V0.17b-done, merge `origin/main`, continue.

---

## Phase V1.0 - quiet checks, 72 h soak, evaluation matrix, demo (~6 days; needs L4)

WAIT: `SIGNAL: joined` from L4 (or a Lead note in CAMPAIGN.md that V1.0 runs with 3 laptops).

- **C10-0 HUMAN BATCH (one sitting before the soak, ~1 h):** stop/start the services for part Q (C10-a); the
  sleep `mask` line, the `mf-chaos` install + sudo rule, `db.pw` (C10-c, C10-e); mains power + Windows Update
  paused on every laptop for 4 days; later the `mf-eval-profile` rule (C10-h) and the demo recordings (C10-i).
  *[blocks: C10-c..-g]*
- **C10-a (V1.0 Part Q, evening, unattended, ~3-4 h)** *[non-blocking]* Q1, Q2, Q3 exactly as written in
  `V1.0-campaign.md` Day L0 part Q. HUMAN first: `sudo systemctl stop meshforge-agent meshforge-controller`
  (the other laptops show L1 offline: expected). Nothing else runs on L1. Afterwards HUMAN:
  `sudo systemctl start meshforge-controller meshforge-agent`; `compute nodes` all online again.
  Evidence: `lab/reports/raw/<date>-V1.0-L0-quiet/`.
- **C10-b (V1.0 L3.1)** *[non-blocking]* controller restart timing again on the final build:
  `uv run python lab/fabric_check.py --name L1 restart-check --link wifi` (kills as in C3.15-b).
- **C10-c (1.0.2) Soak setup - `V1.0-soak.md` part A on L1:** *[blocks: C10-d..-g]* A2 (HUMAN `sudo systemctl mask sleep.target
  suspend.target hibernate.target hybrid-sleep.target`), A3, A5 (HUMAN installs `lab/mf-chaos` + the sudo rule,
  the checks), A5b (`df --output=target /var/lib/meshforge/cas /var/lib/meshforge`, `df -h /var/lib/meshforge`,
  `stat -c '%U %a %F' /var/lib/meshforge`), A6 (`ip -br link` -> `wlp0s20f3`), A7, A9. A4 (SSH to L2) only for a
  **Linux** L3/L4 (`executor: ssh`, mf-chaos installed there too); L2 is Windows (`executor: windows`).
  A8 `~/mf-soak/targets.json` with one entry per laptop: L1 `"os": "linux", "executor": "local"`,
  `"iface": "wlp0s20f3"`; every Windows laptop `"os": "windows", "executor": "windows", "iface": "Wi-Fi"` (name
  from its `Get-NetAdapter`, in its REPORT). `controller_deb` var: the controller .deb under `/var/lib/mf-lab/`.
- **C10-d B1 plan:** *[blocks: C10-e..-g]* `uv run python lab/soak.py plan --out ~/mf-soak/RUN --targets ~/mf-soak/targets.json --seed <int> --start-in-min 30 --hours 72 --link wifi`
  (`RUN` = `<date>-V1.0-soak`). Copy `plan.json` + `chaos-schedule.json` to `lab/reports/raw/<date>-V1.0-soak-plan/`,
  commit, push. EMIT SIGNAL: soak-plan run=<RUN> t0=<t0> (the Windows laptops take the files from git).
- **C10-e B3-B6 on L1** *[blocks: C10-f, C10-g]* (each in its own terminal / tmux window that stays open): the L1 sampler with
  `--db-url ... --db-password-file ~/mf-soak/db.pw` (HUMAN writes db.pw; it never goes into git), the
  transition recorder (`fabric_check.py soak --hours 73 --link wifi --checkpoint ...`), `soak_load.py run`,
  `chaos.py run`. WAIT before t0: `SIGNAL: soak-sampler-up` and `SIGNAL: soak-chaos-up` from every Windows laptop.
- **C10-f (72 h + 30 min) C1-C3 during the soak.** *[non-blocking]* Twice a day: `uv run python lab/chaos.py show --dir ~/mf-soak/RUN`,
  every terminal alive; HUMAN writes outside events into `~/mf-soak/RUN/notes.json`. Manual `revoke_rejoin`:
  as V1.0-soak "Manual chaos actions" (codes typed by humans only). Push a short REPORT line each morning/evening.
- **C10-g D1-D4 judge.** *[non-blocking]* `soak_load.py collect`; WAIT: `SIGNAL: soak-files-pushed` from every Windows laptop,
  then `git checkout origin/lab/Lx -- lab/reports/raw/<date>-V1.0-soak-Lx/` and copy `samples.Lx.jsonl` /
  `chaos-log.lx.jsonl` into `~/mf-soak/RUN`; `uv run python lab/soak.py report ~/mf-soak/RUN`.
  **OK when:** `VERDICT: PASS`. Part E: copy RUN (without db.pw) to `lab/reports/raw/<date>-V1.0-soak/`. Part G
  cleanup on L1 (G1, G2, G4) after every laptop's `SIGNAL: soak-cleanup-done`.
- **C10-h (1.0.6) V1.0 L4.0.3 + N2 + M2.1 policy matrix (night 2, ~8-11 h; then ~1.5 h).** *[non-blocking]* As written:
  the bundle `$PY`, the `mf-eval-profile` sudo rule (HUMAN), the smoke run, `v1-matrix.yaml` with
  `link: wifi`, the run (`--profile-hook ...`), then M2.1 (`v1-ablation-wifi`). M2.2 Ethernet: NOT RUN (D425,
  A7 dropped). M2.3 cleanup, M2.4 copy raw folders into `lab/reports/raw/<date>-V1.0-L4-eval/raw/`.
  EMIT SIGNAL: night-2-started before, policy-matrix-done after.
- **C10-i (1.0.7) Day L5 demo rehearsal × 2** *[non-blocking]* (`scripts/demo/README.md`): L5.1 dry runs, L5.2 two rehearsals
  (05 needs a Windows laptop that is not joined: L3's TODO uninstalls with data removed = W5.1 second half),
  L5.3 recordings (not in git), L5.4 slide check. HUMAN runs the recordings.
- **C10-j V1.0-campaign §10 end:** *[non-blocking]* `uv sync --all-extras --group evaluation && (cd frontend && npm ci && npm run build)`;
  `uv run python scripts/release_check.py --only lab` (report only; the Lead decides what counts). Undo lab
  settings: `sudo systemctl unmask sleep.target suspend.target hibernate.target hybrid-sleep.target`, no
  `mf-chaos` / `mf-eval-profile` in `ls /etc/sudoers.d/`, no spike rules in `sudo ufw status`.
- **C10-z (0.17.6, V0.17-install L5.2) Final purges - only when the Lead says the campaign is closed.** *[last step]*
  HUMAN: `sudo apt purge meshforge-agent` -> `sudo python3 lab/verify_uninstall.py --os linux --package agent --data-removed`
  (controller still running); then `sudo apt purge meshforge-controller` -> `sudo python3 lab/verify_uninstall.py --os linux --data-removed`.
  **OK when:** every verifier `CLEAN: nothing left behind`.
- **LEAD CHECKPOINT V1.0.** EMIT SIGNAL: V1.0-done. Write `## DONE` in REPORT.md, push, tell the human
  "L1 done - tell the Lead to read lab/L1".
