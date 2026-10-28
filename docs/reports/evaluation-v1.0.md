# MeshForge v1.0 — final evaluation (roadmap 1.0.6, V1.0 criterion 3)

> **Status: SKELETON — no results yet.** Every place marked **TODO-lab** is filled in G6 from the Day L4 data of
> `lab/checklists/V1.0-campaign.md`. No number in this file may be written before that data exists, and every
> number must be copied from the generated report or the lab files named next to it — never estimated.

This is the hand-written report: question, setup, method, discussion and threats to validity. The **numbers**
(tables, CIs, verdicts, figures) are in the generated companion **`docs/reports/v1.0-evaluation.md`**
(`scripts/eval/make_report.py`; do not edit it by hand). Where this file quotes a number it names the table it
came from.

## 1. Research question

> On a LAN of heterogeneous personal laptops, does a planner that chooses between LOCAL, Mode 1 and Mode 3 using
> calibrated and learned time predictions reduce job completion time compared with a fixed local baseline and
> conventional schedulers, and by how much? (D415)

- **RQ1** completion time (makespan, end-to-end) against the baselines;
- **RQ2** prediction error: ML vs calibration vs the raw analytical model;
- **RQ3** which planner components matter (ablations A1–A7);
- **RQ4** the fabric's own overhead (Local vs Mode 1 on the same job).

Scope: v1.0 ships **Local, Mode 1 (Job Offloading) and Mode 3 (Task Splitting)** with the planner (rule-based,
planner v2 with calibration, ML). **Mode 2 (Remote GPU) and Mode 4 (Combined GPU) are deferred** (ADR-0012):
designed, not built, and not evaluated here.

## 2. Setup

| Laptop | Role | CPU / threads | RAM | GPU | OS | Link speed |
|---|---|---|---|---|---|---|
| L1 | controller + agent (origin, "Local") | TODO-lab | TODO-lab | TODO-lab | TODO-lab | TODO-lab |
| L2 | agent | TODO-lab | TODO-lab | TODO-lab | TODO-lab | TODO-lab |
| L3 | agent | TODO-lab | TODO-lab | TODO-lab | TODO-lab | TODO-lab |
| L4 | agent | TODO-lab | TODO-lab | TODO-lab | TODO-lab | TODO-lab |

Source: `lab/reports/inventory/*.json` (Day L0) and `docs/LAB.md` §1–2.

