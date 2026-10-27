# lab/

Physical-lab tooling and evidence for the MeshForge fabric. The human summary lives in [`docs/LAB.md`](../docs/LAB.md).

```
lab/
  inventory.py      hardware/software inventory → JSON   (task 0.2.2)
  _inventory/       helpers for inventory.py (parsers, probes, privacy filter)
  fabric_check.py   V0.3 fabric checks over REST /v1: inventory, watch, restart-check, soak (0.3.15)
  agent_rss.py      V0.3 soak: the agent process's RSS on each laptop (0.3.15)
  sched_check.py    V0.6 scheduling lab: submit the mixed job set, wait, check the ledger (0.6.13)
  _sched/           its verdict (lost / duplicated / partition overlaps), pure
  mode3_check.py    V0.10 Mode 3 lab: build a split job of any size, verify an output, criteria 2-3 report (0.10.10)
  _mode3/           its criteria 2-3 arithmetic (D274), pure
  _fabric/          helpers for both (stdlib REST client, timeline maths, checkpoints, reports)
  soak.py           V1.0 72 h soak: plan (seeded load + chaos), run (per-laptop sampler), report (D410 rules) (1.0.2)
  soak_load.py      V1.0 soak: the seeded mixed job load + the final collect (1.0.2)
  chaos.py          V1.0 soak: the seeded chaos schedule over SSH / locally (1.0.2)
  chaos_win.ps1     V1.0 soak: the same schedule's actions on a Windows laptop (1.0.2)
  mf-chaos          V1.0 soak: root-owned chaos wrapper (validated args, trap undo, cleanup) for sudo on Linux laptops (1.0.2)
  _soak/            their plan, load mix, chaos catalogue, sampler, D410 rules, report; procedure: checklists/V1.0-soak.md
  checklists/       step-by-step human procedures, e.g. V0.3-fabric.md
  spikes/           feasibility spikes S1–S7              (tasks 0.2.5–0.2.11)
    _harness/       shared by every spike: reps runner, stats, result JSON, report
    s1_network.py   S1 network matrix (serve / client / matrix)
    _s1/            S1 measurement code (plain functions, no CLI)
    s2_crossos.py   S2 cross-OS reachability (serve / client / report)
    _s2/            S2 HTTP + gRPC server, probes, WSL/OS facts, verdicts
    S2.md           S2: what is measured, how to run, how to read it, limits
    s3_transport.py S3 transport throughput (serve / client / report)
    _s3/            S3 senders (raw TCP, gRPC, HTTP) + client, payload, report tables
    S3.md           S3: what is measured, how to run, how to read it, limits
    s7_hardware.py  S7 hardware scores (run / report)
    _s7/            S7 benchmarks (child process), sizing, scores, report
    S7.md           S7: what is measured, formulas, how to run, limits
    sp2_managed_pg.py  SP2 (0.17.0) controller-managed PostgreSQL 16: bundle, lifecycle, crash, backup
    _sp2/           SP2 bundle fetch/verify/trim, crash child, scenarios
    SP2.md          SP2: binary source, sizes, crash design, results, what G2 must do
  reports/          one markdown report per lab run
    TEMPLATE.md     copy this for every report
    raw/            unedited command output, per run and per host
    inventory/      <hostname>.json from inventory.py
```

## inventory.py

Run it from the repo root after a fresh clone and a plain `uv sync` (no extras needed; no admin rights needed).

```bash
# Linux / WSL (bash)
uv run python lab/inventory.py --out "lab/reports/inventory/$(hostname).json"
```
```powershell
# Windows (PowerShell), native, not inside WSL
uv run python lab/inventory.py --out "lab/reports/inventory/$env:COMPUTERNAME.json"
```

| Flag | Meaning |
|---|---|
| `--out PATH` | write the JSON to `PATH` (parent folders are created). Without it, the JSON goes to stdout. |
| `--pretty` | indent the JSON (keys are always sorted, so output is deterministic) |
| `--no-summary` | do not print the short human summary to stderr |
| `--timeout SECONDS` | wall-clock budget for the whole run, 5–3600, default `60`. Each command gets its own cap or the time left, whichever is smaller. When the budget runs out, remaining commands are not started and show up as `skipped: run deadline exceeded` errors. Use a larger value on a very slow machine, for example when the first `import torch` takes minutes. |

**Exit codes:** `0` = inventory written, **even if some probes failed** (a failure is data, e.g. `nvidia-smi` not working); `1` = the output file could not be written; `2` = bad command-line usage.

