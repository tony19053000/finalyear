# pytorch-train

A small MLP trained on synthetic data from a fixed seed, with
`torch.use_deterministic_algorithms(True)`, one CPU thread and deterministic
cuBLAS. Uses CUDA when the node has it (`--device auto`), else the CPU.

```bash
compute run workloads/examples/pytorch-train --node <NODE_ID> -f
compute get <JOB_ID>     # -> outputs/<JOB_ID>/results/{metrics.json,weights.bin,run.json}
```

For a CPU-vs-CPU comparison (criterion 1), force the CPU with a copy of the
manifest whose entrypoint says `--device cpu` (the lab checklist does this).

**What "reproducible" means here** (brief D76, honest version):

| compared runs | expectation |
|---|---|
| same machine, CPU, twice | byte-identical `metrics.json` + `weights.bin` |
| two laptops, **same CPU model**, CPU | byte-identical |
| two laptops, different CPUs, CPU | usually identical; may differ in the last bits (vector kernels) - `compare.py` (rtol `1e-3`, atol `1e-5`) |
| same GPU model + driver + CUDA | byte-identical |
| different GPUs, or CPU vs GPU | not bit-reproducible - `compare.py` (rtol `1e-3`, atol `1e-5`) |

`python compare.py A/results B/results` says `IDENTICAL`, `WITHIN TOLERANCE` or
`DIFFERENT` - it never calls a tolerance match identical. `run.json` records the
device, GPU name and torch version to explain a difference.

Needs torch: in the `cuda-pytorch` image (Docker runtime) or in the agent's own
environment (`uv sync --extra gpu`) for the subprocess runtime.
