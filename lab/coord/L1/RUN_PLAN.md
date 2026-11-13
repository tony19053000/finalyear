# Lab run plan - planner exam + Mode 3 evidence (CPU + GPU) - 2026-11-13

Status: **PLAN, not started.** Each run starts only after the human says "go"; laptops get the context + start time
first and reply "ready". All laptops on v0.17.4. Order: **Run 2B -> Run 2A -> Run 1.**

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

## Run 2A - CPU Mode 3 - 8 sizes x 3 repeats

| Job type | L1 alone (local) | Split on 2 (L1+L2) | Split on 3 (L1+L2+L4) | All 4 together | Total |
|---|---|---|---|---|---|
| split.image_classify (CPU CNN on images) | 24 | 24 | 24 | 24 | 96 |
| split.montecarlo_range (scientific simulation) | 24 | 24 | 24 | 24 | 96 |
| split.csv_aggregate (data processing) | 24 | 24 | 24 | 24 | 96 |
| **Total** | **72** | **72** | **72** | **72** | **288** |

- Built-in campaign (`strategies: [local, mode3]`, `mode3_sets` = the 3 sets). Big sizes. Time ~5-6 h.

## Run 1 - local + Mode 1 (best single laptop, planner exam 0.14.8) - 6 sizes x 3 repeats

| Job type | L1 alone (local) | One other laptop (Mode 1) | All 4 together (Mode 3) | Total |
|---|---|---|---|---|
| cpu.primes (CPU maths) | 18 | 54 (18 each on L2, L3, L4) | - | 72 |
| cpu.zlib (compression) | 18 | 54 (18 each on L2, L3, L4) | - | 72 |
| **Total** | **36** | **108** | **0** | **144** |

- Built-in campaign. Big sizes (primes 200M-380M, zlib 160-250 MB level 9). Time ~3-4 h.

## Summary

| Run | Runs | Time |
|---|---|---|
| Run 2B (GPU Mode 3) | 360 | ~9-12 h |
| Run 2A (CPU Mode 3) | 288 | ~5-6 h |
| Run 1 (single laptop) | 144 | ~3-4 h |
| Preparation (write + test 2 new GPU jobs, time them) | - | ~2-3 h |
| **Total** | **792** | **~19-25 h (about 2 days of lab time)** |

Rules during a run: laptops plugged in, sleep Never, Best performance, big apps closed (L2/L3 > 2 GB free RAM),
agent never stopped, Windows Update paused; light use only; pause/resume possible (`compute bench pause/resume`).

## Product gaps found while planning (for LEAD / BUG-FIX)
- No built-in **GPU split benchmark family** (all `split.*` families are CPU-only) -> campaigns and ML data never
  cover Mode 3 on GPUs systematically.
- No workload on a **real dataset** (all benchmark data is generated).
