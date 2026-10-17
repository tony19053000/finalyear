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
(change n, repeats)`.

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
| `ml.cnn_train` | the same CNN, SGD steps on learnable synthetic labels | `batch` 8-512 (64), `width`, `depth`, `image`, `steps` 20-50000 (400) | finite losses; first loss equals a **float64 CPU recompute** on the seeded rebuild; last quarter < first quarter | no |
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

The run's only output (`schema_version` 1):

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
  CNN batch fits), otherwise evenly spaced rows, always the first and last (worst case, the largest transformer:
  2 rows, ~3e12 FLOPs, tens of seconds on a laptop CPU - still inside the 15 min job timeout);
- training: the first timed step's loss is recomputed in float64 on the rebuild (the whole first batch, since
  batch-norm couples it; under 1 s at the default size, an estimated ~30 s at the largest window-valid batch).

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
