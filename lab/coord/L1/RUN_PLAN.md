# Lab run plan - planner exam + Mode 3 evidence (CPU + GPU) - 2026-11-13

Status: **ON HOLD (human, 2026-11-14): no further runs until the Windows slowness is fixed.** Run 1 done; FINAL PLAN agreed 2026-11-13, approved by LEAD. Each run starts only after the human says "go"; laptops get the context + start time
first and reply "ready". All laptops on v0.17.4. Order: **Run 1 (while 2B jobs are written) -> Part E -> Run 2B -> Run 2A -> Run 3.** GPU-split runs (2B) are not a built-in campaign, so they are graphs + ML data, not 0.14.8-scored (testing-tool gap, not a product gap).

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

## Run 1 - planner exam 0.14.8 - FINAL (human approved 2026-11-13) - 12 sizes x 3 repeats

| Job type | L1 alone (local) | One other laptop (Mode 1) | All 4 together (Mode 3) | Total |
|---|---|---|---|---|
| cpu.primes (CPU maths) | 36 | 108 (36 each on L2, L3, L4) | - | 144 |
| split.image_classify (CPU image AI) | 36 | 108 (36 each on L2, L3, L4) | 36 | 180 |
| **Total** | **72** | **216** | **36** | **324** |

- LEAD recommendation: 12 sizes = 2 spare groups over the >= 10 groups rule (one failed group must not make C1/C2 NOT MET).
  split.image_classify keeps its mode3 config so split decisions are scored (2A at 8 sizes is graphs-only, not exam-scored).
- Campaign `run1h.yaml`: cpu.primes 180M-400M, images 9k-20k; 324 runs, 24 groups, 5.58 h ref (~6-7 h real).
- 2026-11-13 18:0x: first Run 1 (camp_01M7AE2T3K7AMXP5EAEEZHFKMB, with ml.cnn_train) cancelled after 3 min: built-in ml.cnn_train uses
  env.base cuda-pytorch = Docker only; L2/L3/L4 (Windows, no Docker) skip it (no_runtime_for_base) -> 108 Mode 1 runs lost. Swapped for cpu.primes.
  GPU single-laptop offload moves to Run 3 with our own venv GPU jobs. Product follow-up: built-in GPU families should run on the venv runtime.
- **Part E** (checklist E, core of 0.14.8 "fresh workloads"): split.csv_aggregate (CPU split) + split.hash_files (I/O), never used
  before - 5 planner-choice submissions each + a small 12-group campaign (LEAD: 12 not 10, spare groups), ~80-90 runs, ~1-2 h. BEFORE Run 2A (2A uses csv_aggregate).
- Then checklist A1 (calibrate), B (eval), C (latency), D (explain) on L1, ~30 min, no laptops.

## Summary - FINAL PLAN

| Run | Sizes | Runs | Time |
|---|---|---|---|
| Run 1 (planner exam 0.14.8, incl. small Mode 3 part) - first | 12 | 324 | ~6-7 h |
| Part E (2 never-seen families) - second | - | ~80 | ~1-2 h |
| Run 2B (GPU Mode 3, real data) - third | 8 | 360 | ~9-12 h |
| Run 2A (CPU Mode 3) + 1 kill run - fourth | 8 | 360 (+1) | ~6-8 h |
| Preparation (datasets, write + test + time the 2B GPU jobs) | - | - | ~2-3 h |
| **Total** | | **~1124** | **~24-32 h (about 3 days of lab time)** |

Proposed order: start **Run 1 first** while L1 writes the 2B job scripts (writing only - no GPU test runs during Run 1),
then Part E, then test/time the 2B jobs, then Run 2B, then Run 2A. After all: ML dataset ~970 + ~1070 runs -> small top-up to
>= 2000, train ML, offline comparison (no re-run needed), short live ML check.

## Run 3 - GPU offload (Run 1's GPU part) + showcase - AFTER all planned runs (human, 2026-11-13)

Part A - GPU offload (Mode 1 on GPUs), the same job on one laptop's GPU at a time, 6 sizes x 3 repeats:

| Job type | L1 GPU (local) | Sent to L4 GPU (Mode 1) | Sent to L2 GPU (Mode 1) | Total |
|---|---|---|---|---|
| GPU AI training (Tiny ImageNet) | 18 | 18 | 18 | 54 |
| GPU image AI (Tiny ImageNet) | 18 | 18 | 18 | 54 |
| GPU text AI (IMDB) | 18 | 18 | 18 | 54 |
| **Total** | **54** | **54** | **54** | **162** |

Part B - showcase: one really big GPU AI training job, once alone (L2 or L1, ~2-3 h) and once split on all 4 (~30-45 min).

- Our own venv GPU jobs (same as 2B; no Docker needed on Windows). L3 not in Part A (no GPU). Timed jobs from the L1 script
  (not exam-scored; ML data checked). ~164 runs, ~6-8 h (exact from the 2B timings). Humans get context + start time first.

Rules during a run: laptops plugged in, sleep Never, Best performance, big apps closed (L2/L3 > 2 GB free RAM),
agent never stopped, Windows Update paused; light use only; pause/resume possible (`compute bench pause/resume`).

## Product gaps found while planning (for LEAD / BUG-FIX)
- No built-in **GPU split benchmark family** (all `split.*` families are CPU-only) -> campaigns and ML data never
  cover Mode 3 on GPUs systematically.
- No workload on a **real dataset** (all benchmark data is generated).
