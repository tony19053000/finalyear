"""V0.9 G2 (0.9.2, D214): the workload-family contract, registry and size discipline.

Pure logic: no kernel runs here (see ``test_v09_g2_kernels.py``), so these pass
on a plain install without numpy or torch.
"""

from __future__ import annotations

import itertools
import json
import math
import subprocess
import sys
from typing import Any

import pytest

from meshforge.bench.workloads import (
    CALIBRATION_HW,
    MAX_RUNTIME_S,
    MID_LAPTOP,
    MIN_RUNTIME_S,
    Family,
    HardwareHint,
    Kind,
    ParamsError,
    Unavailable,
    UnknownFamilyError,
    WorkloadResult,
    get_family,
    list_families,
)
from meshforge.bench.workloads.base import register
from meshforge.common.errors import FailureType

pytestmark = pytest.mark.unit

EXPECTED = {
    "cpu.matmul": Kind.CPU,
    "cpu.primes": Kind.CPU,
    "cpu.sort": Kind.CPU,
    "cpu.zlib": Kind.CPU,
    "cpu.sha256": Kind.CPU,
    "cpu.integrate": Kind.CPU,
    "memory.copy": Kind.MEMORY,
    "memory.groupby": Kind.MEMORY,
    "gpu.matmul": Kind.GPU,
    "gpu.conv2d": Kind.GPU,
    "gpu.elementwise": Kind.GPU,
    "gpu.resize": Kind.GPU,
    "ml.cnn_infer": Kind.ML,
    "ml.cnn_train": Kind.ML,
    "ml.transformer_infer": Kind.ML,
}
FAMILIES = list_families()
IDS = [f.name for f in FAMILIES]


def test_the_v1_family_set_is_registered_sorted_by_name() -> None:
    assert [f.name for f in FAMILIES] == sorted(EXPECTED)
    assert {f.name: f.kind for f in FAMILIES} == EXPECTED


def test_an_unknown_family_is_a_typed_error() -> None:
    with pytest.raises(UnknownFamilyError) as info:
        get_family("cpu.nope")
    assert info.value.failure_type is FailureType.INVALID_INPUT
    assert "compute bench workloads" in str(info.value)


def test_a_name_cannot_be_registered_twice() -> None:
    with pytest.raises(ValueError, match="registered twice"):
        register(get_family("cpu.sort"))


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_defaults_are_the_smallest_valid_run(family: Family[Any]) -> None:
    """The defaults sit at the bottom of the 5 s - 3 min window on the mid laptop."""
    params = family.smallest_params()
    hint = family.expected_runtime_s(params)
    assert MIN_RUNTIME_S <= hint <= 2.5 * MIN_RUNTIME_S, hint
    assert params == family.params_model()


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_size_params_exist_and_have_bounds(family: Family[Any]) -> None:
    fields = family.params_model.model_fields
    assert family.size_params
    for name in family.size_params:
        assert name in fields, name
        schema = family.params_model.model_json_schema()["properties"][name]
        assert schema["type"] == "integer"
        assert schema["minimum"] < schema["maximum"]


