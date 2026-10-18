"""ML kernels (torch): small CNN inference and training, small transformer
inference (V0.9 G2, D214).

Job-side code (rules in :mod:`._support`). Models are **built in code** with a
seeded initialisation and run on synthetic data - nothing is downloaded. The
sweep parameters are the ones the planner will see in real AI jobs: ``batch``,
``width`` (channels / ``d_model``), ``depth`` (blocks / layers) and ``seq_len``.
In a job they always run on CUDA (``device="cpu"`` is for testing the kernels).

**Independent float64 reference (D225).** Running the last batch again only
shows the output is *repeatable*; a consistently wrong kernel (a bad cuDNN
algorithm, a changed weight) repeats too. So every check also rebuilds the model
from the seed on the CPU - a separate object built by the same seeded
initialisation, never a copy of the timed model - and recomputes in float64
(:func:`_seeded_model`):

* inference: the timed model's weights and buffers must equal the seeded build
  bit for bit, the whole last batch rerun must equal the output, and rows of the
  last batch (all of them when that fits :data:`REFERENCE_FLOPS`; else evenly
  spaced plus seeded random rows, see :func:`reference_rows`) must agree with
  the float64 recompute within :func:`ml_tolerance`;
* training: the first timed step's loss must agree with a float64 recompute of
  that step on the seeded build (the whole first batch - batch-norm statistics
  couple its rows), and the losses must go down. The recompute runs layer by
  layer with the convolutions in row chunks (:func:`_train_reference_forward`),
  so its memory is about two float64 activations, and the family refuses a
  first batch above :data:`TRAIN_REFERENCE_ELEMENTS` (G3 item 0.3, D228).

The reference uses the timed run's inputs (generated in ``prepare``, not timed):
a CUDA generator's numbers differ from a CPU one's, so they cannot be redrawn on
the CPU.

**Transformer reference memory (G4 item 0.1, D233.1).** ``nn.TransformerEncoder``
deep-copies its one seeded layer ``depth`` times, so every layer starts with the
same weights: the seeded rebuild is the embedding plus **one** layer
(:func:`_transformer_parts`), compared bit for bit with each of the timed model's
layers, and the float64 recompute applies that layer ``depth`` times
(:func:`_transformer_reference_forward`) - by hand, one row at a time, attention
and feed-forward in chunks of :data:`TRANSFORMER_CHUNK_BYTES`. Peak memory is one float64
layer plus one row's activations, ~0.3 GB at the largest accepted corner
(width 1024, seq_len 2048; it was ~4.9 GB with a whole float64 model and 4 rows
of 16 x 2048 x 2048 attention scores at once).
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Mapping
from typing import Any

from ._support import (
    Check,
    Kernel,
    floats_digest,
    int_param,
    require_torch,
    shape_guard,
    synchronize,
    worse,
)

__all__ = [
    "KERNELS",
    "ML_FACTOR",
    "REFERENCE_CHUNK_BYTES",
    "REFERENCE_FLOPS",
    "SAMPLED_ROWS",
    "TRAIN_REFERENCE_ELEMENTS",
    "TRANSFORMER_CHUNK_BYTES",
    "cnn_layout",
    "float32_eps",
    "ml_tolerance",
    "reference_rows",
]

CLASSES = 10
POOL = 4
"""Distinct synthetic batches cycled through (data stays on the device)."""
VOCAB = 1024

ML_FACTOR = 16.0
"""Tolerance of the float64 reference in units of
``eps * sqrt(layers) * max|reference row|`` (:func:`ml_tolerance`; for the
training loss the scale is ``max(|loss|, 1)``).

Measured on Laptop 1 (2026-10-17, torch 2.14 CPU, float32 timed model vs the
float64 seeded rebuild; 4 shapes per family from tiny up to width 128-512 /
depth 12, 20 seeds each - docs/BENCHMARKS.md): the worst honest run used 1.05
units (cnn_infer), 2.85 (transformer_infer) and 1.42 (cnn_train first loss), so
factor 16 leaves >= 5.6x headroom; an output scaled by 1.0001 or one weight
changed by 1e-3 in the timed model fails.
On CUDA the unit is the TF32 rounding whenever cuDNN / cuBLAS may use TF32
(:func:`float32_eps`) - 8192x looser than float32. **The factor has not been
measured on CUDA yet** (first CUDA lab run, D225 open item)."""

REFERENCE_FLOPS = 1e11
"""Float64 FLOP budget of the inference reference: about 1 s on a laptop CPU.
The whole last batch is recomputed when it fits; otherwise evenly spaced rows,
always the first and the last, at least two, plus :data:`SAMPLED_ROWS` seeded
random ones (:func:`reference_rows`).

