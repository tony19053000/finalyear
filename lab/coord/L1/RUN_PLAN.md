# Lab run plan - planner exam + Mode 3 evidence (CPU + GPU) - 2026-11-13

Status: **PLAN agreed 2026-11-13 (human), not started.** Each run starts only after the human says "go"; laptops get the context + start time
first and reply "ready". All laptops on v0.17.4. Order: **Run 1 (while 2B jobs are written) -> Run 2B -> Run 2A.**

Why: MeshForge is for **long jobs**, and the X factor is **using other laptops' GPUs** (Mode 3 over GPUs).
Small jobs were dropped (the 13:38 campaign ran ~6 s jobs; cancelled). Every run is also recorded telemetry, so all
runs count as **ML training data** for the AI planner (V0.15, needs >= 2000 runs).

Words: "size" = how big one job is (e.g. 120 ... 960 items); every size is run each way, 3 repeats.

## Run 2B - GPU Mode 3 (main, the X factor) - 8 sizes x 3 repeats, all-4 doubled

| Job type | L1 alone (L1 GPU) | Split on 2 (L1+L2 GPUs) | Split on 3 (L1+L2+L4 GPUs) | All 4 together (3 GPUs + L3 CPU) | Total |
|---|---|---|---|---|---|
| GPU AI training (a neural net trained per item) | 24 | 24 | 24 | 48 | 120 |
| GPU image AI (CNN inference on images per item) | 24 | 24 | 24 | 48 | 120 |
| GPU text AI (transformer inference on text per item) | 24 | 24 | 24 | 48 | 120 |
| **Total** | **72** | **72** | **72** | **144** | **360** |

- Covers the 3 main GPU AI kinds: **training, vision, language**.
- Not a built-in benchmark family: runs as timed jobs from an L1 script (`--exclude-node` picks the laptops; check
  each run really split and really used CUDA). Lab test jobs, not product code.
- Items must be **heavy on the GPU** (bigger nets/batches) so GPU work, not Python/launch cost, dominates.
- Time: ~9-12 h (rough; one run of each job is timed first, then the exact total is given).
- Open ideas: real public dataset (e.g. part of CIFAR-10, one-time download ~170 MB, needs OK); make image/text jobs train too.

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

## Run 1 - local + Mode 1 (best single laptop, planner exam 0.14.8), CPU + GPU - 6 sizes x 3 repeats

| Job type | L1 alone (local) | One other laptop (Mode 1) | Total |
|---|---|---|---|
| cpu.primes (CPU maths) | 18 | 54 (18 each on L2, L3, L4) | 72 |
| cpu.zlib (compression) | 18 | 54 (18 each on L2, L3, L4) | 72 |
| ml.cnn_train (AI training on GPU) | 18 | 54 (18 each on L2, L3, L4) | 72 |
| gpu.matmul (GPU maths) | 18 | 54 (18 each on L2, L3, L4) | 72 |
| **Total** | **72** | **216** | **288** |

- All of L2, L3, L4 kept (human + L1 decision): L2 = different GPU (RTX 2050), L3 = no GPU / weakest (the case to avoid),
  L4 = same RTX 3050 as L1 but Windows (measured ~4x slower per GPU chunk than L1) - each teaches the planner/ML something.
- Built-in campaign `run1c.yaml`: primes 200M-380M, zlib 160-250 MB level 9, cnn_train batch 64-256, gpu.matmul n 6144-11264
  (12288 > 180 s limit). 288 runs, 5.16 h ref, ~5-6 h real. GPU runs on L3 (no GPU) are expected to be infeasible/marked.

## Summary

| Run | Runs | Time |
|---|---|---|
| Run 2B (GPU Mode 3) - first | 360 | ~9-12 h (rough; timed before start) |
| Run 2A (CPU Mode 3) | 360 | ~6-8 h |
| Run 1 (single laptop, CPU + GPU) | 288 | ~5-6 h |
| Preparation (write + test the 2B GPU jobs, time them) | - | ~2-3 h |
| **Total** | **1008** | **~22-29 h (about 2-3 days of lab time)** |

Proposed order: start **Run 1 first** while L1 writes the 2B job scripts (writing only - no GPU test runs during Run 1),
then test/time the 2B jobs, then Run 2B, then Run 2A. After all three: ML dataset ~970 + 1008 runs -> small top-up to
>= 2000, train ML, offline comparison (no re-run needed), short live ML check.

Rules during a run: laptops plugged in, sleep Never, Best performance, big apps closed (L2/L3 > 2 GB free RAM),
agent never stopped, Windows Update paused; light use only; pause/resume possible (`compute bench pause/resume`).

## Product gaps found while planning (for LEAD / BUG-FIX)
- No built-in **GPU split benchmark family** (all `split.*` families are CPU-only) -> campaigns and ML data never
  cover Mode 3 on GPUs systematically.
- No workload on a **real dataset** (all benchmark data is generated).
