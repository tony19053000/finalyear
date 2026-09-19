# LAB — Physical test lab

> Living record of the team's real machines, network, and feasibility measurements. Filled in during **V0.2** (tasks 0.2.2–0.2.12). Evidence lives in [`lab/reports/`](../lab/reports/); this file is the summary.
> Related: [ARCHITECTURE §23 Deployment topology](ARCHITECTURE.md#23-deployment-topology-lab) · [§27 Open questions](ARCHITECTURE.md#27-open-questions) · [Roadmap V0.2](DEVELOPMENT_ROADMAP.md#v02--lab-setup--feasibility-validation)

**Last updated:** 2026-09-15 · Unknown values are marked **TBD**.

---

## 1. Machines

| Field | Laptop 1 | Laptop 2 | Laptop 3 | Laptop 4 |
|---|---|---|---|---|
| Hostname | TBD | TBD | TBD | TBD |
| Owner | TBD | TBD | TBD | TBD |
| Role (proposed) | controller + PostgreSQL + agent | agent + GPU service | agent + GPU service | agent + GPU service |
| OS / build | Ubuntu 24.04 (native) | Windows TBD | Windows TBD | Windows TBD |
| CPU (cores/threads) | Intel i7-12650H (TBD from inventory) | TBD | TBD | TBD |
| RAM | 16 GB | TBD | TBD | TBD |
| GPU / VRAM | NVIDIA RTX 3050 Mobile / ~4 GB | TBD | TBD | TBD |
| Driver / CUDA | **blocked**: Secure Boot MOK not enrolled (0.2.1); 595 installed, leftover 590-open metapackage | TBD | TBD | TBD |
| `nvidia-smi` / `torch.cuda.is_available()` | not yet (driver blocked) | TBD | TBD | TBD |
| Docker (GPU in container?) | Docker 29.7, daemon usable without sudo; GPU TBD | TBD (Docker Desktop?) | TBD | TBD |
| Python / uv | Python 3.12.3 / uv 0.10.12 | TBD | TBD | TBD |
| Other tooling | git 2.43, Node 22 / npm 10, gh 2.45; no `protoc` (grpcio-tools used), no `psql` (Postgres in Docker) | TBD | TBD | TBD |
| Network adapters (Wi-Fi / Ethernet, link speed) | TBD | TBD | TBD | TBD |
| MAC (Ethernet / Wi-Fi) | TBD | TBD | TBD | TBD |
| IP reservation (Ethernet / Wi-Fi) | TBD | TBD | TBD | TBD |
| Inventory JSON | TBD | TBD | TBD | TBD |
| AI-agent access (SSH / Claude Code / human relay) | local | TBD (Q5) | TBD (Q5) | TBD (Q5) |

### How inventory feeds this table
1. `lab/inventory.py` (task 0.2.2) runs on each laptop from the repo root, after a plain `uv sync`, without admin rights, and writes one JSON file. On Linux (bash) run `uv run python lab/inventory.py --out "lab/reports/inventory/$(hostname).json"`. On Windows (PowerShell, native, not WSL) run `uv run python lab/inventory.py --out "lab/reports/inventory/$env:COMPUTERNAME.json"`. The exit code is 0 even when some probes fail, for example a broken NVIDIA driver; the failure is then recorded in the JSON. A short summary is printed to stderr. Its `problems` list names everything a human must fix, for example "NVIDIA GPU present but the driver is not working". The whole run takes at most `--timeout` seconds (default 60). Flags and privacy rules are listed in [`lab/README.md`](../lab/README.md#inventorypy). `LAB_FIELDS` in `lab/_inventory/model.py` maps every row of this table to its JSON fields.
2. Commit the JSON as it is, without editing it by hand. It is the raw evidence.
3. Copy the fields into the table above and link the JSON in the "Inventory JSON" row.
4. When hardware or drivers change, re-run the script, overwrite the JSON, and update the table and the **Last updated** date.

The table is the human-readable view and the JSON is the source of truth. If they disagree, the JSON wins.

---

## 2. Network

**Topology:** gigabit Ethernet switch (not yet purchased) plus the Wi-Fi router, used for internet and as the fallback link. See ARCHITECTURE §23.
Measurements come from spike **S1** (task 0.2.5), ≥ 5 reps per cell, reported as mean ± stdev. Each direction is measured separately (row → column).

**How the matrix is produced.** Tool: `lab/spikes/s1_network.py`, our own standard-library Python measurement. iperf3 is only an optional cross-check. First open TCP+UDP port 47201 to the lab subnet, and remove the rule afterwards. Then every laptop runs `uv run python lab/spikes/s1_network.py serve --bind <its lab IP>`. The clients run one at a time: `uv run python lab/spikes/s1_network.py client --link wifi|ethernet --server <peer IP> ...`, with six runs covering all pairs. Finally, `uv run python lab/spikes/s1_network.py matrix --results lab/reports/raw/<date>-S1-network --out lab/reports/<date>-S1-network.md --labels L1=<host>,...` generates the report with both tables per link. Copy the tables here; don't write them by hand. Step-by-step instructions, the firewall commands and how to read the numbers are in [`lab/README.md`](../lab/README.md#s1_networkpy-spike-s1-task-025).

### 2.1 Wi-Fi matrix
Router model / band / channel: TBD · Date: TBD · Report: TBD

**Bandwidth (Mbit/s, TCP, row = sender)**

| from \ to | L1 | L2 | L3 | L4 |
|---|---|---|---|---|
| L1 | — | TBD | TBD | TBD |
| L2 | TBD | — | TBD | TBD |
| L3 | TBD | TBD | — | TBD |
| L4 | TBD | TBD | TBD | — |

**RTT (ms, mean ± stdev) / jitter (ms) / loss (%)**

| pair | RTT | jitter | loss |
|---|---|---|---|
| L1–L2 | TBD | TBD | TBD |
| L1–L3 | TBD | TBD | TBD |
| L1–L4 | TBD | TBD | TBD |
| L2–L3 | TBD | TBD | TBD |
| L2–L4 | TBD | TBD | TBD |
| L3–L4 | TBD | TBD | TBD |

### 2.2 Ethernet matrix
Switch model / link speeds: TBD · Date: TBD · Report: TBD

**Bandwidth (Mbit/s, TCP, row = sender)**

| from \ to | L1 | L2 | L3 | L4 |
|---|---|---|---|---|
| L1 | — | TBD | TBD | TBD |
| L2 | TBD | — | TBD | TBD |
| L3 | TBD | TBD | — | TBD |
| L4 | TBD | TBD | TBD | — |

**RTT (ms, mean ± stdev) / jitter (ms) / loss (%)**

| pair | RTT | jitter | loss |
|---|---|---|---|
| L1–L2 | TBD | TBD | TBD |
| L1–L3 | TBD | TBD | TBD |
| L1–L4 | TBD | TBD | TBD |
| L2–L3 | TBD | TBD | TBD |
| L2–L4 | TBD | TBD | TBD |
| L3–L4 | TBD | TBD | TBD |

---

## 3. Spike results index

| Spike | Task | Question | Status | Report | Answer / decision (one line) | ADR updated |
|---|---|---|---|---|---|---|
| S1 | 0.2.5 | Network matrix, Wi-Fi vs Ethernet | not run | TBD | TBD | — |
| S2 | 0.2.6 | Native Windows gRPC + HTTP reachable both ways without WSL? | not run | TBD | TBD | ADR-0009 |
| S3 | 0.2.7 | gRPC streaming vs raw TCP vs HTTP range for 1 MB → 2 GB (Q2) | not run | TBD | TBD | ADR-0003, ADR-0005 |
| S4 | 0.2.8 | `torch.export` round-trip across laptops; equality and latency vs payload | not run | TBD | TBD | ADR-0005 |
| S5 | 0.2.9 | gloo all-reduce Windows↔Linux usable? (Q3) | not run | TBD | TBD | ADR-0006 / Mode 4 |
| S6 | 0.2.10 | Docker Desktop GPU on Windows; uv venv time with/without cache (Q4) | not run | TBD | TBD | ADR-0004 |
| S7 | 0.2.11 | CPU GFLOPS, memory bandwidth, GPU fp32/fp16 TFLOPS per laptop | not run | TBD | TBD | ADR-0007 inputs |

Spike scripts live in `lab/spikes/`. Each writes machine-readable results plus a report based on [`lab/reports/TEMPLATE.md`](../lab/reports/TEMPLATE.md).

---

## 4. Controller-laptop choice

| Field | Value |
|---|---|
| Proposed | Laptop 1: native Linux and the simplest to keep running (ARCHITECTURE §23) |
| Final choice | TBD (decided in 0.2.12) |
| Criteria | always-on availability · wired Ethernet · RAM headroom for PostgreSQL · OS stability · whether its GPU is needed as a worker |
| Evidence | TBD (links to inventory JSON + S1 report) |
| Decided by / date | TBD |

---

## 5. Lab procedures (🧑 human steps)

AI agents cannot touch most lab machines, so every 🧑 step follows this loop:

1. **The agent writes the instructions** in its handoff note: which laptops, the exact commands (per OS: bash / PowerShell), the expected output, and exactly what to paste back.
2. **A human runs them** from the repo root on the named laptops, with the correct git commit checked out. Record `git rev-parse --short HEAD` in the report.
3. **Raw output is saved, not summarized.** Redirect it to a file under `lab/reports/raw/YYYY-MM-DD-<topic>/<hostname>.txt` (or `.json`), or paste the full terminal output back to the agent session.
4. **The agent writes the report** at `lab/reports/YYYY-MM-DD-<topic>.md` from the template, e.g. `2026-09-20-S1-network.md` or `2026-09-18-inventory.md`, and updates the tables in this file.
5. **Decisions** triggered by the result go into an ADR (Proposed) or the roadmap design change log. The Lead records them in `STATUS.md`.

Rules for measurements: ≥ 5 repetitions, report mean ± stdev · note the link (Wi-Fi/Ethernet), power state (plugged in, performance mode), and background load · never mix Wi-Fi and Ethernet runs in one table.

---

## 6. Safety notes

- **Trusted LAN only** until pairing + mTLS (ADR-0008) ship. The fabric is remote code execution by design, so never run agents on public or shared networks.
- **Windows firewall:** set the lab network profile to **Private**. Open only the specific ports a spike needs, scope them to the lab subnet, and remove temporary rules after the spike.
- **PostgreSQL is never exposed to the LAN.** Bind it to `127.0.0.1` on the controller laptop (Docker `-p 127.0.0.1:5432:5432`). Agents talk to the controller, never to the DB.
- **No secrets in reports or logs.** Remove passwords, tokens, private keys, and Wi-Fi keys from pasted output before committing. MAC and private IP addresses are fine.
- Stop spike servers (iperf3 `-s`, test gRPC/HTTP servers) when the measurement ends.
- GPU/driver changes (MOK enrollment, driver installs) need an explicit human decision. Agents only give instructions.
