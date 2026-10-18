# Benchmarks (V0.9)

The benchmark system produces the labelled performance data the planner learns from (ADR-0018). It has four
parts, built in this order: the **workload library** (this page, V0.9 G2), the telemetry record and export (G1),
campaigns with forced strategies and conditions (G3-G5), and the Benchmarks page (G6). Later sections are added
by the groups that build them.

## Workload families

A **workload family** is one small, deterministic benchmark job type: `cpu.matmul`, `ml.cnn_train`, ... Every
benchmark run is one family + its parameters + a seed, turned into an **ordinary job folder** and submitted
through the normal job path - benchmarks measure the real system, not a side channel (ARCHITECTURE §17).

```bash
compute bench workloads          # the families, their parameters (range, default) and run-time hints
compute bench workloads --json   # the same, with each family's JSON Schema (docs/CLI.md section 3)
```

Code: `src/meshforge/bench/workloads/` (`base.py` contract and registry, `cpu.py` / `memory.py` / `gpu.py` /
`ml.py` the families, `kernels/` the job-side code, `materialise.py` the job folders).

### The contract

Each family has:

| Part | What it is |
|---|---|
| `name`, `kind` | `cpu.matmul`; kind `cpu`, `memory`, `gpu` or `ml` |
| parameter model | a pydantic model: every field typed, bounded, with a default; unknown keys and type coercion are refused |
| defaults | by convention **the smallest valid run** (about 5 s on the reference mid laptop) |
| `size_params` | the parameters that make a run longer; the hint grows strictly with each (unit test) |
| `expected_runtime_s(params, hw)` | a rough **compute-phase** hint: work units / (node's score x efficiency) |
| `run(params, seed)` / `verify(result)` | one in-process run through the same code a job runs, and its self-check |
| `ram_gb` / `vram_gb` | memory estimates, written into the job manifest so the planner can refuse a node too small |

**Run-time window (ADR-0012).** Campaigns need thousands of runs, so a run must take **5 s - 3 min** on the
reference mid laptop. Per-field bounds cannot say that alone (`n` and `repeats` trade off), so the library refuses
any combination whose hint is outside the window, with the hint in the error:
`cpu.sort: invalid parameters: expected about 0.3 s on the reference mid laptop; a benchmark run must take 5-180 s
(change n, repeats)`. A family may add **combination rules** the per-field bounds cannot say
(`Family.combination_problems`; one error lists them together with the window). v1 has one: `ml.cnn_train`
refuses `batch x width x image^2 > 2^24` so its float64 check fits 512 MB (see "ML reference" below).

**Hints.** The hint follows one hardware score (the `meshforge-agent hardware score` metrics): multi-core FLOP/s
for BLAS matmul, single-thread FLOP/s for one-core Python/numpy kernels (used as a proxy for per-core speed),
memory bandwidth for copies, GPU TFLOP/s or GB/s for GPU kernels. A node without the score (a GPU family on a
CPU-only node) gets a typed `Unavailable`, never a guess. The hint is rough by design - the measured run is the
truth, and V0.15's ML model is what learns the real costs.

| Hardware | Role |
|---|---|
| **Laptop 1** (i7-12650H, 16 threads, 16 GB): fp32 1-thread 131.8, fp32 all 481.0, fp64 all 262.3 GFLOP/s, triad 29.4 GB/s | `CALIBRATION_HW`: CPU/memory efficiencies were measured here (2026-10-17) |
| **Reference mid laptop**: fp32 1-thread 90, fp32 all 240, fp64 all 120 GFLOP/s, triad 20 GB/s; GPU 5 TFLOP/s fp32, 12 fp16, 180 GB/s | `MID_LAPTOP`: the window is checked against it. **An assumption** (about half of Laptop 1, an RTX 3050-class GPU) until the team's other laptops are scored |

### The v1 families

Kernels build their own synthetic data from `(params, seed)`; models are built in code. **Nothing is downloaded
at run time.** Every run checks its own output against an independent property. "Exact" = the checksum is
expected to be bit-identical on every node (integer/byte output); float outputs are only expected to repeat on
the same node and library build (BLAS and GPUs round differently).

| Family | Work (library) | Parameters (default = smallest) | Self-check | Exact |
|---|---|---|---|---|
| `cpu.matmul` | n x n matrix multiply (numpy/BLAS, all cores) | `n` 256-4096 (2048), `repeats` 1-500 (22), `dtype` float64/float32 | row-wise Freivalds, 3 vectors, in float64: every row within `k eps (\|A\|(\|B\|\|x\|))_i / sqrt(n)` (k = 4 float32, 8 float64; see below) | no |
| `cpu.primes` | sieve of Eratosthenes (pure Python, 1 core) | `limit` 1e7-4e8 (1e8), `repeats` 1-100 (4) | whole table vs an independent odd-only sieve, + 64 trial divisions, + pi(x) for 1e7 / 1e8 | yes |
| `cpu.sort` | sort random int64 (numpy, 1 core) | `n` 1e6-5e7 (1e7), `repeats` 1-500 (18) | equals the input sorted by a different algorithm (stable sort): a permutation check | yes |
| `cpu.zlib` | compress word-like synthetic text (stdlib, 1 core) | `size_mb` 8-256 (64), `level` 1/6/9 (6), `repeats` 1-100 (2) | decompresses to the input | yes |
| `cpu.sha256` | sha256 of a byte stream in chunks (hashlib, 1 core) | `size_mb` 1024-400000 (8192), `chunk_kb` 4/64/1024 (64) | re-chunked digest equal (costs as much as the run) | yes |
| `cpu.integrate` | composite Simpson of `exp(-ax) cos(bx)` (numpy, chunked) | `points_m` 10-4000 (70), `repeats` 1-50 (1) | analytic integral, rel. error <= 1e-8 | no |
| `memory.copy` | `numpy.copyto` int64 array into another | `size_mb` 64-1024 (1024), `repeats` 1-5000 (60) | destination equals source; the destination is poisoned after the warm-up, so only the timed loop can make it so | yes |
| `memory.groupby` | sort-based group-by: count/sum/max per key (numpy, pandas-free) | `rows_m` 1-40 (10), `groups` 10-1e6 (1e4), `repeats` 1-100 (4) | every group's key, count, sum and max vs a scatter recompute (`bincount`, `maximum.at`) | yes |
| `gpu.matmul` | n x n matmul (torch, CUDA) | `n` 1024-12288 (4096), `repeats` 1-5000 (200), `dtype` float32/float16 | the same row-wise float64 Freivalds as `cpu.matmul` (shared helper; k = 4 float32, 8 float16) | no |
| `gpu.conv2d` | 3x3 convolution, padding 1 (cuDNN) | `batch` 1-256 (32), `channels` 16-512 (128), `size` 16-256 (64), `repeats` (300) | the whole output recomputed in float64 on the device; each element within 2e-3 of `conv(\|x\|, \|w\|)` (allows cuDNN TF32) | no |
| `gpu.elementwise` | `sin`, then `ops` fused `x + c*y` steps (bandwidth-bound) | `elements_m` 16-256 (64), `ops` 1-64 (16), `repeats` (60) | the whole output recomputed in float64 on the device, in chunks | no |
| `gpu.resize` | bilinear resize of RGB images | `batch` 1-256 (64), `size` 64-1024 (256), `out_size` 32-2048 (512), `repeats` (1100) | every image resized again in float64 on the device; a wrong shape fails the check | no |
| `ml.cnn_infer` | small CNN (conv-BN-ReLU blocks) inference | `batch` 1-512 (64), `width` 16-256 (64), `depth` 2-12 (6), `image` 32/64, `batches` (1000) | finite logits; weights equal a seeded CPU rebuild; last batch rerun equal; **float64 CPU reference** of the last batch (see below) | no |
| `ml.cnn_train` | the same CNN, SGD steps on learnable synthetic labels | `batch` 8-512 (64), `width`, `depth`, `image`, `steps` 20-50000 (400); `batch x width x image^2 <= 2^24` | finite losses; first loss equals a **float64 CPU recompute** on the seeded rebuild; last quarter < first quarter | no |
| `ml.transformer_infer` | transformer encoder on random tokens | `batch` 1-256 (16), `seq_len` 16-2048 (256), `width` (d_model, x64) 64-1024 (256), `depth` 1-24 (4), `batches` (300) | finite; weights equal the seeded rebuild; last batch rerun equal; **float64 CPU reference** | no |

GPU and ML families **require CUDA** (`env.base: cuda-pytorch`, `resources.gpu: required`). On a node without
CUDA (or without torch) the job exits **3** with `status: "unavailable"` and the reason - a typed outcome, not a
crash. The kernels also run with `device="cpu"` in the unit tests, so their maths is tested on any machine with
torch; jobs never use that.

**Calibration status.** CPU and memory efficiencies were measured on Laptop 1 with the real kernels (the smallest
runs land within ~10-30 % of the hint there, except `cpu.integrate` which varies more with turbo). **GPU and ML
efficiencies are assumptions** (matmul 70 % of TFLOP/s, conv 40 %, CNN 20-25 %, transformer 30 %, bandwidth-bound
30-70 % of GB/s): Laptop 1's NVIDIA driver is not usable yet, so no GPU family has run on real CUDA hardware.
The first CUDA lab run recalibrates them (`tests/component/bench/` runs the GPU cases when CUDA is present).

### What a benchmark job folder holds

`build_job(family, params, seed, dest)` writes a normal job folder that `compute run` accepts (atomically:
it is built in a private sibling folder and renamed into place; on Windows, which cannot rename onto a folder,
the caller's empty folder is renamed aside and restored if the final rename fails):

```
compute.yaml              manifest: entrypoint [python, run_workload.py] (argv, never a shell),
                          env.base, requirements, resources (gpu, ram_gb, vram_gb), outputs [results/],
                          limits (timeout 15m, network none)
run_workload.py           3 lines: bench_kernels.runner.main()
workload_params.json      {"family", "params", "seed"}, canonical JSON
requirements.txt          numpy==2.5.3 (pinned to uv.lock, bumped in lockstep), only for families that use numpy
bench_kernels/            __init__, _support, runner + the family's kernel module (stdlib + numpy/torch only)
```

The job environment has no MeshForge installed, which is why the kernels are copied in and import only the
standard library, numpy and torch (a unit test parses every copied file to hold that). The same
`(family, params, seed)` gives a **byte-identical folder** (same bundle digest - the blob store keeps one copy
and a resubmit uploads nothing): fixed file set, sorted JSON, LF line endings, no timestamps. numpy is installed
by the node's environment cache once per node, not per run.

The digest is a function of the MeshForge version: changing a kernel file (`bench_kernels/*`) or
`NUMPY_REQUIREMENT` changes every affected job tree. A `NUMPY_REQUIREMENT` bump (always together with `uv.lock`;
a unit test holds them equal) is a **dataset-version event**: runs before and after measured different numpy
builds, so the bump is recorded with the dataset's library versions and the two are never mixed silently.

### `results/workload.json`

The run's only output (`schema_version` 1, `runner.RESULT_SCHEMA_VERSION`).

**Versioning rule (D228).** Version 1 was frozen when G1 started recording runs. From then on **any change to
the shape of `workload.json`** - a field added, removed, renamed, retyped, or a meaning changed (e.g. what
`compute_ns` covers) - **bumps `schema_version`**, and the agent's reader (`agent/execution/workload_result.py`)
and the run-record builder (`controller/telemetry/record.py`) learn the new version in the same change. The
agent forwards the version it read (`result_schema_version` in the attempt's stored workload report), so every
attempt says which result shape it came from. If the change alters a run-record column, the **telemetry schema
version** bumps too (`docs/TELEMETRY.md`) - a dataset never mixes two meanings of one column silently.


| Field | Meaning |
|---|---|
| `family`, `params`, `seed` | what ran |
| `status` | `ok`, `check_failed`, `unavailable` (no CUDA/torch/numpy here), `bad_params` |
| `reason` | why it is not `ok` |
| `compute_ns`, `compute_ms` | `time.perf_counter_ns()` around the kernel's compute **only** - data generation, warm-up and the self-check are excluded (D213: a monotonic duration measured where the work runs). It covers the **whole timed loop**: all `repeats` (`batches` for inference, `steps` for training) iterations, not one |
| `compute_iterations` | how many iterations `compute_ns` covers (`runner.ITERATION_PARAM`; 1 for `cpu.sha256`, whose timed work is one pass). One iteration = `compute_ns / compute_iterations` (G5 labels use this). `null` when nothing was computed; `verify()` requires it to match the params |
| `prepare_ms`, `warmup_ms`, `check_ms` | the excluded parts, measured separately |
| `checksum`, `checksum_exact` | `sha256:...` of the output; whether it should match across nodes |
| `verified`, `check_detail` | the self-check's outcome and numbers (e.g. the Freivalds error) |
| `device`, `libraries`, `platform` | `cpu`/`cuda`; python, numpy, torch, CUDA, GPU name, zlib versions; OS, machine, CPU count |

The job side re-checks what it reads (a hand-edited `workload_params.json` must not crash or allocate
without bound): seed `0..2**32-1`, no unknown keys, each integer `1..` the family's maximum, choices as listed
(`runner.PARAM_LIMITS`, held equal to the family models by a unit test; `cpu.primes` `limit` starts at 2,
`runner.PARAM_MINIMA`). The minimum is 1, not the family's: the
5 s - 3 min window is the library's rule, and tiny runs are how the kernels are tested.

**Check rules (every kernel).** (1) Shape and dtype are checked first: a wrong-shaped or garbage output is a
failed check, never an exception. (2) A NaN or inf anywhere in a float output fails, and every worst-error
reduction uses `worse()`, which keeps NaN (Python's `max(worst, nan)` silently drops it). (3) The whole output is
verified, independently of the timed path. `tests/unit/bench/test_v09_g2_check_rules.py` holds every family to
all three.

**Matmul tolerance (measured).** Row `i` of `C x - A (B x)` must stay within
`k * eps(dtype) * (|A| (|B| |x|))_i / sqrt(n)`. On Laptop 1 (numpy/OpenBLAS and torch-CPU, n = 1..4096, 20 seeds x
3 vectors up to 1024) the worst honest product used 1.43 units in float32, 3.44 in float64 (the float64 reference
rounds as much as the product) and 0.51 in float16; the ratio falls with n (float32: 0.35 at n = 4096), so the
GPU corner n = 12288 extrapolates lower. k = 4 (float32) / 8 (float64, float16) leaves >= 2.3x headroom; one
float32 element off by 1e-2 at n = 384 scores 22 units (5.5x over). Rows are converted to float64 1024 at a time,
so even a 12288 x 12288 GPU product needs no float64 copy. Recalibrate on the first CUDA run (cuBLAS may round
differently from OpenBLAS).

**ML reference (D225, measured).** A rerun only proves an ML output is *repeatable*; a consistently wrong kernel
(a bad cuDNN algorithm, a changed weight) repeats too. So the ML checks rebuild the model from the seed **on the
CPU** - a separate object built by the same seeded initialisation, never a copy of the timed model - and:

- inference: the timed model's weights and buffers must equal the rebuild bit for bit, and rows of the last
  batch are recomputed in **float64**: the whole batch when it fits a 1e11-FLOP budget (about 1 s; the default
  CNN batch fits), otherwise evenly spaced rows - always the first and last - **plus 2 rows drawn by a generator
  seeded with the run's seed** (`SAMPLED_ROWS`; never the first or last).
  **The row floor overrides the budget.** At least 2 spread rows + 2 sampled rows are always recomputed, however
  expensive one row is, so at the corners the reference is far above 1e11 FLOPs and `check_ms` can be **minutes**
  (`REFERENCE_FLOPS` says the same next to the constant). Measured on Laptop 1 (2026-10-18, torch 2.14 CPU): the
  largest window-valid transformer (`batch` 4, `seq_len` 2048, `width` 1024, `depth` 24) recomputes 4 rows =
  ~6.6e12 float64 FLOPs in **51 s** (about 2 min on the mid laptop) - inside the 15 min job timeout. Its
  memory was **~4.9 GB** of host RAM (a whole float64 model plus 4 rows of 16 x 2048 x 2048 attention scores),
  more than the 2 GB the ML families declare (`ram_gb`); since V0.9 G4 item 0.1 (D233.1) the transformer
  reference rebuilds the embedding plus **one** seeded layer (`nn.TransformerEncoder` deep-copies one layer, so
  every layer starts equal to it; each timed layer is compared with it bit for bit), applies it `depth` times
  by hand in float64 **one row at a time** with attention and feed-forward in 16 MiB chunks, and peaks at
  **~0.33 GB** at that corner (measured on Laptop 1, 2026-10-18, `VmHWM`; component test
  `tests/component/bench/test_v09_g4a_transformer_reference_memory.py`, limit 512 MB). The worst `cnn_infer` reference (`batch` 64, `width` 256, `depth` 12, `image`
  64) is 14 rows, ~1.1e11 FLOPs, about a second. G5's labels use `compute_ns`, never `check_ms`, so a slow
  check costs campaign time, not label quality;
  **Limit, stated plainly:** when sampling, only the sampled rows **of the last batch** are compared with float64.
  A fault confined to rows a run did not sample, or to an earlier batch of the timed loop, passes that run (the
  exact weight check and the full-batch rerun still apply). One run is reproducible (same seed, same rows);
  across a campaign's seeds the random rows reach every row of the batch;
- training: the first timed step's loss is recomputed in float64 on the rebuild (the whole first batch, since
  train-mode batch norm normalises with whole-batch statistics). It runs **layer by layer** with the
  convolutions in row chunks of ~64 MiB of temporaries (`REFERENCE_CHUNK_BYTES`), so only the current input and
  output activation are alive: about 2 float64 activations + one chunk. Chunking cannot go below that without
  recomputing every layer prefix per chunk (up to `depth + 1` forward passes, minutes of check), so the family
  also **caps the first activation**: `batch x width x image^2 <= 2^24` (`TRAIN_REFERENCE_ELEMENTS`, 128 MiB
  per float64 activation). Measured on Laptop 1 (2026-10-18, peak RSS growth during the reference): the old
  whole-batch forward needed ~11 activations (the CPU float64 conv's im2col buffer is 9x its input) - 356 MiB at
  the default size, 1.4 GiB at `batch` 64 `width` 256, and ~44 GiB at the old corner (`batch` 512, `width` 256,
  `image` 64); the layer-wise forward peaks at **119 MiB** at the default size and **323-387 MiB** at every
  corner of the cap (`tests/component/bench/test_v09_g3a_train_reference_memory.py` holds it under 512 MB), in
  0.5-1.4 s. The cap keeps 605 of the 914 model shapes (`batch`/`width`/`depth`/`image`) that have a
  window-valid `steps`; the refused ones would also need several GB of VRAM to train, beyond most laptop GPUs.

Each element must be within `16 * eps * sqrt(layers) * max|reference row|` (loss: scale `max(|loss|, 1)`), where
`eps` is float32's 2^-24 on the CPU and on CUDA **TF32's 2^-11** when cuDNN convolutions or cuBLAS matmuls may
use TF32 (cuDNN's default; bfloat16 2^-8 under `set_float32_matmul_precision("medium")`). On Laptop 1 (torch 2.14
CPU, 4 shapes per family up to width 128-512 / depth 12, 20 seeds each) the worst honest run used 1.05 units
(cnn_infer), 2.85 (transformer_infer) and 1.42 (cnn_train first loss), so factor 16 leaves >= 5.6x headroom. An
output scaled by 1.0001 with equal weights and an equal rerun (a forward hook - what a wrong device kernel looks
like), one weight changed by 1e-3, and a changed batch-norm buffer all fail. The reference uses the timed run's
inputs (a CUDA generator cannot be replayed on the CPU). **Not yet measured on CUDA**: the TF32 unit makes the
CUDA tolerance 8192x looser (about 3 % of the row maximum for a depth-12 CNN); the first CUDA lab run calibrates
it. A loss is one scalar, so in training a tiny change that train-mode batch-norm absorbs (e.g. 1e-3 on one conv
weight) can stay under the tolerance; the decreasing-loss property still applies.

**Self-checks are what the dataset trusts.** Labels (G5) use `verified`, so every check must fail on wrong
output, not only on gross corruption; the mutation tests in `tests/unit/bench/` and
`tests/component/bench/` corrupt outputs realistically and require the check to notice. Check cost is reported
in `check_ms` (at the smallest sizes on Laptop 1: under 1 s, except `cpu.sha256` whose re-hash costs as much as
the run).

Exit codes: **0** ok, **2** bad parameters, **3** unavailable on this node, **4** self-check failed. A crash
(a bug) is a normal job failure with its traceback in the job log.

### Adding a family

1. Kernel: four functions (`prepare`, `warmup`, `compute`, `check`) in `kernels/<module>.py`, stdlib at import
   time, numpy/torch through `require_numpy()` / `require_torch(device)`; add it to `KERNELS` and to
   `runner.KERNEL_MODULES`.
2. Family: a `Params` model whose defaults are the smallest valid run, `work_units`, `metric`, `efficiency`
   (measure it: `calibrated(units_per_second_on_laptop_1, metric)`), `ram_gb`/`vram_gb`; `register(...)`.
3. Tests: `tests/unit/bench/` (tiny-size kernel run, a wrong output fails its check) - the registry, monotonic,
   window, materialise and CLI tests pick the new family up by themselves.

## Campaign spec

A **campaign** measures families x parameters x conditions x strategies x nodes, every configuration repeated
(at least 3 times) so its noise can be judged. It is a YAML file; the annotated example is
[`examples/bench/cpu-baseline.yaml`](../examples/bench/cpu-baseline.yaml).

```bash
compute bench plan examples/bench/cpu-baseline.yaml       # expand against the live nodes: count, hours, first runs
compute bench plan spec.yaml --node node_01... --node ...  # the same offline, for named node ids
compute bench create spec.yaml                             # store it (admin): prints camp_...
compute bench campaigns [camp_...]                         # list (runs per state) / one with its spec
compute bench submit camp_... -n 10 [--wait]               # submit the next 10 planned runs
compute bench runs camp_... [--state failed]               # the runs, in execution order
compute telemetry export --campaign camp_... -o c.parquet  # its run records (TELEMETRY.md)
```

| Key | Meaning |
|---|---|
| `version` | spec format, `1` |
| `name` | 1-64 of `a-z 0-9 . _ -` |
| `seed` | drives parameter sampling, the run order and each group's workload seed |
| `families[]` | `{family, params}`; a param is one value, a list of values, or a range `{min, max, log?, int?, steps?}`; unnamed params keep the family default |
| `conditions` | a **sweep axis**: each run has exactly one condition (`none` = the baseline); see [Conditions](#conditions). An unknown or malformed one is an error naming it |
| `on_unsupported` | a node that cannot do a condition: `error` (default; the error lists every node x condition) or `skip` (those runs are stored `skipped` with the reason, and counted) |
| `session_label` | set by the human for the whole session (e.g. `wifi`, `ethernet`); copied to every record (inside `condition_measured`, D243) - Wi-Fi vs Ethernet is a label, not a condition (D241) |
| `strategies` | `local` (run on the origin itself) and/or `mode1` (run on every other node, submitted from the origin) |
| `nodes` | `all` (enrolled, not revoked) or a list of node ids / host names |
| `origin` | the submitting laptop; omitted = every node is the origin in turn (local on itself, mode1 on each other node) |
| `repetitions` | 3..100 |
| `sampler` | `grid` (cross product; a range needs `steps`), `random` or `lhs` (both need `samples: N`) |
| `on_invalid` | a point outside a family's limits (its model or the 5 s - 3 min window): `error` (default) or `drop` (dropped **and counted**) |
| `budget` | `max_runs` (required, <= 100000) and `max_hours` (optional): exceeding either is an error with the count, never a silent cut |

**Expansion** (`meshforge.bench.campaign.expand`, pure): parameter points per family -> **groups** (family +
params + the run's one condition in canonical form; `group_id` = `grp_` + digest; a spec with only `none` keeps
its pre-G4 ids, and every condition of a point uses the same workload seed) -> **configurations** (group x strategy x node x origin;
`config_id` = `cfg_` + digest) -> runs. Order: `repetitions` rounds, each visiting **every** configuration once
in an order shuffled by (seed, round), so drift over a night spreads over all configurations. The same spec,
seed and node ids give the same run list byte for byte, on any machine: every random draw comes from a SHA-256
counter stream, not Python's `random`, and LHS is our own (each non-fixed parameter's range cut into `samples`
strata, every stratum used once). The hour estimate is each family's `expected_runtime_s` on the reference mid
laptop + 15 s per run (staging, env, collect).

The controller expands the spec **once**, at `create`, against the nodes enrolled then, and stores every run
(`bench_campaign`, `bench_run`; migration 0022). Runs move `planned -> submitted -> done | failed`, or `planned ->
skipped` with a reason; a campaign is `done` when no run is planned or submitted.

## Forced runs

Each run becomes an **ordinary job** (D215): its folder from the workload library, pinned to the run's node
(`mode: local` on the origin, `offload` elsewhere), `submitter_node_id` = the origin, at the **lowest priority**
(-10: a user's job queued later is dispatched first; benchmarks never pre-empt), owned by the campaign's creator.
The planner still plans it - and, because it is a pinned **benchmark** job (its `bench:<run_id>` key; since
V0.9 G4 item 0.8 a user's pinned job gets no shadow), also plans the same request **unpinned** as a
side-effect-free shadow: the node it **prefers** is stored on the decision (`compute explain`,
`shadow_chosen_node_id`) and becomes the run record's `planner_node_id`, so every record of a forced run also
says what the planner would have picked (the V0.14 regret metric). The shadow **ignores slot occupancy** (every
slot and GPU of a node counts as free - otherwise, with the other laptops busy running the campaign, the label
would collapse onto the forced node) but keeps everything else: feasibility (capabilities, hardware, owner
policy, a slot kind the node has at all), health (state, live session) and the job's retry exclusions (D235). The run record carries `campaign_id`,
`group_id`, `config_id` and `repetition` from the `bench_run.job_id` link.

`submit` is idempotent per run (the job's Idempotency-Key is `bench:<run_id>`). A run whose node is offline, has
no session or whose owner currently refuses jobs is **deferred** (stays planned; the next submit tries it again);
one that can never run as planned (node deleted, cannot give the job what it needs - e.g. a GPU family on a
CPU-only node -, parameters the library now refuses) is **skipped** with the reason. `submit?count=k` submits
up to k runs: a **deferred run does not use up k** - the call keeps paging through the planned runs (at most
2 000 examined per call) until k are submitted or none are left (V0.9 G4 item 0.5). A node that defers for a
node-wide reason (offline, no session, its owner accepts no jobs or not this campaign's owner) is tried once
and its other runs are passed over for the rest of the call (not listed); a deferral that depends on the run
(an owner RAM / CPU / VRAM limit ...) is per run. A crash between a job's commit and its run's link cannot
leave the run planned: every campaign read or submit (`sync`) links a planned run to the job holding its
`bench:<run_id>` key - only if that job is the run's own (owned by the campaign's creator, pinned to the run's
node, submitted from its origin; G4 item 0.6). A run's `ended_at` is its job's final transition time (G4 item
0.7). Until V0.9 G5's resumable
runner, `submit` is the only driver: nothing runs in the background.

## Conditions

A **condition** is the state a run is measured under (V0.9 G4, D217/D241). The spec lists conditions as a sweep
axis - `conditions: [none, cpu_load:2, cache:cold]` gives every configuration once per condition - and a run has
**exactly one** (never a stack). Each condition is **applied, verified and reverted**; a condition that cannot be
applied or verified fails the run before it is measured, and its record's failure class is
**`condition_failed`** - it is never labelled (D217). What was measured goes into the record's
`condition_measured` (with `verified`, `reason`, the agent's and the controller's numbers and the campaign's
`session_label`).

| Condition | What happens | Verified by | Who |
|---|---|---|---|
| `none` | nothing (the baseline) | - | - |
| `cpu_load:N` (1..256, <= the node's threads) | N busy-loop processes at the **lowest OS priority** (`nice 19` / idle class), children of the agent - outside the job's cgroup / Job Object | over 2 s their CPU time adds up to >= 0.8 x N cores (`load_cores`); a load process that dies while the job runs makes the record `condition_failed` | agent |
| `gpu_load:P` (10..100 %) | a CUDA matmul loop on the attempt's GPU (else device 0), busy P % of every 100 ms | NVML utilisation within +-15 points of P (`gpu_util_percent`) | agent (CUDA + torch) |
| `cache:cold` | every blob of the job's trees (project + named inputs) this node holds is evicted right before staging - **except** what another running attempt leases or a pinned named input protects (the GC index's own rule, in its transaction) | staging hit **no** blob (`blobs_hit = 0`) | agent |
| `cache:warm` | the controller queues one unrecorded **warm-up** run of the same job on the node first (key `bench:<run>:warmup`) | staging fetched **nothing** (`blobs_fetched = 0`) | controller + agent |
| `queue:K` (1..32) | the controller queues K **filler** jobs (the run's own workload, lowest priority, pinned to the node, keys `bench:<run>:filler:<i>`) ahead of the run | K fillers were still queued or running when the run was queued (`fillers_ahead`, from the job rows) | controller |
| `netem:delay=Xms,loss=Y%,rate=Zmbit` (any non-empty subset; delay <= 500 ms, loss <= 20 %, rate >= 1 mbit) | `tc qdisc replace dev <iface> root netem ...` on the interface toward the controller, through one fixed command (`lab/netem/README.md`) | `tc qdisc show` lists it **and**, with a delay, the median TCP-connect time to the controller grows by >= half the delay (`rtt_before_ms` / `rtt_after_ms`) | agent (Linux, opt-in) |
| `unavailable:Ts` (1..300 s; `mode1` only) | the run's **origin** is paused for T s right before the mode1 run is submitted: the planner treats it as `draining` (nothing new starts there; the shadow plan avoids it) | the run was dispatched within the pause (`dispatched_after_s < T`) | controller |

Fillers and warm-ups are ordinary jobs at the lowest priority (their `bench:` key keeps them out of priority
aging like every benchmark job); they have no `bench_run` link, so they are never campaign records. Their own run
records are **tagged**: `condition` null, `condition_measured = {"bench_role": "filler" | "warmup", "run_id": ...}`
- label and planner-training queries must drop rows with `condition_measured->>'bench_role'` set (system-made
background load, not user demand).

**What is verified, exactly.** `netem`: the shaped qdisc line from `tc qdisc show` is the only check for `loss`
and `rate` (no packet-loss or throughput probe); the RTT probe (median TCP-connect time to the controller) only
proves the **egress** delay - netem shapes what leaves this interface, so the probe's SYN is delayed, the reply
is not. `gpu_load`: the load runs on the attempt's first GPU (`gpu_indices[0]`), device 0 when the attempt has
none, and NVML utilisation is read for that same index.

**Support by OS.** The agent advertises what it can do in one capability tag (`bench-conditions:cache,cpu_load,
gpu_load,netem`); the controller checks it at **create** (an error listing node x condition, or `on_unsupported:
skip`), and the agent re-checks every condition it receives (refused typed if not advertised or not canonical).

| | Linux | Windows | needs |
|---|---|---|---|
| `cpu_load`, `cache` | yes | yes | `[agent.conditions] enabled` (default on) |
| `gpu_load` | yes | yes | an NVIDIA GPU + `torch` installed |
| `netem` | yes | **no** (reported unsupported) | `[agent.conditions] netem = true`, `netem_interface`, the sudoers rule |
| `queue`, `unavailable` | yes | yes | nothing (controller-side) |

**Always reverted.** The agent reverts the condition right before it sends the attempt's final status - on
success, failure, cancel and agent stop - and writes what it changed (load pids with their start times, the
shaped interface) to `<data dir>/conditions/active.json` **before** changing it; the next agent start undoes
whatever a crashed process left (a pid is killed only if it is still that burner; a netem is deleted only if the
entry says `netem_installed` - written right before `tc replace` - and `tc qdisc show` still lists a netem root
there: a foreign netem or a non-netem root qdisc is never touched). A revert never runs under a running apply:
`stop_all` cancels the apply and waits for it (a cancelled `tc` is killed and reaped) before it reverts. Load processes are bounded
twice more: each ends by itself at the attempt's time limit + 30 s + `setup_allowance_s`, and within about a
second of its agent dying. G5's owner `pause benchmarks` switch stops every running load through the same hook
(`ConditionRunner.stop_all`).

Limits (v1): `cache:cold` does not evict the job's **environment** (its hash is only known inside the env phase,
and a rebuild may need a download the offline LAN does not have); `cache:warm` / `queue:K` assume the node runs
one benchmark at a time (with more CPU slots the warm-up may run beside the run - then the agent sees fetched
blobs and the run is `condition_failed`, never mislabelled); a pause lives in the controller's memory (a
controller restart ends it early - the record sees that the controller started between the pause and the dispatch,
`pause_lost`, and the run is `condition_failed`); netem shapes the whole egress of the interface, not only the
controller's traffic.