**What it collects** (one section each): `git` (short commit, dirty flag), `os`, `wsl`, `cpu`, `memory`, `disks`, `gpus` (every GPU incl. integrated/non-NVIDIA), `nvidia` (nvidia-smi / NVML status and error text), `secure_boot`, `python`, `uv`, `torch` (version, CUDA build, `cuda.is_available()`), `docker` (client/server, reachable, runtimes incl. `nvidia`; no containers are started), `network` (adapter kind Wi-Fi/Ethernet, MAC, private IPv4, link speed, up/down), `power` (AC/battery), `tools` (git, node, npm, gh, iperf3, protoc, psql).

Every section looks like `{"ok": true, "source": "lscpu", "value": {...}}` or `{"ok": false, "source": "...", "error": "..."}`. `failed_sections` lists the failed ones. **`problems`** is a sorted list of short sentences about anything a human must fix, even when the probe itself worked. It covers an NVIDIA GPU whose driver is not working, a CUDA build of torch where `cuda.is_available()` is false, Docker installed but its daemon unreachable, and every failed section. The stderr summary prints it. Check `problems`, not only `failed_sections`: the `nvidia` section is `ok: true` with `driver_working: false` on a machine whose driver is blocked. `LAB_FIELDS` in `_inventory/model.py` maps each `docs/LAB.md` §1 row to the JSON fields that answer it.

**Sources:** standard library first. psutil and nvidia-ml-py are used when installed (`--extra agent`). torch is used when installed (`--extra gpu`). Otherwise the script falls back to OS tools. On Linux these are `/proc`, `/sys`, `lscpu`, `ip -j`, `lspci -mm` and `mokutil`. On Windows it is one PowerShell `Get-CimInstance` / `Get-NetAdapter` call. `nvidia-smi` is used on both. Each command has its own timeout (15 s by default) and the whole run has a budget (`--timeout`). A command runs in its own process group, and a timed-out command is killed together with its child processes, so nothing is left running. A tool that timed out once is skipped for the rest of the run. No shell is used. torch and NVML run in a child process, so a broken GPU stack cannot hang or crash the run.

**Privacy (docs/LAB.md §6):** no Wi-Fi SSIDs or keys, usernames, serial numbers, environment variables or public IPs are collected. The home directory is written as `~` and other user folders as `<user>`. Public IPv4 addresses become `<public-ip>`. Secret-looking text is scrubbed with `meshforge.common.redaction`. MAC addresses, private IPv4 addresses and the hostname are kept on purpose. Known gap: a 4-part version string that looks like a public IP (for example `1.2.3.4`) is masked too.

**Windows notes:** Secure Boot is read from the registry, which is readable without admin. If the registry can't be read, `Confirm-SecureBootUEFI` is tried, and it usually reports `unknown` without admin. `Win32_VideoController.AdapterRAM` caps at 4 GiB, so for NVIDIA GPUs the nvidia-smi row replaces it. Integrated GPUs keep the CIM value, with a `vram_note` when it is capped. CPU `max_mhz` is `null` on Windows because turbo clocks are not exposed; `base_mhz` is the rated clock.

## Spikes (lab/spikes/)

Every spike is a script in `lab/spikes/` that runs after a plain `uv sync` (standard library only unless the spike needs torch/Docker) and uses the shared harness in `spikes/_harness/`:

- **Repetitions:** `warmup` + `reps` per case (default 1 + 5). Warmup samples are kept in the JSON but never enter the summary. A failed repetition is recorded with a typed `failure_type`; a fatal one (peer gone) stops that case at once.
- **Statistics:** n, mean, sample stdev, min, max, median, p95 per metric. A value with n < 5 is **INVALID** in every table.
- **Result JSON** (one per host and run): `schema_version`, `spike`, `topic`, git commit + dirty flag, host (hostname, OS, label), UTC timestamp, `link` (`wifi`/`ethernet`, given by the operator), all parameters, raw samples, summaries, and errors. Keys are sorted, so the output is deterministic. Home paths and public IPs are masked with the inventory privacy filter. Files go to `reports/raw/YYYY-MM-DD-<topic>/<host>__<suffix>.json`.
- **Report:** generated from `reports/TEMPLATE.md`. The header, machines table, raw-output location, n and the Results table (INVALID rows and every failed repetition listed) are filled in automatically. Goal, conclusion and decisions are written afterwards.
- **Re-running `report --out <existing file>` is safe:** it **merges**. The generated regions (title and `> File:` line, the host table rows, `Raw output location`, the whole `Results` section including the extra tables and the notes list) are refreshed; the hand-written ones (`Goal`, the prose after the host table, `Commands run`, `Expected vs actual`, `Conclusion`, `Decisions triggered`) are kept from the file on disk, and the tool prints which ones it kept. Write your own sub-headings as `###`: an extra, missing, duplicated or reordered `##` heading makes the merge **refuse** (exit 1) and leave the file untouched, naming what did not match. Anything written *inside* a generated region is replaced — including hand-filled link speed / power-mode cells of the host table. If you add your own table under the host table, put a **blank line** before it: a bare `|` row with exactly the host table's shape and no `|---|` row under it is indistinguishable from a generated row and is replaced. `--force` regenerates the whole file from the template and destroys every hand-written section. Writes are atomic: a refused or failed run leaves the old file byte-identical.

