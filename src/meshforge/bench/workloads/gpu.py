"""GPU workload families (torch, CUDA required; V0.9 G2, D214): matmul, conv2d,
elementwise, image resize.

**Efficiencies are assumptions, not measurements**: Laptop 1's NVIDIA driver is
not usable yet, so no GPU family has been timed on real CUDA hardware. They are
typical fractions of a laptop GPU's score (dense matmul ~70 % of the measured
TFLOP/s, convolutions ~40 %, bandwidth-bound kernels ~30-70 % of the measured
GB/s). The first lab run on a CUDA laptop recalibrates them
(``docs/BENCHMARKS.md``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .base import Family, Kind, Metric, Params, register, round_up_gb

__all__ = ["ConvFamily", "ElementwiseFamily", "GpuMatmulFamily", "ResizeFamily"]

_CONTEXT_VRAM_GB = 0.5
"""CUDA context + allocator slack, before the family's own tensors."""


class _GpuFamily[P: Params](Family[P]):
    kind = Kind.GPU

    def ram_gb(self, params: P) -> float:
        return 2.0  # torch + CUDA runtime in host memory


class GpuMatmulParams(Params):
    n: int = Field(4096, ge=1024, le=12288, description="matrix side (n x n)")
    repeats: int = Field(200, ge=1, le=5000)
    dtype: Literal["float32", "float16"] = "float32"


class GpuMatmulFamily(_GpuFamily[GpuMatmulParams]):
    name = "gpu.matmul"
    description = "dense n x n matrix multiply on the GPU (torch); Freivalds check"
    params_model = GpuMatmulParams
    size_params = ("n", "repeats")

    def work_units(self, params: GpuMatmulParams) -> float:
        return 2.0 * params.n**3 * params.repeats

    def metric(self, params: GpuMatmulParams) -> Metric:
        return "gpu_fp32_tflops" if params.dtype == "float32" else "gpu_fp16_tflops"

    def efficiency(self, params: GpuMatmulParams) -> float:
        return 0.7

    def vram_gb(self, params: GpuMatmulParams) -> float:
        itemsize = 4 if params.dtype == "float32" else 2
        return round_up_gb(_CONTEXT_VRAM_GB + 3.5 * params.n**2 * itemsize / 1e9)


class ConvParams(Params):
    batch: int = Field(32, ge=1, le=256)
    channels: int = Field(128, ge=16, le=512, description="in = out channels")
    size: int = Field(64, ge=16, le=256, description="image side")
    repeats: int = Field(300, ge=1, le=5000)


class ConvFamily(_GpuFamily[ConvParams]):
    name = "gpu.conv2d"
    description = "3x3 convolution, padding 1 (torch / cuDNN); sampled float64 check"
    params_model = ConvParams
    size_params = ("batch", "channels", "size", "repeats")

    def work_units(self, params: ConvParams) -> float:
        return (
            2.0
            * 9
            * params.batch
            * params.channels**2
            * params.size**2
            * params.repeats
        )

    def metric(self, params: ConvParams) -> Metric:
        return "gpu_fp32_tflops"

    def efficiency(self, params: ConvParams) -> float:
        return 0.4

    def vram_gb(self, params: ConvParams) -> float:
        activations = 2 * params.batch * params.channels * params.size**2 * 4
        return round_up_gb(_CONTEXT_VRAM_GB + 1.5 * activations / 1e9)


class ElementwiseParams(Params):
    elements_m: int = Field(64, ge=16, le=256, description="millions of float32")
    ops: int = Field(16, ge=1, le=64, description="fused add-scale steps per pass")
    repeats: int = Field(60, ge=1, le=5000)


class ElementwiseFamily(_GpuFamily[ElementwiseParams]):
    name = "gpu.elementwise"
    description = (
        "chain of elementwise kernels (bandwidth-bound, torch); CPU float64 check"
    )
    params_model = ElementwiseParams
    size_params = ("elements_m", "ops", "repeats")

    def work_units(self, params: ElementwiseParams) -> float:
        # sin: read + write; each step reads x and y and writes y.
        return params.elements_m * 1e6 * 4 * (2 + 3 * params.ops) * params.repeats

    def metric(self, params: ElementwiseParams) -> Metric:
        return "gpu_mem_gb_s"

    def efficiency(self, params: ElementwiseParams) -> float:
        return 0.7

    def vram_gb(self, params: ElementwiseParams) -> float:
        return round_up_gb(_CONTEXT_VRAM_GB + 4 * params.elements_m * 1e6 * 4 / 1e9)


class ResizeParams(Params):
    batch: int = Field(64, ge=1, le=256, description="RGB images")
    size: int = Field(256, ge=64, le=1024, description="input side")
    out_size: int = Field(512, ge=32, le=2048, description="output side")
    repeats: int = Field(1100, ge=1, le=5000)


class ResizeFamily(_GpuFamily[ResizeParams]):
    name = "gpu.resize"
    description = "bilinear image resize (torch interpolate); CPU recompute check"
    params_model = ResizeParams
    size_params = ("batch", "size", "out_size", "repeats")

    def work_units(self, params: ResizeParams) -> float:
        pixels = params.size**2 + params.out_size**2
        return params.batch * 3 * pixels * 4.0 * params.repeats  # bytes moved

    def metric(self, params: ResizeParams) -> Metric:
        return "gpu_mem_gb_s"

    def efficiency(self, params: ResizeParams) -> float:
        return 0.3

    def vram_gb(self, params: ResizeParams) -> float:
        pixels = params.size**2 + params.out_size**2
        return round_up_gb(_CONTEXT_VRAM_GB + 2 * params.batch * 3 * pixels * 4 / 1e9)


for _family in (GpuMatmulFamily(), ConvFamily(), ElementwiseFamily(), ResizeFamily()):
    register(_family)