def _ladder(low: int, high: int, steps: int = 8) -> list[int]:
    """``steps`` distinct integers from ``low`` to ``high``, geometric."""
    values = {round(low * (high / low) ** (i / (steps - 1))) for i in range(steps)} | {
        low,
        high,
    }
    return sorted(values)


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_the_hint_grows_with_every_size_param(family: Family[Any]) -> None:
    """Brief §5c item 4: the hint is monotonic in size (strictly increasing)."""
    base = family.params_dict(family.smallest_params())
    schema = family.params_model.model_json_schema()["properties"]
    for name in family.size_params:
        hints = []
        for value in _ladder(schema[name]["minimum"], schema[name]["maximum"]):
            params = {**base, name: value}
            if name == "width" and family.name == "ml.transformer_infer":
                params[name] = max(64, value // 64 * 64)
            hints.append(family.expected_runtime_s(params))
        hints = sorted(set(hints), key=hints.index)
        assert all(a < b for a, b in itertools.pairwise(hints)), (name, hints)


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_parse_params_refuses_runs_outside_the_window(family: Family[Any]) -> None:
    schema = family.params_model.model_json_schema()["properties"]
    biggest = {
        **family.params_dict(family.smallest_params()),
        **{name: schema[name]["maximum"] for name in family.size_params},
    }
    if family.name == "ml.transformer_infer":
        biggest["width"] = 1024
    assert family.expected_runtime_s(biggest) > MAX_RUNTIME_S
    with pytest.raises(ParamsError) as info:
        family.parse_params(biggest)
    assert "reference mid laptop" in str(info.value)
    assert info.value.failure_type is FailureType.INVALID_INPUT
    smallest = {
        **family.params_dict(family.smallest_params()),
        **{name: schema[name]["minimum"] for name in family.size_params},
    }
    if family.name == "ml.transformer_infer":
        smallest["width"] = 64
    assert family.expected_runtime_s(smallest) < MIN_RUNTIME_S
    with pytest.raises(ParamsError):
        family.parse_params(smallest)


def test_a_valid_mid_window_run_is_accepted() -> None:
    family = get_family("cpu.matmul")
    params = family.parse_params({"n": 4096, "repeats": 40})
    assert MIN_RUNTIME_S < family.expected_runtime_s(params) < MAX_RUNTIME_S


@pytest.mark.parametrize(
    ("raw", "where"),
    [
        ({"n": 100}, "n:"),
        ({"n": 5000}, "n:"),
        ({"n": "2048"}, "n:"),  # strict: no string coercion
        ({"n": 2048.0}, "n:"),
        ({"n": True}, "n:"),
        ({"dtype": "float16"}, "dtype:"),
        ({"bogus": 1}, "bogus:"),
    ],
)
def test_bad_params_name_the_field(raw: dict[str, Any], where: str) -> None:
    with pytest.raises(ParamsError) as info:
        get_family("cpu.matmul").parse_params(raw)
    assert any(p.startswith(where) for p in info.value.problems), info.value.problems


def test_transformer_width_must_be_a_multiple_of_64() -> None:
    with pytest.raises(ParamsError, match="multiple of 64"):
        get_family("ml.transformer_infer").parse_params({"width": 100})


def test_a_faster_node_gets_a_proportionally_shorter_hint() -> None:
    family = get_family("cpu.sort")
    params = family.smallest_params()
    slow = HardwareHint(cpu_fp32_gflops_1t=50.0)
    fast = HardwareHint(cpu_fp32_gflops_1t=100.0)
    assert family.expected_runtime_s(params, slow) == pytest.approx(
        2 * family.expected_runtime_s(params, fast)
    )


@pytest.mark.parametrize("family", [f for f in FAMILIES if f.gpu], ids=lambda f: f.name)
def test_a_gpu_hint_on_a_node_without_a_gpu_score_is_unavailable(
    family: Family[Any],
) -> None:
    with pytest.raises(Unavailable, match="gpu_"):
        family.expected_runtime_s(family.smallest_params(), CALIBRATION_HW)


def test_hardware_hint_from_scores_ignores_unknown_metrics() -> None:
    hint = HardwareHint.from_scores({"mem_triad_gb_s": 12.5, "future_metric": 3.0})
    assert hint.mem_triad_gb_s == 12.5
    assert hint.per_second("mem_triad_gb_s") == 12.5e9
    with pytest.raises(Unavailable):
        hint.per_second("cpu_fp32_gflops_1t")


def test_the_calibration_laptop_is_faster_than_the_mid_laptop() -> None:
    for family in FAMILIES:
        if family.gpu:
            continue
        params = family.smallest_params()
        assert family.expected_runtime_s(params, CALIBRATION_HW) < (
            family.expected_runtime_s(params, MID_LAPTOP)
        ), family.name


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_memory_needs_and_environment(family: Family[Any]) -> None:
    params = family.smallest_params()
    assert 0 < family.ram_gb(params) < 16
    vram = family.vram_gb(params)
    if family.gpu:
        assert family.env_base == "cuda-pytorch"
        assert vram is not None and 0 < vram <= 4, vram  # fits an entry laptop GPU
        assert not family.needs_numpy  # cuda-pytorch promises torch, not numpy
    else:
        assert family.env_base == "python-cpu"
        assert vram is None


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_describe_is_json_and_complete(family: Family[Any]) -> None:
    entry = family.describe()
    assert json.loads(json.dumps(entry)) == entry
    assert entry["name"] == family.name
    assert entry["smallest"] == family.params_dict(family.smallest_params())
    assert entry["runtime_window_s"] == [MIN_RUNTIME_S, MAX_RUNTIME_S]
    assert set(entry["params_schema"]["properties"]) == set(
        family.params_model.model_fields
    )
    assert entry["gpu"] == ("required" if family.gpu else "none")


def _result(**overrides: Any) -> WorkloadResult:
    document: dict[str, Any] = {
        "schema_version": 1,
        "family": "cpu.sort",
        "params": {"n": 10_000_000, "repeats": 18},
        "seed": 7,
        "status": "ok",
        "compute_ns": 3_000_000_000,
        "compute_ms": 3000.0,
        "checksum": "sha256:" + "0" * 64,
        "checksum_exact": True,
        "verified": True,
    }
    document.update(overrides)
    return WorkloadResult.model_validate(document)


def test_verify_accepts_a_good_result() -> None:
    verdict = get_family("cpu.sort").verify(_result())
    assert verdict.ok and verdict.problems == ()


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"family": "cpu.zlib"}, "family"),
        ({"schema_version": 2}, "schema_version"),
        ({"status": "check_failed", "verified": False, "reason": "x"}, "status"),
        ({"verified": False}, "status"),
        ({"compute_ns": 0}, "compute time"),
        ({"compute_ns": None}, "compute time"),
        ({"checksum": None}, "checksum"),
        ({"params": {"n": 1}}, "n:"),
    ],
)
def test_verify_names_what_is_wrong(overrides: dict[str, Any], problem: str) -> None:
    verdict = get_family("cpu.sort").verify(_result(**overrides))
    assert not verdict.ok
    assert any(problem in p for p in verdict.problems), verdict.problems