### s1_network.py (spike S1, task 0.2.5)

Measures, for each pair of laptops: TCP bandwidth in **both directions** (row = sender), with 1 and 4 parallel streams, bytes counted at the receiver; and UDP echo RTT (mean ± stdev), jitter and loss. It is our own Python tool, so it needs no extra install on Windows. iperf3 is an optional cross-check (`--iperf3`, needs `iperf3 -s` running on the server).

**Before you start:** all four laptops are on the same link (for the Wi-Fi matrix, unplug the Ethernet cables; for the Ethernet matrix, use the Ethernet IPs and turn Wi-Fi off, so the traffic cannot take the other path), plugged in, and in performance mode. The JSON records the local IP used (`params.local_address`), so you can check which link was used.

**1. Firewall (only for the spike, remove it afterwards).** The server listens on TCP **and** UDP port `47201`. Replace `192.168.1.0/24` with the lab subnet.

```powershell
# Windows, PowerShell as Administrator. Network profile must be Private (task 0.2.4).
New-NetFirewallRule -DisplayName "MeshForge S1 TCP" -Group "MeshForge lab" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 47201 -RemoteAddress 192.168.1.0/24 -Profile Private
New-NetFirewallRule -DisplayName "MeshForge S1 UDP" -Group "MeshForge lab" -Direction Inbound -Action Allow -Protocol UDP -LocalPort 47201 -RemoteAddress 192.168.1.0/24 -Profile Private
# afterwards:
Remove-NetFirewallRule -DisplayName "MeshForge S1 TCP","MeshForge S1 UDP"
```
If Windows shows a "Windows Security Alert" for Python the first time, tick **Private networks** only and click **Allow**. Do not press Cancel, which can create a *block* rule for python.exe that overrides the rule above. Check with `Get-NetFirewallRule -Direction Inbound -Action Block -Enabled True | Where-Object DisplayName -like "*python*"`.
```bash
# Linux: only needed if ufw is active (sudo ufw status)
sudo ufw allow from 192.168.1.0/24 to any port 47201
# afterwards:
sudo ufw delete allow from 192.168.1.0/24 to any port 47201
```