**The row floor overrides this budget** (D228.2): those >= 4 rows are
recomputed however expensive one row is, so at the corners ``check_ms`` can be
minutes - the largest window-valid transformer (batch 4, seq_len 2048, width
1024, depth 24) is 4 rows = ~6.6e12 FLOPs, 51 s on Laptop 1 (~2 min on the mid
laptop); since G4 item 0.1 its memory peak is ~0.33 GB (one row at a time,
module docstring; it was ~4.9 GB - docs/BENCHMARKS.md, "ML reference")."""

SAMPLED_ROWS = 2
"""Rows of the last batch drawn at random (seeded by the run seed, never the
first / last) on top of the evenly spaced ones when the budget forces sampling."""


TRAIN_REFERENCE_ELEMENTS = 2**24
"""Largest first-batch activation (``batch * width * image**2`` values, layer 0)
``ml.cnn_train`` accepts, so its float64 first-loss reference stays under
**512 MB** (G3 item 0.3, D228.1).

Why a cap and not only chunking: batch norm in train mode normalises with the
statistics of the *whole* batch, so a layer's output for one row needs every
row's conv output first. Chunking rows therefore cannot drop below one full
float64 activation per layer in and one out; the only way under it is to
recompute the layer prefix per chunk (up to ``depth + 1`` forward passes,
~2.6e13 float64 FLOPs = 5-11 min of check at the old corner - longer than
the job may take). The old corner (batch 512, width 256, image 64) was
4 GiB per float64 activation, ~44 GiB peak with the plain forward (the CPU
float64 conv's im2col buffer is 9x the input). With the layer-wise forward the
peak is ~2 activations + one chunk: at this cap 2 x 128 MiB + <= 80 MiB, measured
~0.3-0.4 GiB (docs/BENCHMARKS.md). The cap also keeps the timed run's training
memory near 1 GB of VRAM, inside every laptop GPU."""

REFERENCE_CHUNK_BYTES = 64 * 2**20
"""Target size of one conv chunk's temporaries in the training reference
(im2col buffer + chunk output, float64). At least one row per chunk, so a
single row above it (9 x 256 channels x 64 x 64 x 8 B = 72 MiB at most) still
runs."""


TRANSFORMER_CHUNK_BYTES = 16 * 2**20
"""Largest temporary of the transformer float64 reference (one attention score
chunk, one feed-forward hidden chunk; G4 item 0.1)."""


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


# ------------------------------------------------------------- the reference


def float32_eps(torch: Any, device: str, *, conv: bool) -> float:
    """Unit roundoff the timed float32 model may really have used.

    CPU: float32 (2**-24). CUDA: TF32 (2**-11) when cuDNN convolutions
    (``conv``) or cuBLAS matmuls may use it - cuDNN's default is on - and
    bfloat16 (2**-8) under ``set_float32_matmul_precision("medium")``.
    """
    if device != "cuda":
        return 2.0**-24
    precision = torch.get_float32_matmul_precision()
    if precision == "medium":
        return 2.0**-8
    if (
        precision != "highest"
        or bool(torch.backends.cuda.matmul.allow_tf32)
        or (conv and bool(torch.backends.cudnn.allow_tf32))
    ):
        return 2.0**-11
    return 2.0**-24


def ml_tolerance(eps: float, layers: int, scale: Any) -> Any:
    """Allowed ``|timed - float64 reference|``: ``ML_FACTOR * eps *
    sqrt(layers) * scale`` (rounding grows about like the square root of the
    number of layers it passes through). ``scale`` is a float (training loss)
    or a per-row tensor (inference); the result has the same type."""
    return ML_FACTOR * eps * math.sqrt(max(layers, 1)) * scale


def reference_rows(batch: int, row_flops: float, seed: int = 0) -> list[int]:
    """Rows of the last batch recomputed in float64.

    All of them when they fit :data:`REFERENCE_FLOPS`. Otherwise (D225 tester
    T1) evenly spaced rows - first and last always, at least two - **plus**
    :data:`SAMPLED_ROWS` more drawn from the remaining rows by a generator
    seeded with the run's ``seed``: a fault confined to fixed middle rows cannot
    hide from every run of a campaign, while one run stays reproducible.
    """
    fits = int(REFERENCE_FLOPS // max(row_flops, 1.0))
    count = min(batch, max(2, fits))
    if count >= batch:
        return list(range(batch))
    spread = {round(i * (batch - 1) / (count - 1)) for i in range(count)}
    rest = [row for row in range(batch) if row not in spread]
    # Reproducible row choice, not a security boundary: a seeded PRNG is right.
    rng = random.Random(seed)  # noqa: S311
    picked = rng.sample(rest, min(SAMPLED_ROWS, len(rest)))
    return sorted(spread.union(picked))


def _seeded_model(state: Mapping[str, Any]) -> Any:
    """The model rebuilt from the seed on the CPU (float32, before ``.double()``).

    Uses only the CPU generator - module initialisation always runs on the CPU,
    also in ``prepare`` before ``.to(device)`` - inside ``fork_rng`` so the
    process's random state is left as it was.
    """
    torch = state["torch"]
    with torch.random.fork_rng(devices=[]):
        torch.default_generator.manual_seed(state["seed"])
        builder: Callable[[], Any] = state["build"]
        return builder()


def _weights_mismatch(timed: Any, reference: Any) -> str | None:
    """The first parameter / buffer of ``timed`` that is not bit-identical to
    ``reference`` (None when all are)."""
    mine: dict[str, Any] = dict(timed.state_dict())
    theirs: dict[str, Any] = dict(reference.state_dict())
    if list(mine) != list(theirs):
        return "the parameter names"
    for name, value in mine.items():
        other = theirs[name]
        got = value.detach().cpu()
        if got.shape != other.shape or got.dtype != other.dtype:
            return str(name)
        if not bool((got == other).all()):
            return str(name)
    return None


def _inference_reference(state: Any, out: Any) -> tuple[float, int] | Check:
    """Float64 recompute of rows of the last batch; ``(worst units, rows)`` or a
    failed :class:`Check` when the timed model is not the seeded one."""
    torch = state["torch"]
    reference_of: Callable[[Any, Any], Any | str] = state.get(
        "reference", _whole_model_reference
    )
    reference = reference_of(state, state["model"])
    if isinstance(reference, str):
        return Check(
            ok=False,
            checksum=floats_digest([]),
            exact=False,
            detail=f"timed model differs from the seeded build at {reference}",
        )
    x = state["xs"][(state["batches"] - 1) % POOL]
    rows = reference_rows(int(x.shape[0]), state["row_flops"], state["seed"])
    picked = x[rows].cpu()
    if picked.is_floating_point():
        picked = picked.double()
    with torch.no_grad():
        expected = reference(picked).flatten(1)
    got = out[rows].detach().cpu().double().flatten(1)
    scale = expected.abs().amax(dim=1, keepdim=True)
    eps = float32_eps(torch, state["device"], conv=state["conv"])
    # one tolerance rule for inference and training (D228.4); ``scale`` is a
    # per-row tensor here, the formula is elementwise
    bound = ml_tolerance(eps, state["layers"], scale) + 1e-300
    return worse(0.0, float(((got - expected).abs() / bound).max())), len(rows)


def _whole_model_reference(state: Any, timed: Any) -> Any:
    """The seeded build as one float64 module (the CNN), or the name of the
    first parameter where ``timed`` differs from it."""
    reference = _seeded_model(state)
    mismatch = _weights_mismatch(timed, reference)
    if mismatch is not None:
        return mismatch
    return reference.double().eval()


# --------------------------------------------------------------------- cnn


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
    state: dict[str, Any] = {
        "torch": torch,
        "device": device,
        "model": model,
        "xs": xs,
        "seed": seed,
        "build": lambda: _build_cnn(torch, width, depth, image),
        "layers": depth + 1,
        "conv": True,
        "row_flops": sum(
            2.0 * 9 * c_in * c_out * res**2
            for c_in, c_out, res in cnn_layout(width, depth, image)
        ),
    }
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


def _inference_check(
    state: Any, out: Any, shape: tuple[int, ...], digits: int
) -> Check:
    """Shared by both inference checks (rules in :mod:`._support`): shape/dtype
    guard first, every value finite, the last batch run again gives the same
    output, and the float64 seeded reference agrees (module docstring)."""
    torch = state["torch"]
    guard = shape_guard(out, shape, torch.float32)
    if guard is not None:
        return guard
    if not bool(torch.isfinite(out).all()):
        return Check(False, floats_digest([]), False, "non-finite values in the output")
    with torch.inference_mode():
        again = state["model"](state["xs"][(state["batches"] - 1) % POOL])
    rerun = worse(0.0, float((again - out).abs().max()))
    reference = _inference_reference(state, out)
    if isinstance(reference, Check):
        return reference
    units, rows = reference
    reduced = out.double().sum(dim=tuple(range(out.dim() - 1))).tolist()[:64]
    return Check(
        ok=rerun <= 1e-3 and units <= 1.0,
        checksum=floats_digest([round(float(v), digits) for v in reduced]),
        exact=False,
        detail=f"finite, shape {tuple(out.shape)}, rerun max diff {rerun:.3e}, "
        f"float64 reference {units:.3f} of tolerance on {rows} row(s)",
    )


def _infer_check(state: Any, out: Any) -> Check:
    """Finite logits of the right shape; rerun equal; float64 reference agrees."""
    return _inference_check(state, out, (state["xs"][0].shape[0], CLASSES), 4)


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


def _conv_in_chunks(torch: Any, conv: Any, x: Any) -> Any:
    """``conv(x)`` computed a chunk of rows at a time into one output tensor
    (rows of a convolution are independent; only the temporaries shrink)."""
    batch, c_in, height, width = (int(v) for v in x.shape)
    c_out = int(conv.out_channels)
    per_row = (9 * c_in + c_out) * height * width * 8
    rows = max(1, REFERENCE_CHUNK_BYTES // per_row)
    if rows >= batch:
        return conv(x)
    out = torch.empty((batch, c_out, height, width), dtype=x.dtype)
    for start in range(0, batch, rows):
        out[start : start + rows] = conv(x[start : start + rows])
    return out


def _train_reference_forward(torch: Any, model: Any, x: Any) -> Any:
    """``model(x)`` for the float64 CNN, layer by layer: convolutions in row
    chunks, ReLU in place, batch norm (train mode, whole-batch statistics) and
    the rest as the module computes them. Only the current input and output
    activation are alive at any time."""
    nn = torch.nn
    for module in model:
        if isinstance(module, nn.Conv2d):
            x = _conv_in_chunks(torch, module, x)
        elif isinstance(module, nn.ReLU):
            x = x.relu_()
        else:
            x = module(x)
    return x


def _first_loss_reference(state: Any) -> float:
    """The first timed step's loss recomputed in float64 on the seeded build
    (train mode, whole first batch, :func:`_train_reference_forward`)."""
    torch = state["torch"]
    reference = _seeded_model(state).double().train()
    with torch.no_grad():
        logits = _train_reference_forward(
            torch, reference, state["xs"][0].cpu().double()
        )
        loss = torch.nn.functional.cross_entropy(logits, state["ys"][0].cpu())
    return float(loss)


def _train_check(state: Any, losses: Any) -> Check:
    """One finite loss per step; the first equals the float64 seeded recompute;
    they went down (mean of the last quarter < first quarter). Shape/dtype
    first."""
    torch = state["torch"]
    guard = shape_guard(losses, (state["steps"],), torch.float32)
    if guard is not None:
        return guard
    values = [float(v) for v in losses.double().cpu().tolist()]
    if not all(math.isfinite(v) for v in values):
        return Check(False, floats_digest([]), False, "non-finite losses")
    expected = _first_loss_reference(state)
    eps = float32_eps(torch, state["device"], conv=True)
    allowed = ml_tolerance(eps, state["layers"], max(abs(expected), 1.0))
    units = worse(0.0, abs(values[0] - expected) / allowed)
    quarter = max(1, len(values) // 4)
    first = sum(values[:quarter]) / quarter
    last = sum(values[-quarter:]) / quarter
    return Check(
        ok=last < first and units <= 1.0,
        checksum=floats_digest([round(v, 4) for v in values]),
        exact=False,
        detail=f"loss {first:.4f} -> {last:.4f} over {len(values)} steps, first "
        f"loss vs float64 reference {units:.3f} of tolerance",
    )


# ---------------------------------------------------------------- transformer


def transformer_heads(width: int) -> int:
    """Attention heads for ``d_model = width`` (64 per head, at least one)."""
    return max(1, width // 64)


def _transformer_parts(torch: Any, width: int) -> tuple[Any, Any]:
    """The seeded parts, in initialisation order: the embedding, then the one
    encoder layer ``nn.TransformerEncoder`` deep-copies ``depth`` times (the
    copies draw no random numbers, so every layer starts equal to it)."""
    nn = torch.nn
    embedding = nn.Embedding(VOCAB, width)
    layer = nn.TransformerEncoderLayer(
        width, transformer_heads(width), 4 * width, dropout=0.0, batch_first=True
    )
    return embedding, layer


def _build_transformer(torch: Any, width: int, depth: int) -> Any:
    nn = torch.nn
    embedding, layer = _transformer_parts(torch, width)
    return nn.Sequential(
        embedding,
        nn.TransformerEncoder(layer, depth, enable_nested_tensor=False),
    )


_LAYER_PREFIX = "1.layers."


def _transformer_mismatch(
    timed: Any, embedding: Any, layer: Any, depth: int
) -> str | None:
    """The first parameter / buffer of the timed transformer that is not
    bit-identical to the seeded embedding / layer (None when all are). One
    tensor is copied to the CPU at a time."""
    own = {f"0.{k}": v for k, v in embedding.state_dict().items()}
    per_layer = dict(layer.state_dict())
    expected = list(own) + [
        f"{_LAYER_PREFIX}{i}.{k}" for i in range(depth) for k in per_layer
    ]
    mine: dict[str, Any] = dict(timed.state_dict())
    if list(mine) != expected:
        return "the parameter names"
    for name, value in mine.items():
        # "1.layers.<i>.<key>" -> the one seeded layer's "<key>"
        other = own.get(name)
        if other is None:
            other = per_layer[name[len(_LAYER_PREFIX) :].split(".", 1)[1]]
        got = value.detach().cpu()
        if got.shape != other.shape or got.dtype != other.dtype:
            return str(name)
        if not bool((got == other).all()):
            return str(name)
    return None


def _chunk_rows(per_row_bytes: int, n: int) -> int:
    """Rows per chunk so one chunk's temporary stays within
    :data:`TRANSFORMER_CHUNK_BYTES` (at least one row)."""
    return max(1, min(n, TRANSFORMER_CHUNK_BYTES // max(per_row_bytes, 1)))


def _encoder_layer_f64(torch: Any, layer: Any, x: Any, heads: int) -> Any:
    """One post-norm ``nn.TransformerEncoderLayer`` (ReLU, no dropout, no mask)
    on one row ``x`` (``seq_len x width``, float64), by hand from its weights.
    Attention runs in query chunks (scores ``heads x chunk x seq_len``, softmax
    in place) and the feed-forward in row chunks, so no temporary exceeds
    :data:`TRANSFORMER_CHUNK_BYTES`."""
    functional = torch.nn.functional
    n, width = (int(v) for v in x.shape)
    head_dim = width // heads
    attention = layer.self_attn
    qkv = functional.linear(x, attention.in_proj_weight, attention.in_proj_bias)
    q, k, v = (
        part.reshape(n, heads, head_dim).transpose(0, 1).contiguous()
        for part in qkv.split(width, dim=1)
    )
    del qkv
    q.mul_(1.0 / math.sqrt(head_dim))
    keys = k.transpose(1, 2)
    mixed = torch.empty((n, heads, head_dim), dtype=x.dtype)
    step = _chunk_rows(heads * n * 8, n)
    for start in range(0, n, step):
        scores = torch.bmm(q[:, start : start + step], keys)
        scores.sub_(scores.amax(dim=-1, keepdim=True)).exp_()
        scores.div_(scores.sum(dim=-1, keepdim=True))
        mixed[start : start + step] = torch.bmm(scores, v).transpose(0, 1)
        del scores
    del q, k, v, keys
    attended = functional.linear(
        mixed.reshape(n, width), attention.out_proj.weight, attention.out_proj.bias
    )
    del mixed
    norm1, norm2 = layer.norm1, layer.norm2
    x = functional.layer_norm(
        attended.add_(x), (width,), norm1.weight, norm1.bias, norm1.eps
    )
    out = torch.empty_like(x)
    step = _chunk_rows(int(layer.linear1.out_features) * 8, n)
    for start in range(0, n, step):
        part = x[start : start + step]
        hidden = functional.linear(part, layer.linear1.weight, layer.linear1.bias)
        fed = functional.linear(
            hidden.relu_(), layer.linear2.weight, layer.linear2.bias
        )
        out[start : start + step] = functional.layer_norm(
            fed.add_(part), (width,), norm2.weight, norm2.bias, norm2.eps
        )
        del hidden, fed
    return out


def _transformer_reference_forward(
    torch: Any, embedding: Any, layer: Any, depth: int, tokens: Any
) -> Any:
    """``model(tokens)`` in float64 for the seeded transformer: the float64
    ``embedding`` and ``layer`` (applied ``depth`` times), **one row at a
    time** (rows of an encoder without a mask are independent). ``tokens`` is
    ``rows x seq_len`` (CPU, int64); returns ``rows x seq_len x width``."""
    heads = transformer_heads(int(embedding.weight.shape[1]))
    out = torch.empty(
        (int(tokens.shape[0]), int(tokens.shape[1]), int(embedding.weight.shape[1])),
        dtype=torch.float64,
    )
    with torch.no_grad():
        for row in range(int(tokens.shape[0])):
            x = embedding.weight[tokens[row]]
            for _ in range(depth):
                x = _encoder_layer_f64(torch, layer, x, heads)
            out[row] = x
    return out


def _transformer_reference(state: Any, timed: Any) -> Any:
    """The seeded transformer's float64 reference as a callable
    (``tokens -> output``), or the name of the first parameter where
    ``timed`` differs from the seed (module docstring)."""
    torch = state["torch"]
    with torch.random.fork_rng(devices=[]):
        torch.default_generator.manual_seed(state["seed"])
        embedding, layer = _transformer_parts(torch, state["width"])
    depth = state["depth"]
    mismatch = _transformer_mismatch(timed, embedding, layer, depth)
    if mismatch is not None:
        return mismatch
    embedding, layer = embedding.double().eval(), layer.double().eval()

    def forward(tokens: Any) -> Any:
        return _transformer_reference_forward(torch, embedding, layer, depth, tokens)

    return forward


def _transformer_prepare(params: Mapping[str, Any], seed: int, device: str) -> Any:
    torch = require_torch(device)
    torch.manual_seed(seed)
    batch, seq_len, width, depth = (
        int_param(params, k) for k in ("batch", "seq_len", "width", "depth")
    )
    model = _build_transformer(torch, width, depth).to(device)
    model.eval()
    generator = torch.Generator(device=device)
    generator.manual_seed(seed)
    tokens = [
        torch.randint(0, VOCAB, (batch, seq_len), generator=generator, device=device)
        for _ in range(POOL)
    ]
    per_layer = 24.0 * seq_len * width * width + 4.0 * seq_len * seq_len * width
    return {
        "torch": torch,
        "device": device,
        "model": model,
        "xs": tokens,
        "batches": int_param(params, "batches"),
        "width": width,
        "depth": depth,
        "seed": seed,
        "build": lambda: _build_transformer(torch, width, depth),
        "reference": _transformer_reference,
        "layers": 2 * depth,
        "conv": False,
        "row_flops": per_layer * depth,
    }


def _transformer_check(state: Any, out: Any) -> Check:
    batch, seq_len = state["xs"][0].shape
    return _inference_check(state, out, (batch, seq_len, state["width"]), 3)


KERNELS: dict[str, Kernel] = {
    "ml.cnn_infer": Kernel(_infer_prepare, _infer_compute, _infer_check, _infer_warmup),
    "ml.cnn_train": Kernel(_train_prepare, _train_compute, _train_check, _train_warmup),
    "ml.transformer_infer": Kernel(
        _transformer_prepare, _infer_compute, _transformer_check, _infer_warmup
    ),
}
