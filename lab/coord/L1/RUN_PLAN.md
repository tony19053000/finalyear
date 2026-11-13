# Lab run plan - planner exam + Mode 3 evidence (CPU + GPU) - 2026-11-13

Status: **PLAN agreed 2026-11-13 (human), not started.** Each run starts only after the human says "go"; laptops get the context + start time
first and reply "ready". All laptops on v0.17.4. Order: **Run 1 (while 2B jobs are written) -> Part E -> Run 2B -> Run 2A.**

Why: MeshForge is for **long jobs**, and the X factor is **using other laptops' GPUs** (Mode 3 over GPUs).
Small jobs were dropped (the 13:38 campaign ran ~6 s jobs; cancelled). Every run is also recorded telemetry, so all
runs count as **ML training data** for the AI planner (V0.15, needs >= 2000 runs).

Words: "size" = how big one job is (e.g. 120 ... 960 items); every size is run each way, 3 repeats.

## Run 2B - GPU Mode 3 (main, the X factor) - 8 sizes x 3 repeats, all-4 doubled (human: keep sizes + doubling)

| Job type | L1 alone (L1 GPU) | Split on 2 (L1+L4, both RTX 3050) | Split on 3 (L1+L4+L2) | All 4 together (3 GPUs + L3 CPU) | Total |
|---|---|---|---|---|---|
| GPU AI training (small image model trained per item on CIFAR-10, different settings) | 24 | 24 | 24 | 48 | 120 |
| GPU image AI (CNN labels CIFAR-10 photos per item) | 24 | 24 | 24 | 48 | 120 |
| GPU text AI (transformer over WikiText-2 text per item) | 24 | 24 | 24 | 48 | 120 |
| **Total** | **72** | **72** | **72** | **144** | **360** |

- Covers the 3 main GPU AI kinds: **training, vision, language** - on **real public data**: CIFAR-10 (~163 MB, cs.toronto.edu)
  and WikiText-2 (~4 MB), downloaded once to L1 when 2B is prepared (human OK; files named before download); MeshForge ships them.
- From LEAD review (accepted): split on 2 = two equal GPUs (L1+L4); chunks >= 1-2 min of GPU work (Windows ~10-20 s start per chunk);
  one untimed warm-up run per job (first chunk after upgrade = venv rebuild); per run record real vs predicted time, chunks per laptop,
  CUDA really used, output check; health check `compute nodes` every few hours. Kept (human): 8 sizes, all-4 doubled (forced).
- Not a built-in benchmark family: timed jobs from an L1 script. Whether they count as ML training data / planner records is
  CHECKED before claiming it (LEAD point 8). Lab test jobs, not product code.
- Time: ~9-12 h (exact after warm-up timing).

## Run 2A - CPU Mode 3 - 8 sizes x 3 repeats, all-4 doubled (360, same size as 2B)

| Job type | L1 alone (local) | Split on 2 (L1+L2) | Split on 3 (L1+L2+L4) | All 4 together | Total |
|---|---|---|---|---|---|
| split.image_classify (CPU CNN on images) | 24 | 24 | 24 | 48 | 120 |
| split.montecarlo_range (scientific simulation) | 24 | 24 | 24 | 48 | 120 |
| split.csv_aggregate (data processing) | 24 | 24 | 24 | 48 | 120 |
| **Total** | **72** | **72** | **72** | **144** | **360** |

- Two built-in campaigns back to back (a campaign repeats every way equally): `run2a.yaml` (local + 3 sets, 288 runs,
  5.80 h ref) + `run2a-extra.yaml` (all-4 only, 72 runs, 1.45 h ref). Sizes: images 11k-20k, items 12k-30k, rows 35k-80k.
  Validated with `compute bench plan`. Time ~6-8 h.

## Run 1 - planner exam 0.14.8 (local + Mode 1 + Mode 3) - 10 sizes x 3 repeats (exam needs >= 10 groups per family)

| Job type | L1 alone (local) | One other laptop (Mode 1) | All 4 together (Mode 3) | Total |
|---|---|---|---|---|
| ml.cnn_train (GPU AI training) | 30 | 90 (30 each on L2, L3, L4) | - | 120 |
| split.image_classify (CPU image AI) | 30 | 90 (30 each on L2, L3, L4) | 30 | 150 |
| **Total** | **60** | **180** | **30** | **270** |

- Changed 2026-11-13 after LEAD review: the 288-run 6-size version could not pass (C1/C2 need >= 10 counted groups per
  evaluation -> too_few_groups = NOT MET); split.image_classify with mode3 kept (checklist A0); repetitions minimum is 3.
  CPU-only families (primes, zlib) dropped - Run 2A covers CPU work.
- Campaign `run1f.yaml`: cnn_train batch 48-256, images 11k-20k. 270 runs, 4.27 h ref, ~5-6 h real (validated).
- **Part E** (checklist E): 2 families never used before - split.csv_aggregate (CPU split) + split.hash_files (I/O) - 5 planner-choice
  submissions each + a small 10-group campaign, ~80 runs, ~1-2 h. Must run BEFORE Run 2A (which uses csv_aggregate).
- Then checklist steps A1 (calibrate), B (eval), C (latency), D (explain) on L1, ~30 min, no laptops needed.

## Summary

| Run | Runs | Time |
|---|---|---|
| Run 1 (planner exam) - first | 270 | ~5-6 h |
| Part E (2 unseen families) - second | ~80 | ~1-2 h |
| Run 2B (GPU Mode 3) - third | 360 | ~9-12 h (rough; timed before start) |
| Run 2A (CPU Mode 3) - fourth | 360 | ~6-8 h |
| Preparation (write + test the 2B GPU jobs, time them) | - | ~2-3 h |
| **Total** | **~1070** | **~23-31 h (about 3 days of lab time)** |

Proposed order: start **Run 1 first** while L1 writes the 2B job scripts (writing only - no GPU test runs during Run 1),
then Part E, then test/time the 2B jobs, then Run 2B, then Run 2A. After all: ML dataset ~970 + ~1070 runs -> small top-up to
>= 2000, train ML, offline comparison (no re-run needed), short live ML check.

Rules during a run: laptops plugged in, sleep Never, Best performance, big apps closed (L2/L3 > 2 GB free RAM),
agent never stopped, Windows Update paused; light use only; pause/resume possible (`compute bench pause/resume`).

## Product gaps found while planning (for LEAD / BUG-FIX)
- No built-in **GPU split benchmark family** (all `split.*` families are CPU-only) -> campaigns and ML data never
  cover Mode 3 on GPUs systematically.
- No workload on a **real dataset** (all benchmark data is generated).