**2. Start a server on every laptop** (bound to that laptop's lab IP on the link being measured):
```bash
uv run python lab/spikes/s1_network.py serve --bind 192.168.1.20
```
It refuses `0.0.0.0`/`::` and public addresses (override: `--allow-any`), answers only private/loopback/link-local clients, stops by itself after `--idle` seconds without clients (default 600) or `--duration` seconds in total (default 3600), and stops cleanly on Ctrl-C. It holds at most 32 TCP connections. A new connection must send its complete request header within `--header-timeout` seconds (default 2), or it is dropped. When all 32 slots are full, the oldest connection that has not sent a header yet is evicted to make room. A running measurement is never evicted, so idle or slow peers cannot lock real clients out.

**3. Run the clients one at a time** (never two at once, because they would share the link). Six runs cover all 12 directions and 6 pairs of four laptops: L1 → L2, L3, L4; then L2 → L3, L4; then L3 → L4. `--server` can be repeated; servers are measured one after another.
```bash
uv run python lab/spikes/s1_network.py client --link wifi --server 192.168.1.21 --server 192.168.1.22 --server 192.168.1.23
```
Each client run first checks the loopback ceiling: the highest rate this tool reaches on this laptop with no network. Python counts bytes in user space and may be the limit above about 1 Gbit/s. Per server it takes about 5 minutes with the defaults (`--reps 5 --warmup 1 --seconds 10 --streams 1,4 --pings 200 --ping-interval 0.02`). It writes `lab/reports/raw/<UTC date>-S1-network/<client>__to-<server>_<link>.json`.

**4. Build the report** after copying every laptop's JSON into one folder (Wi-Fi and Ethernet files may share the folder; they are never mixed in a table):
```bash
uv run python lab/spikes/s1_network.py matrix --results lab/reports/raw/2026-09-20-S1-network --out lab/reports/2026-09-20-S1-network.md --labels L1=<host1>,L2=<host2>,L3=<host3>,L4=<host4>
```
The report holds the Results table and both docs/LAB.md §2 matrices per link. Cells show mean ± stdev (n); n < 5 is `INVALID (n=k)`; a pair nobody measured is `TBD`. Runs that cover the same direction (A measured B and B measured A) are pooled. Then write Goal/Conclusion and copy the matrices into docs/LAB.md §2.

**Exit codes:** `0` complete · `1` cannot bind/write · `2` bad usage (e.g. wildcard bind) · `3` results written but incomplete (server lost, failed repetitions: see `errors` in the JSON) · `130` Ctrl-C.

**How to read the numbers (honesty notes):**
- Bandwidth is TCP goodput counted **at the receiver**, from the first byte to the last byte on the receiver's clock. The sender view is recorded too; the report lists every cell where the two differ by more than 5 %.
- Streams start together after all connections are open, so no data is queued before the measured window.
- Buffers: the app sends 128 KiB chunks and receives into a 256 KiB buffer. The kernel socket buffers are left to OS autotuning; their initial sizes are recorded in `extra.buffers`.
- RTT is measured on the **idle** link, before the TCP tests. Jitter = mean |RTT(i) − RTT(i−1)| of consecutive replies (RFC 3550's D without the 1/16 smoothing). The smoothed RFC 3550 value is kept as `jitter_rfc3550_ms`. Loss counts pings without a reply within `--ping-timeout`.
- A cell above 50 % of a host's loopback ceiling is flagged as possibly tool-limited. Confirm it with `--iperf3`.

The measurement code in `_s1/` (`client.tcp_throughput`, `client.udp_rtt`, `server.S1Server`) has no CLI coupling, so the V0.3 agent network probe can adopt it.

### s2_crossos.py (spike S2, task 0.2.6)

Answers, for one **ordered pair** of laptops: can a **native, non-WSL** Python process on the far machine be reached over **HTTP** and over **gRPC**, does a long-lived **agent-initiated bidirectional gRPC stream** (ADR-0002) survive idle gaps with keepalive on *and* off, and does the **outbound-only** model work from Windows with **no inbound firewall rule on the client**? That is the evidence for ADR-0009 (native Windows agent) and ADR-0002. Standard library + `grpcio`; no admin rights needed on the client side. Details, the WSL refusal, the verdict rules and the limits: [`spikes/S2.md`](spikes/S2.md).

**Before you start:** both laptops on the same link, Windows network profile **Private** (the tool warns if an adapter is Public), and the tool **refuses to run inside WSL** (exit `4`, override `--allow-wsl` for a deliberately-WSL data point) — WSL2's NAT would answer a different question. One run measures **one direction**; swap the roles and run it again for the other. About 25–35 minutes per run with the defaults.

**1. Firewall — only the *server* laptop needs a rule.** One `serve` process opens **TCP 47202 (HTTP) and 47203 (gRPC)**, no UDP. The laptop running `client` needs **no inbound rule at all**: adding one would destroy the evidence for ADR-0002. Replace `192.168.1.0/24` with the lab subnet.

```powershell
# SERVER laptop only. Windows, PowerShell as Administrator. Profile must be Private (task 0.2.4).
New-NetFirewallRule -DisplayName "MeshForge S2" -Group "MeshForge lab" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 47202-47203 -RemoteAddress 192.168.1.0/24 -Profile Private
# afterwards:
Remove-NetFirewallRule -DisplayName "MeshForge S2"
```
```bash
# SERVER laptop only. Linux: needed only if ufw is active (sudo ufw status)
sudo ufw allow from 192.168.1.0/24 to any port 47202:47203 proto tcp
# afterwards:
sudo ufw delete allow from 192.168.1.0/24 to any port 47202:47203 proto tcp
```
The same "Windows Security Alert" warning as for S1 applies on the server: tick **Private networks** and **Allow**, never Cancel. A prompt on the **client** is itself a finding — record it in `--note` and allow nothing.

**2. Start the server** on the laptop that accepts connections, bound to its lab IP:
```bash
uv run python lab/spikes/s2_crossos.py serve --bind 192.168.1.20 --name L1
```
```powershell
# Windows (PowerShell, native, not inside WSL) — same command
uv run python lab/spikes/s2_crossos.py serve --bind 192.168.1.20 --name L2
```
It refuses `0.0.0.0`/`::` and public addresses (override: `--allow-any`), answers private/loopback/link-local peers only, and stops by itself after `--idle` seconds without a request (default 900) or `--duration` seconds in total (default 7200), or on Ctrl-C / SIGTERM. On the **Windows** server, once: start a second `serve` with the same `--bind` while the first runs and paste back that it exits `1` with `cannot listen` (that is the port-exclusivity check on Windows).

**3. Run the client** on the other laptop (this machine gets no firewall rule):
```bash
uv run python lab/spikes/s2_crossos.py client --server 192.168.1.20 --link ethernet --name L1 --no-inbound-rule --note "no Defender prompt appeared"
```
```powershell
# Windows (PowerShell, native, not inside WSL) — same command
uv run python lab/spikes/s2_crossos.py client --server 192.168.1.20 --link ethernet --name L2 --no-inbound-rule --note "no Defender prompt appeared"
```
Defaults: `--reps 5 --warmup 1 --pause 0.5 --blob-bytes 33554432 --hold 5 --ping-interval 1 --idle-gaps 30,120 --keepalive both --connect-timeout 5 --io-timeout 20`. `--no-inbound-rule` is the operator's statement that no inbound rule exists here; without it the outbound-only verdict stays `TBD`. `--idle-gaps 30,120,300` tests longer idles, `--keepalive off` isolates the keepalive question, `--out-dir DIR` writes elsewhere. `--reps` below 5 makes every cell INVALID and the client says so. It writes `lab/reports/raw/<UTC date>-S2-crossos/<client>__to-<server>_<link>.json`. **Then swap the roles and run the other direction.**

**4. Build the report** after copying every JSON into one folder (Wi-Fi and Ethernet files may share it):
```bash
uv run python lab/spikes/s2_crossos.py report --results lab/reports/raw/2026-09-21-S2-crossos --out lab/reports/2026-09-21-S2-crossos.md --hosts L1=L1,L2=L2
```
`--hosts LABEL=HOST` renames each host in the table (case-insensitive) **and** declares which directions should exist: every ordered pair nobody measured becomes a `TBD` row. `HOST` is the name the runs recorded — the `--name` you passed, or the hostname if you passed none. Tables: reachability verdicts per direction × link (HTTP GET, HTTP stream, gRPC unary, gRPC server-stream, one column per bidi idle/keepalive case, outbound-only), latency/throughput means, and the diagnoses with their fixes. Then write Goal/Conclusion and update ADR-0002 / ADR-0009 and `docs/LAB.md`.

**Exit codes:** `0` complete · `1` cannot bind/write · `2` bad usage · `3` results written but incomplete (peer lost, a failed *measured* repetition: see `errors` in the JSON; a failed warmup repetition never makes a run incomplete) · `4` refused: running inside WSL · `130` Ctrl-C.

**How to read the numbers (honesty notes):**
- `yes` needs **every** repetition to pass with n ≥ 5; `flaky k/n` shows both counts; `INVALID (n<5)` is measured but not a lab result; `TBD` is untested and is never replaced by a guess.
- One run = one direction. `A -> B` says nothing about `B -> A`, and a Wi-Fi row says nothing about Ethernet — the two are separate rows and are never merged.
- A session that dropped with keepalive **off** while keepalive **on** survived is an idle-survival finding (turn keepalive on), not a failure of the outbound-only model.
- `outbound-only` is `yes` only with `--no-inbound-rule` **and** a working gRPC unary **and** a surviving session; it is `no` only when a gRPC case really failed, and `TBD` when those cases never produced a valid result. Its `yes` reads "operator declared no inbound rule (--no-inbound-rule); client binds no listening socket by construction" — the first half is your statement, the second a property of the code, neither a measurement.
- A run against the **same machine** (loopback, or both sides with the same hostname) is marked `⚠ same machine` in the `link` cell, gets `TBD — loopback path crosses no firewall` for outbound-only, and is listed under "Diagnoses and fixes" as `not a cross-OS result`; a run where both sides report the **same OS** is listed as `same OS on both sides … — does not answer ADR-0009`. Such a run proves the tool works, nothing more.
- A direction with a WSL side is flagged as not a native pair: ADR-0009 needs a native run.
- The throughput columns bound the stdlib HTTP server and the gRPC handler, **not** the link — bandwidth questions belong to S1 and S3.

`_s2/client.py`, `_s2/env.py` and `_s2/protocol.py` have no CLI coupling, so V0.3's agent connection code can adopt the session shape and keepalive settings (by moving them under `src/`, never importing `lab/`).

### s3_transport.py (spike S3, task 0.2.7)

Measures, for one pair of laptops, how much of a plain TCP connection survives when bytes travel over **gRPC byte streams** (ADR-0005, tensors) or over **HTTP `GET`/`Range`** (ADR-0003, blobs), from 1 MiB to 2 GiB — the numbers that answer ARCHITECTURE §27 Q2. Per payload size it runs six transfers (raw TCP down/up as the baseline, gRPC server-stream/client-stream, HTTP GET, `N` parallel HTTP `Range` requests), plus a gRPC message-size sweep, a one-shot probe of gRPC's default 4 MB message limit, and this laptop's loopback ceiling per transport. Every transfer is CRC-32 verified against a deterministic generated payload, so a transport that loses bytes fails instead of winning. Standard library + `grpcio`; without `grpcio` the gRPC rows become typed `grpc_unavailable` errors and the rest of the run still works. Details, formulas and limits: [`spikes/S3.md`](spikes/S3.md).

**Before you start:** both laptops on the same link (unplug Ethernet for the Wi-Fi matrix; Wi-Fi off for the Ethernet matrix), plugged in, performance mode, nothing else running. **Never run S1 and S3 at the same time**, and never two S3 clients at once — they would share the link.

**1. Firewall on the server laptop (only for the spike, remove it afterwards).** One `serve` process opens **TCP 47301 (raw TCP + control), 47302 (gRPC) and 47303 (HTTP)** — three TCP ports, no UDP, unlike S1's single 47201. Replace `192.168.1.0/24` with the lab subnet.

```powershell
# Windows, PowerShell as Administrator. Network profile must be Private (task 0.2.4).
New-NetFirewallRule -DisplayName "MeshForge S3" -Group "MeshForge lab" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 47301-47303 -RemoteAddress 192.168.1.0/24 -Profile Private
# afterwards:
Remove-NetFirewallRule -DisplayName "MeshForge S3"
```
```bash
# Linux: only needed if ufw is active (sudo ufw status)
sudo ufw allow from 192.168.1.0/24 to any port 47301:47303 proto tcp
# afterwards:
sudo ufw delete allow from 192.168.1.0/24 to any port 47301:47303 proto tcp
```
The same "Windows Security Alert" warning as for S1 applies: tick **Private networks** and **Allow**, never Cancel.

**2. Start the server** on the laptop that sends the bytes, bound to its lab IP on the link being measured:
```bash
uv run python lab/spikes/s3_transport.py serve --bind 192.168.1.20
```
It refuses `0.0.0.0`/`::` and public addresses (override: `--allow-any`), answers private/loopback/link-local clients only, caps connections per transport, drops a connection without a complete request header within `--header-timeout`, and stops by itself after `--idle` seconds without traffic (default 900) or `--duration` seconds in total (default 7200), or on Ctrl-C / SIGTERM.

**3. Run the client** on the other laptop (one pair at a time; one run measures both directions):
```bash
# Ethernet, full size ladder (~20 min)
uv run python lab/spikes/s3_transport.py client --server 192.168.1.20 --link ethernet
# Wi-Fi: stop at 256 MiB, otherwise 1 GiB and 2 GiB dominate the run
uv run python lab/spikes/s3_transport.py client --server 192.168.1.20 --link wifi --max-size 256MiB
```
```powershell
# Windows (PowerShell, native, not inside WSL) — same commands
uv run python lab/spikes/s3_transport.py client --server 192.168.1.20 --link ethernet
```
Defaults: `--reps 5 --warmup 1 --pause 0.5 --sizes 1MiB,8MiB,64MiB,256MiB,1GiB,2GiB --chunk 1MiB --chunk-sweep 64KiB,256KiB,1MiB,4MiB --chunk-sweep-size 64MiB --range-parts 4 --ceiling-size 64MiB --ceiling-reps 3 --transfer-timeout 600`. `--server` can be repeated (servers are measured one after another), `--no-uploads` halves the run time, `--ceiling-size 0` skips the loopback check, `--name L2` labels the column, `--out-dir DIR` writes elsewhere. `--reps` below 5 makes every row INVALID and the client says so. Before measuring the peer it starts a second `serve` process on `127.0.0.1` to record this laptop's **loopback ceiling** per transport. It writes `lab/reports/raw/<UTC date>-S3-transport/<client>__to-<server>_<link>.json`.

**4. Build the report** after copying every laptop's JSON into one folder:
```bash
uv run python lab/spikes/s3_transport.py report --results lab/reports/raw/2026-09-21-S3-transport --out lab/reports/2026-09-21-S3-transport.md
```
Tables: throughput per transport × payload size, the gRPC chunk sweep, CPU ms per MiB, the loopback ceiling, the gRPC limit probe, and a **Q2 answer generated from the data** (gRPC and HTTP as a percentage of the raw-TCP baseline, the parallel-range judgement, the best chunk). `INVALID (n=k)` for n < 5, `TBD` where nobody measured. Then write Goal/Conclusion and update ADR-0003 / ADR-0005 and ARCHITECTURE §27 Q2.

**Exit codes:** `0` complete · `1` cannot bind/write · `2` bad usage (wildcard bind, public server address, bad size) · `3` results written but incomplete (server lost, failed repetitions, no grpcio: see `errors` in the JSON) · `130` Ctrl-C.

**How to read the numbers (honesty notes):**
- Throughput is counted **at the receiver**, over *request sent → last byte* (downloads) or the server's *request seen → last byte* (uploads). Connection setup is excluded for all three transports alike; request latency is included.
- Compare every LAN cell with the **loopback ceiling** table: a result within a few percent of its transport's ceiling is limited by this tool (Python, framing, CRC verification), not by the link.
- `cpu_ms_per_mib` (client + server) is the part of the overhead that does not disappear on a faster link — it is the honest cost of gRPC/HTTP framing.
- Verdict thresholds: ≥ 85 % of raw TCP = acceptable, 60–85 % = usable with a recorded penalty, < 60 % = the ADR needs revision. Parallel ranges count as a gain above ×1.10 and as **slower** below ×0.90 of a single HTTP GET.
- Numbers from different pairs or links are never mixed; the best gRPC chunk is chosen inside one pair+link group, and a runner-up within 5 % is reported as not distinguishable.
- `http-range4` at 1 MiB is dominated by connection setup and is noisy by design; read parallel ranges at ≥ 64 MiB.
- No TLS anywhere in this spike, and the payload is generated, not read from disk: mTLS (ADR-0008) and storage cost are not included.

`_s3/client.py`, `_s3/payload.py`, `_s3/meters.py` and `_s3/protocol.py` have no CLI coupling, so V0.4's blob transfer and V0.6's tensor transport can adopt them (by moving them under `src/`, never importing `lab/`).

### s7_hardware.py (spike S7, task 0.2.11)

Measures, on **one laptop at a time**, the hardware scores the planner cost model reads (ARCHITECTURE §7): CPU matmul GFLOP/s (float32/float64, 1 thread and all threads, torch or numpy), a pure-Python SAXPY fallback (`python_mflops`, always available, never comparable with GFLOP/s), host memory bandwidth (STREAM copy/scale/add/triad on all cores with torch — single-threaded lower bound with numpy/stdlib — over buffers larger than the cache), and GPU matmul TFLOP/s in fp32/fp16 (bf16 when supported) plus device copy GB/s (torch + CUDA). Formulas, size rules and limits are in [`spikes/S7.md`](spikes/S7.md). No network traffic: the result's `link` is `null`.

**Before you start:** plugged in, performance power mode, nothing else running. Without torch (ADR-0011) the CPU score is only the Python fallback and the GPU section is a `torch_not_installed` error; the run still succeeds for memory bandwidth and exits `3`.

**1. Run on every laptop** (smoke test first, then the real run, 1–5 minutes):
```bash
# Linux / WSL (bash)
uv run python lab/spikes/s7_hardware.py run --quick
uv run python lab/spikes/s7_hardware.py run
```
```powershell
# Windows (PowerShell, native, not inside WSL)
uv run python lab/spikes/s7_hardware.py run --quick
uv run python lab/spikes/s7_hardware.py run
```
Defaults: `--warmup 2 --reps 5 --pause 0.2 --timeout 1800`. `--quick` uses small sizes (32 MiB buffers, GPU N ≤ 2048, 0.02 s batches — about 5 s on a laptop with a GPU) and is marked `quick: true` in the JSON and "quick (smoke)" in the report, so it can never pass as a measurement. `--skip-gpu` for a laptop without a usable NVIDIA GPU (recorded as `gpu_skipped`), `--device N` for a second GPU, `--name L2` to label the column, `--out-dir DIR` to write elsewhere. Every benchmark runs in a child process with its own timeout, so a hung CUDA call or a crash is recorded as a typed error (`worker_timeout`, `child_crashed`, `cuda_broken`, …) and the run continues; a case that no longer fits in `--timeout` is `budget_exhausted`. It writes `lab/reports/raw/<UTC date>-S7-hardware/<host>.json` and prints the scores on stderr; `!! ON BATTERY` or `!! thermal throttling suspected` means: fix it and run again.

**2. Build the report** after copying every laptop's JSON into one folder:
```bash
uv run python lab/spikes/s7_hardware.py report --results lab/reports/raw/2026-09-21-S7-hardware --out lab/reports/2026-09-21-S7-hardware.md --labels L1=<host1>,L2=<host2>,L3=<host3>,L4=<host4>
```
One table per metric (rows = cases, columns = laptops, `mean ± stdev`), a ranking per headline metric, and a run-conditions table (backends, GPU, power, governor, throttling). `INVALID (n=k)` for n < 5, `TBD` for a laptop without a result, `err: <failure_type>` when every repetition failed. Label matching is case-insensitive (Windows hostnames are upper-case). Then write Goal/Conclusion and copy the headline numbers into `docs/LAB.md` §1.

**Exit codes:** `0` complete · `1` cannot write (or the folder holds another host's file of the same name) · `2` bad usage · `3` result written but incomplete (missing backend, failed case, budget ran out: see `errors` in the JSON) · `130` Ctrl-C.

**How to read the numbers (honesty notes):**
- Sizes are derived per machine by a fixed rule (CPU N calibrated to ≥ 0.3 s per multiplication; memory buffers = max(256 MiB, 8 × L3), ≤ ¼ of free RAM; GPU N from half the *free* VRAM), and the chosen size, calibration trace and formula are stored under `attrs.setup`, so any number can be recomputed from the JSON.
- `gflops`/`tflops` are dense-matmul peaks, not the throughput of every workload; the planner corrects them with measured history later.
- A `python_fallback` backend in the conditions table means that laptop had no torch/numpy: its CPU row is `python_mflops`, in its own table.
- `extra.planner_fields` lists the JSON paths the planner reads (`cpu-float32-all.summary.gflops.mean`, `gpu-float16.summary.tflops.mean`, `memory-bandwidth.summary.triad_gb_s.mean`, …).

`_s7/child.py` (benchmarks) and `_s7/worker.py` (bounded child protocol) have no CLI coupling; task 0.3.4 reuses them inside the agent at enrollment.

## fabric_check.py and agent_rss.py (V0.3 lab run, task 0.3.15)

The procedure the team follows is [`checklists/V0.3-fabric.md`](checklists/V0.3-fabric.md); it
has every command for Linux and Windows. In short:

| Command (on the controller laptop unless noted) | Evidence for V0.3 criterion |
|---|---|
| `uv run python lab/fabric_check.py inventory` | 1 — every node's hardware from `GET /v1/nodes/{id}`; a human compares it with `docs/LAB.md` §1 (the tool never says "matches") |
| `uv run python lab/fabric_check.py watch --node HOST --link ethernet` | 2 — polls every 100 ms; OFFLINE / ONLINE latency from the operator's Enter, on the monotonic clock; session `end_reason`; agent restart yes/no |
| `… watch --node HOST --action agent --link ethernet` | 4 — stop/start the agent: back under the same `node_id`, and really a new process |
| `uv run python lab/fabric_check.py restart-check --link ethernet` | 3, 4 — time from the new controller answering until every node is connected again; any `node_id` change |
| `uv run python lab/fabric_check.py soak --hours 24 --link ethernet` | 5 (part 1) — every state change and session end for 24 h; checkpointed, resumes when re-run |
| `uv run --extra agent python lab/agent_rss.py run --hours 24 --name L2` (**every** laptop) | 5 (part 2) — RSS of the running agent every 60 s; growth vs a post-warm-up baseline |

- **Standard library only** for `fabric_check.py` (`urllib`), so a plain `uv sync` is enough.
  `agent_rss.py` needs psutil, which comes with the `agent` extra every agent laptop has.
- It talks **only** to the controller's read-only REST surface — never to PostgreSQL or gRPC,
  never imports `meshforge` (decision D24). Default URL `$MESHFORGE_CLI__CONTROLLER_URL`, else
  `http://127.0.0.1:47110`; `--controller` overrides.
- **Agent restart detection** uses the per-process resource `sequence` (it restarts at 1 only in
  a new agent process). `session_epoch` is *not* a restart signal: the agent bumps it on every
  connection attempt, so it changes on every reconnect of the same process.
- Results: JSON envelope under `reports/raw/<day>-V0.3-fabric-<sub>/` (watch and restart runs
  carry their start time in the file name, so runs never overwrite each other) and a merged
  report `reports/<day>-V0.3-fabric-<sub>.md` rebuilt from every run of that day.
- Exit codes: `0` ok, `1` cannot write, `2` usage, `3` finished with a failure or a bound
  exceeded, `6` controller unreachable (like `compute`), `130` Ctrl-C (partial results written;
  `soak` resumes). `agent_rss.py` adds `4` psutil missing and `5` no agent process found.

## Conventions
- **Report name:** `reports/YYYY-MM-DD-<topic>.md`, e.g. `2026-09-20-S1-network.md`.
- **Raw output:** `reports/raw/YYYY-MM-DD-<topic>/<hostname>.txt|json`. Never edit raw output by hand.
- **Repetitions:** at least 5 per measurement, reported as mean ± stdev.
- **Reproducibility:** every report records the git commit, the exact commands, the machines, and the link type (Wi-Fi/Ethernet).
- Lab scripts are not part of the `meshforge` package. They must not import `controller` or `agent`.
- No secrets in any file here (see docs/LAB.md §6).

## Running a 🧑 lab step
Follow [docs/LAB.md §5](../docs/LAB.md#5-lab-procedures--human-steps): run the exact commands from the task handoff, save the raw output, paste it back, and the agent writes the report from `reports/TEMPLATE.md`.