def test_seed_out_of_range_is_refused_before_running() -> None:
    with pytest.raises(ParamsError, match="seed"):
        get_family("cpu.sort").run({}, -1)


def test_the_library_imports_and_lists_without_numpy_or_torch() -> None:
    """A plain install has neither: listing, validating and hinting must work."""
    code = (
        "import sys\n"
        "for name in ('numpy', 'torch'):\n"
        "    sys.modules[name] = None  # any import of them now fails\n"
        "from meshforge.bench.workloads import list_families, build_job\n"
        "from meshforge.cli.bench_commands import workloads_document\n"
        "doc = workloads_document()\n"
        "assert doc['count'] == 15, doc['count']\n"
        "print('ok', len(list_families()))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok 15"


def test_window_constants_match_the_brief() -> None:
    assert (MIN_RUNTIME_S, MAX_RUNTIME_S) == (5.0, 180.0)
    assert math.isfinite(MID_LAPTOP.per_second("gpu_fp32_tflops"))


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_job_side_limits_equal_the_family_models(family: Family[Any]) -> None:
    """Tester r1 D6: the job refuses what the library refuses (maxima, choices);
    its minimum is 1 because tiny runs test the kernels."""
    from meshforge.bench.workloads.kernels.runner import PARAM_LIMITS, PARAM_MINIMA

    limits = PARAM_LIMITS[family.name]
    schema = family.params_model.model_json_schema()["properties"]
    assert set(limits) == set(schema)
    for name, spec in schema.items():
        if "enum" in spec:
            assert tuple(spec["enum"]) == limits[name], name
        else:
            assert spec["maximum"] == limits[name], name
            assert spec["minimum"] >= 1, name
            # Tester r2 D3: a job-side minimum above 1 never exceeds the model's.
            assert PARAM_MINIMA.get((family.name, name), 1) <= spec["minimum"], name


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_the_job_side_refuses_zero_and_above_the_maximum(family: Family[Any]) -> None:
    from meshforge.bench.workloads.kernels._support import KernelParamsError
    from meshforge.bench.workloads.kernels.runner import PARAM_LIMITS, execute

    base = family.params_dict(family.smallest_params())
    for name, limit in PARAM_LIMITS[family.name].items():
        bad_values = [0, -1, limit + 1] if isinstance(limit, int) else ["nope"]
        for bad in bad_values:
            with pytest.raises(KernelParamsError, match=name):
                execute(family.name, {**base, name: bad}, 1)
    with pytest.raises(KernelParamsError, match="unknown parameter"):
        execute(family.name, {**base, "extra": 1}, 1)
    for seed in (-1, 2**32, True, 1.5):
        with pytest.raises(KernelParamsError, match="seed"):
            execute(family.name, base, seed)  # type: ignore[arg-type]
