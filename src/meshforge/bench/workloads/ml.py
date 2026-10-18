"""ML workload families (torch, CUDA required; V0.9 G2, D214): small CNN
inference and training, small transformer inference.

The FLOP counts use the same layer layout the kernels build
(:func:`.kernels.ml.cnn_layout`), so hint and model cannot drift apart. The
efficiencies (fraction of the GPU's fp32 TFLOP/s a small model reaches) are
**assumptions** until the first CUDA lab run - see :mod:`.gpu`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from .base import Family, Kind, Metric, Params, register, round_up_gb
from .kernels.ml import TRAIN_REFERENCE_ELEMENTS, cnn_layout

__all__ = ["CnnInferFamily", "CnnTrainFamily", "TransformerInferFamily"]

_CONTEXT_VRAM_GB = 0.5


def _cnn_flops(width: int, depth: int, image: int, batch: int) -> float:
    """Forward FLOPs of one batch (convolutions; the head is negligible)."""
    return (
        sum(
            2.0 * 9 * c_in * c_out * res**2
            for c_in, c_out, res in cnn_layout(width, depth, image)
        )
        * batch
    )


def _cnn_vram(width: int, depth: int, image: int, batch: int, *, train: bool) -> float:
    # Activations: every layer's output kept (training) or ~2 alive (inference).
    per_layer = [
        batch * c_out * res**2 * 4 for _c, c_out, res in cnn_layout(width, depth, image)
    ]
    live = sum(per_layer) * 3 if train else 2 * max(per_layer)
    return round_up_gb(_CONTEXT_VRAM_GB + live / 1e9)


class _MlFamily[P: Params](Family[P]):
    kind = Kind.ML

    def ram_gb(self, params: P) -> float:
        return 2.0

    def metric(self, params: P) -> Metric:
        return "gpu_fp32_tflops"


class CnnInferParams(Params):
    batch: int = Field(64, ge=1, le=512)
    width: int = Field(64, ge=16, le=256, description="channels per conv layer")
    depth: int = Field(6, ge=2, le=12, description="conv + batchnorm + relu blocks")
    image: Literal[32, 64] = 32
    batches: int = Field(1000, ge=1, le=100_000, description="forward passes timed")


class CnnInferFamily(_MlFamily[CnnInferParams]):
    name = "ml.cnn_infer"
    description = "small CNN inference on synthetic images (torch, eval); rerun check"
    params_model = CnnInferParams
    size_params = ("batch", "width", "depth", "batches")

    def work_units(self, params: CnnInferParams) -> float:
        flops = _cnn_flops(params.width, params.depth, params.image, params.batch)
        return flops * params.batches

    def efficiency(self, params: CnnInferParams) -> float:
        return 0.25

    def vram_gb(self, params: CnnInferParams) -> float:
        return _cnn_vram(
            params.width, params.depth, params.image, params.batch, train=False
        )


class CnnTrainParams(Params):
    batch: int = Field(64, ge=8, le=512)
    width: int = Field(64, ge=16, le=256)
    depth: int = Field(6, ge=2, le=12)
    image: Literal[32, 64] = 32
    steps: int = Field(400, ge=20, le=50_000, description="SGD steps timed")


class CnnTrainFamily(_MlFamily[CnnTrainParams]):
    name = "ml.cnn_train"
    description = (
        "small CNN training steps (SGD, synthetic labels); loss-decreases check"
    )
    params_model = CnnTrainParams
    size_params = ("batch", "width", "depth", "steps")

    def work_units(self, params: CnnTrainParams) -> float:
        # forward + backward ~ 3x forward
        flops = _cnn_flops(params.width, params.depth, params.image, params.batch)
        return 3.0 * flops * params.steps

    def efficiency(self, params: CnnTrainParams) -> float:
        return 0.2

    def vram_gb(self, params: CnnTrainParams) -> float:
        return _cnn_vram(
            params.width, params.depth, params.image, params.batch, train=True
        )

    def combination_problems(self, params: CnnTrainParams) -> list[str]:
        # The float64 first-loss check needs the whole first batch at once
        # (batch norm), so its memory follows the first activation (G3 0.3).
        values = params.batch * params.width * params.image**2
        if values <= TRAIN_REFERENCE_ELEMENTS:
            return []
        return [
            f"batch x width x image^2 is {values:,}; the float64 first-loss check "
            f"holds the whole first batch, so at most {TRAIN_REFERENCE_ELEMENTS:,} "
            "(512 MB of check memory) - lower batch, width or image"
        ]


class TransformerParams(Params):
    batch: int = Field(16, ge=1, le=256)
    seq_len: int = Field(256, ge=16, le=2048)
    width: int = Field(256, ge=64, le=1024, description="d_model (multiple of 64)")
    depth: int = Field(4, ge=1, le=24, description="encoder layers")
    batches: int = Field(300, ge=1, le=100_000, description="forward passes timed")

    @field_validator("width")
    @classmethod
    def _multiple_of_64(cls, value: int) -> int:
        if value % 64:
            raise ValueError("must be a multiple of 64 (64 dimensions per head)")
        return value


class TransformerInferFamily(_MlFamily[TransformerParams]):
    name = "ml.transformer_infer"
    description = (
        "small transformer encoder inference on random tokens (torch); rerun check"
    )
    params_model = TransformerParams
    size_params = ("batch", "seq_len", "width", "depth", "batches")

    def work_units(self, params: TransformerParams) -> float:
        b, n, d = params.batch, params.seq_len, params.width
        per_layer = 24.0 * b * n * d * d + 4.0 * b * n * n * d
        return per_layer * params.depth * params.batches

    def efficiency(self, params: TransformerParams) -> float:
        return 0.3

    def vram_gb(self, params: TransformerParams) -> float:
        b, n, d = params.batch, params.seq_len, params.width
        weights = params.depth * 12 * d * d * 4
        heads = max(1, d // 64)
        activations = b * n * 4 * d * 4 * 3 + b * heads * n * n * 4 * 2
        return round_up_gb(_CONTEXT_VRAM_GB + (weights + activations) / 1e9)


for _family in (CnnInferFamily(), CnnTrainFamily(), TransformerInferFamily()):
    register(_family)
