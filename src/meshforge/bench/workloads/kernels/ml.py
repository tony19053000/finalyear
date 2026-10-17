"""ML kernels (torch): small CNN inference and training, small transformer
inference (V0.9 G2, D214).

Job-side code (rules in :mod:`._support`). Models are **built in code** with a
seeded initialisation and run on synthetic data - nothing is downloaded. The
sweep parameters are the ones the planner will see in real AI jobs: ``batch``,
``width`` (channels / ``d_model``), ``depth`` (blocks / layers) and ``seq_len``.
In a job they always run on CUDA (``device="cpu"`` is for testing the kernels).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ._support import (
    Check,
    Kernel,
    floats_digest,
    int_param,
    require_torch,
    synchronize,
)

__all__ = ["KERNELS", "cnn_layout"]

CLASSES = 10
POOL = 4
"""Distinct synthetic batches cycled through (data stays on the device)."""
VOCAB = 1024


def cnn_layout(width: int, depth: int, image: int) -> list[tuple[int, int, int]]:
    """``(in_channels, out_channels, resolution)`` of every conv layer.

    Shared with the family's FLOP count (:mod:`meshforge.bench.workloads.ml`) so
    the hint and the model cannot drift apart. A 2x max-pool follows every second
    layer while the resolution is above 4.
    """
    layers: list[tuple[int, int, int]] = []
    channels, resolution = 3, image
    for index in range(depth):
        layers.append((channels, width, resolution))
        channels = width
        if index % 2 == 1 and resolution > 4:
            resolution //= 2
    return layers


def _build_cnn(torch: Any, width: int, depth: int, image: int) -> Any:
    nn = torch.nn
    modules: list[Any] = []
    for index, (c_in, c_out, resolution) in enumerate(cnn_layout(width, depth, image)):
        modules += [
            nn.Conv2d(c_in, c_out, 3, padding=1),
            nn.BatchNorm2d(c_out),
            nn.ReLU(),
        ]
        if index % 2 == 1 and resolution > 4:
            modules.append(nn.MaxPool2d(2))
    modules += [nn.AdaptiveAvgPool2d(1), nn.Flatten(), nn.Linear(width, CLASSES)]
    return nn.Sequential(*modules)


def _cnn_prepare(
    params: Mapping[str, Any], seed: int, device: str, *, train: bool
) -> Any:
    torch = require_torch(device)
    torch.manual_seed(seed)
    batch, width, depth, image = (
        int_param(params, k) for k in ("batch", "width", "depth", "image")
    )
    model = _build_cnn(torch, width, depth, image).to(device)
    generator = torch.Generator(device=device)
    generator.manual_seed(seed)
    xs = [
        torch.randn(batch, 3, image, image, generator=generator, device=device)
        for _ in range(POOL)
    ]
    state: dict[str, Any] = {"torch": torch, "device": device, "model": model, "xs": xs}
    if train:
        # Learnable labels: argmax of a fixed random projection of the pooled image.
        projection = torch.randn(3 * 16, CLASSES, generator=generator, device=device)
        pooled = [torch.nn.functional.adaptive_avg_pool2d(x, 4).flatten(1) for x in xs]
        state["ys"] = [(p @ projection).argmax(dim=1) for p in pooled]
        state["steps"] = int_param(params, "steps")
        state["optimizer"] = torch.optim.SGD(model.parameters(), lr=0.05, momentum=0.9)
        model.train()
    else:
        state["batches"] = int_param(params, "batches")
        model.eval()
    return state


def _infer_prepare(params: Mapping[str, Any], seed: int, device: str) -> Any:
    return _cnn_prepare(params, seed, device, train=False)


def _train_prepare(params: Mapping[str, Any], seed: int, device: str) -> Any:
    return _cnn_prepare(params, seed, device, train=True)


def _infer_loop(state: Any, batches: int) -> Any:
    torch, model, xs = state["torch"], state["model"], state["xs"]
    out = None
    with torch.inference_mode():
        for index in range(batches):
            out = model(xs[index % POOL])
    synchronize(torch, state["device"])
    return out


def _infer_warmup(state: Any) -> None:
    _infer_loop(state, 2)


def _infer_compute(state: Any) -> Any:
    return _infer_loop(state, state["batches"])


def _infer_check(state: Any, out: Any) -> Check:
    """Finite logits of the right shape; the last batch run again gives the same."""
    torch = state["torch"]
    with torch.inference_mode():
        again = state["model"](state["xs"][(state["batches"] - 1) % POOL])
    finite = bool(torch.isfinite(out).all())
    error = float((again - out).abs().max())
    shape_ok = tuple(out.shape) == (state["xs"][0].shape[0], CLASSES)
    return Check(
        ok=finite and shape_ok and error <= 1e-3,
        checksum=floats_digest(
            [round(float(v), 4) for v in out.double().sum(0).tolist()]
        ),
        exact=False,
        detail=f"finite {finite}, shape {tuple(out.shape)}, rerun max diff {error:.3e}",
    )


def _train_step(state: Any, index: int) -> Any:
    torch, model, optimizer = state["torch"], state["model"], state["optimizer"]
    optimizer.zero_grad(set_to_none=True)
    loss = torch.nn.functional.cross_entropy(
        model(state["xs"][index % POOL]), state["ys"][index % POOL]
    )
    loss.backward()
    optimizer.step()
    return loss.detach()


def _train_warmup(state: Any) -> None:
    # Warm-up runs real steps; restore the initial weights so the timed run starts
    # from the seeded model (the reported losses are those of the timed steps).
    torch = state["torch"]
    model_state = {k: v.clone() for k, v in state["model"].state_dict().items()}
    for index in range(2):
        _train_step(state, index)
    synchronize(torch, state["device"])
    state["model"].load_state_dict(model_state)
    state["optimizer"] = torch.optim.SGD(
        state["model"].parameters(), lr=0.05, momentum=0.9
    )


def _train_compute(state: Any) -> Any:
    losses = [_train_step(state, index) for index in range(state["steps"])]
    stacked = state["torch"].stack(losses)
    synchronize(state["torch"], state["device"])
    return stacked


def _train_check(state: Any, losses: Any) -> Check:
    """Finite losses that went down: mean of the last quarter < first quarter."""
    values = [float(v) for v in losses.double().cpu().tolist()]
    quarter = max(1, len(values) // 4)
    first = sum(values[:quarter]) / quarter
    last = sum(values[-quarter:]) / quarter
    finite = all(v == v and abs(v) != float("inf") for v in values)
    return Check(
        ok=finite and last < first,
        checksum=floats_digest([round(v, 4) for v in values]),
        exact=False,
        detail=f"loss {first:.4f} -> {last:.4f} over {len(values)} steps",
    )


# ---------------------------------------------------------------- transformer


def transformer_heads(width: int) -> int:
    """Attention heads for ``d_model = width`` (64 per head, at least one)."""
    return max(1, width // 64)


def _transformer_prepare(params: Mapping[str, Any], seed: int, device: str) -> Any:
    torch = require_torch(device)
    torch.manual_seed(seed)
    nn = torch.nn
    batch, seq_len, width, depth = (
        int_param(params, k) for k in ("batch", "seq_len", "width", "depth")
    )
    layer = nn.TransformerEncoderLayer(
        width, transformer_heads(width), 4 * width, dropout=0.0, batch_first=True
    )
    model = nn.Sequential(
        nn.Embedding(VOCAB, width),
        nn.TransformerEncoder(layer, depth, enable_nested_tensor=False),
    ).to(device)
    model.eval()
    generator = torch.Generator(device=device)
    generator.manual_seed(seed)
    tokens = [
        torch.randint(0, VOCAB, (batch, seq_len), generator=generator, device=device)
        for _ in range(POOL)
    ]
    return {
        "torch": torch,
        "device": device,
        "model": model,
        "xs": tokens,
        "batches": int_param(params, "batches"),
        "width": width,
    }


def _transformer_check(state: Any, out: Any) -> Check:
    torch = state["torch"]
    with torch.inference_mode():
        again = state["model"](state["xs"][(state["batches"] - 1) % POOL])
    finite = bool(torch.isfinite(out).all())
    error = float((again - out).abs().max())
    batch, seq_len = state["xs"][0].shape
    shape_ok = tuple(out.shape) == (batch, seq_len, state["width"])
    return Check(
        ok=finite and shape_ok and error <= 1e-3,
        checksum=floats_digest(
            [round(float(v), 3) for v in out.double().sum(dim=(0, 1)).tolist()[:64]]
        ),
        exact=False,
        detail=f"finite {finite}, shape {tuple(out.shape)}, rerun max diff {error:.3e}",
    )


KERNELS: dict[str, Kernel] = {
    "ml.cnn_infer": Kernel(_infer_prepare, _infer_compute, _infer_check, _infer_warmup),
    "ml.cnn_train": Kernel(_train_prepare, _train_compute, _train_check, _train_warmup),
    "ml.transformer_infer": Kernel(
        _transformer_prepare, _infer_compute, _transformer_check, _infer_warmup
    ),
}
