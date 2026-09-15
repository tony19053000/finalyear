# ADR-0011: PyTorch wheels come from the official CUDA 12.6 index on Linux and Windows

> **Status:** Proposed (2026-09-16) — pending team approval and the 0.2.2 inventory of laptops 2–4 · **Deciders:** team + Lead agent

## Context
The `gpu` extra (`pyproject.toml`) pulls `torch` from PyPI. That is wrong on both of our platforms (roadmap 0.2.3, carry-over from the 0.1.2 review):
- **Windows:** the PyPI torch wheel is CPU-only, so Mode 2/4 would silently run without a GPU on laptops 2–4.
- **Linux:** the PyPI wheel bundles a recent CUDA 13.x runtime, which needs a new driver (Laptop 1 has 595, but we cannot assume that elsewhere).

Facts checked on 2026-09-16:
- PyTorch 2.14 (our locked version) publishes stable builds for CUDA 12.6, 13.0 and 13.2 ([RELEASE.md](https://github.com/pytorch/pytorch/blob/main/RELEASE.md)).
- `download.pytorch.org/whl/cu126` and `/whl/cu130` both carry `torch-2.14.0` cp312 wheels for `win_amd64` and `manylinux_2_28_x86_64`.
- Minimum NVIDIA driver ([CUDA release notes](https://docs.nvidia.com/cuda/cuda-toolkit-release-notes/index.html)): CUDA 12.6 ≥ 560 (≥ 525 with minor-version compatibility), CUDA 13.0 ≥ 580, CUDA 13.2 ≥ 595.
- CUDA 13.0 removed Maxwell, Pascal and Volta GPUs (compute capability < 7.5), e.g. GTX 10xx laptops.
- uv supports per-platform/per-extra indexes via `[[tool.uv.index]] explicit = true` + `[tool.uv.sources]` markers, and this works with `uv lock`/`uv sync` ([uv PyTorch guide](https://docs.astral.sh/uv/guides/integration/pytorch/)). `--torch-backend=auto` works only with `uv pip`, not with the lockfile, so it is not an option for us.

The hardware of laptops 2–4 is still unknown (Q1), so the choice must work on the widest range of GPUs and drivers.

## Decision
1. Add an explicit index `pytorch-cu126` (`https://download.pytorch.org/whl/cu126`) and source `torch` (and later `torchvision`, if added) from it for `sys_platform == 'linux' or sys_platform == 'win32'`. Other platforms keep PyPI (CPU). Everything stays in one `uv.lock`.
2. Keep one `gpu` extra (no separate `cpu`/`cu130` extras yet). A machine without an NVIDIA GPU simply doesn't install the extra.
3. The GPU service checks at startup: `torch.version.cuda` is set, `torch.cuda.is_available()` is true, and the device's compute capability is supported by the installed build (`torch.cuda.get_arch_list()`). If any check fails it refuses to start with a typed error that names the fix (driver version, wrong wheel). Never fall back to CPU silently.
4. The 0.2.2 inventory JSON of every laptop is the evidence: after it arrives, 0.2.12 confirms cu126, or moves to cu130 if every GPU is Turing or newer (CC ≥ 7.5) and every driver is ≥ 580.

## Alternatives considered
- **PyPI torch (status quo):** CPU-only on Windows. Rejected.
- **cu130:** newer, but needs driver ≥ 580 and drops pre-Turing GPUs. Kept as the upgrade path once the inventory proves every laptop qualifies.
- **cu132:** needs driver ≥ 595; too new to assume on team laptops.
- **Separate `cpu` / `cu126` / `cu130` extras with `[tool.uv] conflicts`:** flexible, but more choices for users to get wrong; not needed until the lab shows mixed hardware.
- **`uv pip --torch-backend=auto`:** auto-detects the GPU, but bypasses the lockfile, so installs stop being reproducible.

## Trade-offs
+ Real CUDA on Windows and Linux from one lockfile. + Works with the oldest realistic laptop GPUs and drivers. + A wrong setup fails loudly at startup.
− CUDA 12.6 misses newer kernels and features in 13.x; some newer GPUs may run a little slower. − The cu126 wheel is large (about 2–3 GB with its CUDA libraries); first `uv sync --extra gpu` is slow and needs disk space. − We depend on download.pytorch.org being reachable (the uv cache helps).

## Consequences
- `pyproject.toml` (`[[tool.uv.index]]`, `[tool.uv.sources]`) and `uv.lock` change; the CI component job that installs `--extra gpu` (if any) downloads the cu126 wheel.
- New startup CUDA check in `gpu_service` (V0.11 at the latest; a small version can land with S4 in V0.2).
- `lab/inventory.py` already records driver version, CUDA driver version and compute capability, which is exactly what 0.2.12 needs to confirm this ADR.
- Revisit when: the inventory shows a GPU or driver that cu126 cannot serve; PyTorch drops cu126; or benchmarks show a real speed gain from 13.x.
