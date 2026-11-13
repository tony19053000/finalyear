# Lab run plan - planner exam + Mode 3 evidence (CPU + GPU) - 2026-11-13

Status: **PLAN agreed 2026-11-13 (human), not started.** Each run starts only after the human says "go"; laptops get the context + start time
first and reply "ready". All laptops on v0.17.4. Order: **Run 1 (while 2B jobs are written) -> Part E -> Run 2B -> Run 2A.**

Why: MeshForge is for **long jobs**, and the X factor is **using other laptops' GPUs** (Mode 3 over GPUs).
Small jobs were dropped (the 13:38 campaign ran ~6 s jobs; cancelled). Every run is also recorded telemetry, so all
runs count as **ML training data** for the AI planner (V0.15, needs >= 2000 runs).

Words: "size" = how big one job is (e.g. 120 ... 960 items); every size is run each way, 3 repeats.

## Run 2B - GPU Mode 3 (main, the X factor) - FINAL (human approved 2026-11-13)

| Job type | L1 alone (L1 GPU) | Split on 2 (L1+L4, both RTX 3050) | Split on 3 (L1+L4+L2) | All 4 together (3 GPUs + L3 CPU) | Total |
|---|---|---|---|---|---|
| GPU AI training | 24 | 24 | 24 | 48 | 120 |
| GPU image AI | 24 | 24 | 24 | 48 | 120 |
| GPU text AI | 24 | 24 | 24 | 48 | 120 |
| **Total** | **72** | **72** | **72** | **144** | **360** |

| Job | One item = | Real data | What we show |
|---|---|---|---|
| GPU AI training | train a small image model (CNN) on part of CIFAR-10 with one setting (learning rate, size ...) | CIFAR-10 | "trained N models; best X % accuracy; 4 laptops = Y x faster" |
| GPU image AI | label a batch of photos with a CNN | CIFAR-10 | "4 laptops labelled 60,000 photos in X min vs Y min on 1" |
| GPU text AI | run a transformer over a block of text | WikiText-2 | "processed Z text blocks; 4 laptops = W x faster" |

How it runs: 8 sizes per job (small -> large; small ones show "small jobs gain little"), 3 repeats, all-4 doubled (forced);
each chunk >= 1-2 min of GPU work (Windows ~10-20 s start per chunk); one untimed warm-up run per job first (venv install).
Recorded per run: real time + planner predicted time, chunks per laptop, CUDA used on each GPU laptop, output check (correct + complete).
Preparation (~2-3 h): download CIFAR-10 (~163 MB, cs.toronto.edu) + WikiText-2 (~4 MB) to L1 (files named to the human first);
write the 3 GPU jobs (lab test jobs, not product code) and test each once on L1; time one run of each -> exact total.
Runs as timed jobs from an L1 script (no built-in GPU split family); whether they count as ML data is checked before claiming it.
Time ~9-12 h (exact after timing). Proves: AI work (training, vision, language) on real data runs faster when MeshForge uses the
GPUs of other laptops, 1 -> 2 -> 3 -> 4, with proof each GPU really worked.

## Run 2A - CPU Mode 3 - FINAL (human approved 2026-11-13) - 8 sizes x 3 repeats, all-4 doubled

| Job type | L1 alone (local) | Split on 2 (L1+L4) | Split on 3 (L1+L4+L2) | All 4 together | Total |
|---|---|---|---|---|---|
| split.image_classify (CPU CNN on images) | 24 | 24 | 24 | 48 | 120 |
| split.montecarlo_range (scientific simulation) | 24 | 24 | 24 | 48 | 120 |
| split.csv_aggregate (data processing) | 24 | 24 | 24 | 48 | 120 |
| **Total** | **72** | **72** | **72** | **144** | **360** |

- From LEAD review (accepted): split on 2 = L1+L4 (strongest CPUs), split on 3 = +L2, same order as 2B; record which laptop finished
  last per run (L3 may straggle); chunks >= 1 min; **+ 1 kill run** (one laptop's agent stopped mid-split at a fixed time, humans cued
  first + "ready"; job must finish) -> closes 0.10.10. Expectation: csv_aggregate (much data over Wi-Fi) may gain little; montecarlo best.
- "Planner chooses" column DROPPED for now (human): forced runs still record the planner's shadow choice; a planner-choice run is done
  after ML training (with ML on).
- Two built-in campaigns: `run2a.yaml` (local + 3 sets, 288 runs, 5.80 h ref) + `run2a-extra.yaml` (all-4 only, 72 runs, 1.45 h ref).
  Must run AFTER Part E (csv_aggregate is a Part E "unseen" family). Time ~6-8 h + kill run ~10 min.

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
