# ruff: noqa: T201 - an example program: its output is the point
"""MeshForge example: a small, reproducible PyTorch training run (roadmap 0.4.14).

Trains a small MLP on synthetic regression data (generated from a fixed seed),
on CUDA when available (or `--device cpu|cuda`), with every source of
randomness seeded and `torch.use_deterministic_algorithms(True)`.

Writes to `--out` (default `results/`):

* `metrics.json` - loss every `--log-every` steps and the final loss;
* `weights.bin` - the trained parameters as raw little-endian float32, in
  `named_parameters()` order (raw bytes, not `torch.save`, so the file depends
  on the numbers only);
* `run.json` - device, torch version, CUDA device name: what to compare a run
  against (kept apart so `metrics.json`/`weights.bin` stay comparable).

Reproducibility, honestly: on the **same** CPU model (or the same GPU model +
driver + CUDA/cuDNN) two runs give identical bytes. Across different CPUs the
last bits of float32 matrix products may differ (different vector kernels),
and GPU kernels are not bit-reproducible across GPU models - compare those runs
with `compare.py` and its tolerance instead of by digest.

Usage: python main.py [--steps N] [--device auto|cpu|cuda] [--out DIR]
"""

from __future__ import annotations

import os

# Must be set before CUDA initialises (deterministic cuBLAS).
os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")

import argparse
import json
import random
import sys
import time
from pathlib import Path

import torch
from torch import nn


def seed_everything(seed: int) -> None:
    random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.use_deterministic_algorithms(True)
    torch.backends.cudnn.benchmark = False
    torch.set_num_threads(1)  # fixed reduction order on the CPU


def pick_device(requested: str) -> torch.device:
    if requested == "cuda" and not torch.cuda.is_available():
        raise SystemExit("--device cuda asked for, but CUDA is not available here")
    if requested == "auto":
        requested = "cuda" if torch.cuda.is_available() else "cpu"
    return torch.device(requested)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--steps", type=int, default=400)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--log-every", type=int, default=50)
    parser.add_argument("--out", type=Path, default=Path("results"))
    args = parser.parse_args(argv)

    seed_everything(args.seed)
    device = pick_device(args.device)
    # Data is generated on the CPU from the seed, then moved: the same inputs
    # everywhere, whatever the device's own RNG does.
    gen = torch.Generator().manual_seed(args.seed)
    x = torch.randn(4096, 32, generator=gen)
    true_w = torch.randn(32, 1, generator=gen)
    y = torch.tanh(x @ true_w) + 0.05 * torch.randn(4096, 1, generator=gen)
    x, y = x.to(device), y.to(device)

    model = nn.Sequential(nn.Linear(32, 64), nn.ReLU(), nn.Linear(64, 1)).to(device)
    opt = torch.optim.SGD(model.parameters(), lr=0.05, momentum=0.9)
    loss_fn = nn.MSELoss()
    history: list[list[float]] = []
    started = time.monotonic()
    for step in range(1, args.steps + 1):
        batch = (step * 256) % 4096
        xb, yb = x[batch : batch + 256], y[batch : batch + 256]
        opt.zero_grad(set_to_none=True)
        loss = loss_fn(model(xb), yb)
        loss.backward()
        opt.step()
        if step % args.log_every == 0 or step == args.steps:
            value = float(loss.detach().cpu())
            history.append([step, value])
            print(f"step {step:5d} loss {value:.6f}", flush=True)

    with torch.no_grad():
        final = float(loss_fn(model(x), y).cpu())
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "metrics.json").write_text(
        json.dumps({"final_loss": final, "history": history}, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    with (args.out / "weights.bin").open("wb") as fh:
        for _name, param in model.named_parameters():
            fh.write(param.detach().cpu().contiguous().numpy().astype("<f4").tobytes())
    (args.out / "run.json").write_text(
        json.dumps(
            {
                "device": device.type,
                "cuda_device": torch.cuda.get_device_name(device)
                if device.type == "cuda"
                else None,
                "torch": torch.__version__,
                "steps": args.steps,
                "seed": args.seed,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        f"done on {device.type}: final loss {final:.6f} "
        f"({time.monotonic() - started:.2f} s)",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