- Build: `v1.0.0-rc.TODO-lab` at `TODO-lab` (SHA), installed packages on every laptop (D406).
- Network: TODO-lab (Wi-Fi only / Wi-Fi + Ethernet switch); measured bandwidth and RTT matrices: `docs/LAB.md` §2.
- Dates and times of each campaign: TODO-lab (from each raw folder's `sessions.jsonl`).

## 3. Method

- **Arms (D416):** B0 Local only · B1 round_robin · B2 random (seeded) · B3 least_loaded · B4 planner v2
  uncalibrated · B5 planner v2 calibrated · B6 ML planner; **oracle** = the best measured arm per group (regret
  reference). Arms that need different planner settings switch the controller profile between blocks
  (`scripts/eval/evalkit/arms.py`, `lab/eval_profile.sh`).
- **Workloads:** 6 benchmark families × 2 sizes (Mode 1: `cpu.primes`, `memory.groupby`, `cpu.sort`; Mode 3:
  `split.image_classify`, `split.csv_aggregate`, `split.hash_files`). **Held out from ML training:** `cpu.sort`
  and `split.csv_aggregate` (never run before the model set was trained and pinned).
- **Design (D417):** 10 repetitions per (family, size, arm) cell; one block = one repetition of every cell;
  order randomised per block with the campaign seed; one job per run, measured from the controller's records.
- **Statistics:** paired bootstrap 95 % CI (2 000 resamples, fixed seed) over matched groups, against B5 and
  against B0; effect size = geometric-mean speedup with CI; verdicts `better` / `not_separable` / `worse` by the
  D357 rules. A `not_separable` result is reported as such.
- **Training and evaluation data:** the Day L4 night-1 data campaign `lab-v1-data` (8 families, TODO-lab runs),
  the planner evaluator (`compute planner eval`, leave-one-family-out) and `compute planner ml compare
  --ablations` on its dataset.
- **Ablations (D418):** A1–A3 (ML without hardware / load / cache features) and A4 (calibration off) offline on
  the data campaign; A5 (Mode 3 disabled) and A6 (Mode 1 disabled) live, 5 reps on 3 families; A7 the same
  ablation campaign on Wi-Fi and on Ethernet.

## 4. Results

### 4.1 RQ1 — completion time

TODO-lab: the headline sentence per comparison (B5 vs B0, B6 vs B5, B5 vs B1–B3) with its speedup, CI and
verdict, copied from "RQ1 — completion time / Against the baselines" of `v1.0-evaluation.md`; the per-group
table's crossover (where Local stays best — short jobs, I/O-heavy jobs on Wi-Fi — and where distribution wins).

### 4.2 RQ2 — prediction error

TODO-lab: per term, MALE/MAPE of ML vs calibration vs raw ("RQ2" of the generated report; `lab-v1-eval.json`
ML block; `lab-v1-compare/report.md`).

### 4.3 RQ3 — ablations

TODO-lab: A1–A4 verdicts (`matters` / `not_needed` / `not_separable`), A5/A6 speedups vs B6, A7 per arm
(Ethernet vs Wi-Fi), or "A7 not run: no Ethernet switch".

### 4.4 RQ4 — fabric overhead

TODO-lab: Local vs Mode 1 overhead (submit→start, transfer) from "RQ4" of the generated report and the Day L0
part Q D412 numbers (decision p95, submit→RUNNING p50, idle CPU).

### 4.5 Earlier criteria measured in the same campaign

TODO-lab: V0.9 criterion 1 (runs, infrastructure failure rate), V0.14 criteria 1–3, V0.15 criteria 1–2 and the
ML-on decision latency, each with its deciding line (from `lab/reports/raw/<date>-V1.0-L4-eval/`).

## 5. Discussion

TODO-lab: what the numbers mean for the brief's core question ("given a workload and the resources available,
what is the best practical way to run it?"): when the planner keeps work Local and why that is correct, when
Mode 1 / Mode 3 pay, how much the learned part adds over calibration (possibly nothing measurable — then say so).

## 6. Threats to validity

### 6.1 Four laptops
TODO-lab: one fabric of four specific personal laptops (hardware in §2). Results show this fabric's behaviour,
not a general law; the heterogeneity range (fastest/slowest CPU ratio TODO-lab) bounds what the planner could
gain. Every campaign used L1 as the origin ("Local"); a different origin laptop could change B0 and the
crossover.

### 6.2 LAN noise
TODO-lab: background traffic, Wi-Fi channel load and laptop thermal state vary over a night. Mitigations used:
block randomisation (drift falls on every arm alike), paired comparisons within a block, 10 repetitions, CIs
instead of means. Residual: the coefficient of variation per cell (TODO-lab, from the generated per-group table).

### 6.3 Wi-Fi
TODO-lab: which campaigns ran on Wi-Fi; Wi-Fi bandwidth and RTT from `docs/LAB.md` §2. On Wi-Fi, transfer-heavy
families favour Local; results do not carry over to Ethernet (A7, if run, measures by how much).

### 6.4 Families chosen by us
TODO-lab: the six evaluated families (and the eight training families) are our own benchmark library
(`docs/BENCHMARKS.md`), sized to 5 s – 3 min on a reference laptop. Real user workloads may be shorter, longer,
more I/O-bound or GPU-bound (GPU families were out of scope). Two held-out families test generalisation, which
is a small sample.

### 6.5 Other threats
TODO-lab (keep only what applies):
- **Calibration keeps learning during the matrix** (`calibration = auto`, refit every 30 min): B5/B6 later in
  the night use factors that include earlier matrix runs, also for the held-out families. Block randomisation
  spreads this over all arms; it is how the shipped planner behaves.
- **One job at a time** (`jobs_per_run: 1`): no contention between jobs; a busy fabric could rank the arms
  differently.
- **The oracle** is the best *measured* arm, not the best possible plan; regret is relative to the arms run.
- **Evaluation and operation share the controller**: the soak, earlier lab days and the data campaign live in
  the same database (the held-out rule was checked in the dataset summary: TODO-lab).

## 7. Reproduce

```bash
uv sync --group evaluation
uv run python scripts/eval/analyze.py --raw <v1-matrix> --raw <v1-ablation-wifi> [--raw <v1-ablation-ethernet>] \
    --eval-report <lab-v1-eval.json> --ml-report <lab-v1-compare/report.json> --out <analysis dir>
uv run --group evaluation python scripts/eval/make_report.py --analysis <analysis dir>/analysis.json \
    --out docs/reports/v1.0-evaluation.md
```

Raw data: TODO-lab (GitHub release asset of `v1.0.0`, sha256 TODO-lab; Q49). `analysis/SHA256SUMS`: TODO-lab.
The same inputs give byte-identical outputs (D419; CI job `eval-repro` checks this on a committed sample).
