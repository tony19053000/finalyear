"""V0.9 G2 tester round 1: adversarial contract tests for the workload library.

Derived from the brief (docs/briefs/V0.9-benchmark-telemetry.md section 5c, D214),
not from the implementation: strict params, the 5 s - 3 min window at its exact
edges, monotonic hints, byte-identical folders, odd destinations, odd seeds,
registry. The folder and import-contract tests are in
``test_v09_g2_tester_r1_folder.py`` (split in G3 item 0.2, D225.4).
"""

from __future__ import annotations

import itertools
import math
import subprocess
import sys
import typing
from typing import Any

import pytest

from meshforge.bench.workloads import (
    MAX_RUNTIME_S,
    MIN_RUNTIME_S,
    ParamsError,
    UnknownFamilyError,
    get_family,
    list_families,
)
from meshforge.bench.workloads import base as base_mod
from meshforge.bench.workloads.base import HardwareHint, Unavailable
from meshforge.bench.workloads.materialise import job_files

pytestmark = pytest.mark.unit

FAMILIES = list_families()
IDS = [f.name for f in FAMILIES]

EXPECTED_NAMES = {
    "cpu.matmul",
    "cpu.primes",
    "cpu.sort",
    "cpu.zlib",
    "cpu.sha256",
    "cpu.integrate",
    "memory.copy",
    "memory.groupby",
    "gpu.matmul",
    "gpu.conv2d",
    "gpu.elementwise",
    "gpu.resize",
    "ml.cnn_infer",
    "ml.cnn_train",
    "ml.transformer_infer",
}


def _int_bounds(family: Any, field: str) -> tuple[int, int] | None:
    info = family.params_model.model_fields[field]
    if info.annotation is not int:
        return None
    ge = next(m.ge for m in info.metadata if hasattr(m, "ge"))
    le = next(m.le for m in info.metadata if hasattr(m, "le"))
    return int(ge), int(le)


def _step(family: Any, field: str) -> int:
    """Granularity of a field (transformer width must be a multiple of 64)."""
    return 64 if (family.name, field) == ("ml.transformer_infer", "width") else 1


def _int_fields(family: Any) -> list[str]:
    return [
        n
        for n in family.params_model.model_fields
        if _int_bounds(family, n) is not None
    ]


def _literal_fields(family: Any) -> dict[str, tuple[Any, ...]]:
    out: dict[str, tuple[Any, ...]] = {}
    for name, info in family.params_model.model_fields.items():
        if typing.get_origin(info.annotation) is typing.Literal:
            out[name] = typing.get_args(info.annotation)
    return out


# ------------------------------------------------------------------ registry


def test_registry_is_exactly_the_fifteen_families_by_kind() -> None:
    names = [f.name for f in FAMILIES]
    assert set(names) == EXPECTED_NAMES
    assert names == sorted(names)
    kinds = [f.kind.value for f in FAMILIES]
    assert (
        kinds.count("cpu"),
        kinds.count("memory"),
        kinds.count("gpu"),
        kinds.count("ml"),
    ) == (6, 2, 4, 3)
    for family in FAMILIES:
        assert family.name.startswith(family.kind.value + ".")


def test_registering_a_duplicate_name_is_refused() -> None:
    with pytest.raises(ValueError, match="registered twice"):
        base_mod.register(get_family("cpu.sort"))


@pytest.mark.parametrize(
    "name",
    ["", "CPU.SORT", " cpu.sort", "cpu.sort ", "cpu.sort\x00", "cpu", "../x", "cpu.*"],
)
def test_unknown_family_names_raise_the_typed_error(name: str) -> None:
    with pytest.raises(UnknownFamilyError):
        get_family(name)
    with pytest.raises(UnknownFamilyError):
        job_files(name, {}, 1)


def test_importing_the_library_and_listing_does_not_import_numpy_or_torch() -> None:
    code = (
        "import sys\n"
        "from meshforge.bench.workloads import list_families, get_family\n"
        "for f in list_families():\n"
        "    f.parse_params({})\n"
        "    f.describe()\n"
        "bad = [m for m in ('numpy', 'torch', 'pandas') if m in sys.modules]\n"
        "print(bad)\n"
        "sys.exit(1 if bad else 0)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr


# ------------------------------------------------------------ strict params


def _junk_for(bounds: tuple[int, int]) -> list[Any]:
    low, high = bounds
    return [
        True,
        False,
        float(low) if low else 1.0,
        float(low) + 0.5,
        str(low),
        float("nan"),
        float("inf"),
        float("-inf"),
        10**100,
        -(10**100),
        high + 1,
        low - 1,
        None,
        [low],
        {"v": low},
    ]


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_odd_int_values_are_refused_with_params_error_only(family: Any) -> None:
    for field in _int_fields(family):
        bounds = _int_bounds(family, field)
        assert bounds is not None
        for junk in _junk_for(bounds):
            with pytest.raises(ParamsError) as caught:
                family.parse_params({field: junk})
            assert field in "; ".join(caught.value.problems), (field, junk)
            # the same refusal through every other entry point
            with pytest.raises(ParamsError):
                family.params_dict({field: junk})
            with pytest.raises(ParamsError):
                job_files(family.name, {field: junk}, 1)


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_odd_literal_values_unknown_keys_and_wrong_containers(family: Any) -> None:
    for field, choices in _literal_fields(family).items():
        for junk in (
            "nope",
            "",
            None,
            3.5,
            True,
            [choices[0]],
            "1" if isinstance(choices[0], int) else 1,
        ):
            if junk in choices and not isinstance(junk, bool):
                continue
            with pytest.raises(ParamsError):
                family.parse_params({field: junk})
    for raw in (
        {"nope": 1},
        {"seed": 1},
        {"__class__": "x"},
        {"": 1},
        None,
        [],
        "cpu",
        42,
        object(),
    ):
        with pytest.raises(ParamsError):
            family.parse_params(raw)


def test_a_params_instance_of_another_family_is_refused() -> None:
    other = get_family("cpu.sort").smallest_params()
    with pytest.raises(ParamsError):
        get_family("cpu.zlib").parse_params(other)


def test_a_value_inside_pydantic_bounds_but_outside_the_window_names_the_hint() -> None:
    family = get_family("cpu.matmul")
    with pytest.raises(ParamsError) as caught:
        family.parse_params({"n": 4096, "repeats": 500})
    text = " ".join(caught.value.problems)
    assert "reference mid laptop" in text
    assert "5-180" in text


# ---------------------------------------------------- window and the hint


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_the_window_edges_are_exact_for_every_size_parameter(family: Any) -> None:
    """Bisect each integer size parameter to its accepted interval; the value one
    step outside must be refused and the one inside must be inside [5, 180]."""
    defaults = family.params_dict(family.smallest_params())
    for field in family.size_params:
        bounds = _int_bounds(family, field)
        if bounds is None:
            continue
        step = _step(family, field)
        low, high = bounds[0] // step, bounds[1] // step

        def ok(value: int, field: str = field, step: int = step) -> bool:
            try:
                family.parse_params({**defaults, field: value * step})
            except ParamsError:
                return False
            return True

        mid = defaults[field] // step
        assert ok(mid)
        lo, hi = low, mid  # smallest accepted value in [low, mid]
        while lo < hi:
            probe = (lo + hi) // 2
            if ok(probe):
                hi = probe
            else:
                lo = probe + 1
        first = lo
        lo, hi = mid, high  # largest accepted value in [mid, high]
        while lo < hi:
            probe = (lo + hi + 1) // 2
            if ok(probe):
                lo = probe
            else:
                hi = probe - 1
        last = lo
        for edge in (first, last):
            hint = family.expected_runtime_s({**defaults, field: edge * step})
            assert MIN_RUNTIME_S <= hint <= MAX_RUNTIME_S, (field, edge, hint)
        if first > low:
            outside = {**defaults, field: (first - 1) * step}
            assert family.expected_runtime_s(outside) < MIN_RUNTIME_S, outside
            with pytest.raises(ParamsError, match="reference mid laptop"):
                family.parse_params(outside)
        if last < high:
            outside = {**defaults, field: (last + 1) * step}
            rules = family.combination_problems(family._model(outside))
            if rules:
                # G3 item 0.3: a combination rule (ml.cnn_train's float64
                # reference memory cap) can be the upper edge before the window
                with pytest.raises(ParamsError) as refused:
                    family.parse_params(outside)
                assert all(rule in str(refused.value) for rule in rules), outside
                assert not family.combination_problems(
                    family._model({**defaults, field: last * step})
                )
            else:
                assert family.expected_runtime_s(outside) > MAX_RUNTIME_S, outside
                with pytest.raises(ParamsError, match="reference mid laptop"):
                    family.parse_params(outside)


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_hint_is_monotonic_in_every_size_parameter_over_the_whole_range(
    family: Any,
) -> None:
    defaults = family.params_dict(family.smallest_params())
    for field in family.size_params:
        bounds = _int_bounds(family, field)
        if bounds is None:
            continue
        step = _step(family, field)
        low, high = bounds
        grid = sorted(
            {
                low,
                high,
                *(
                    (low + (high - low) * i // 40) // step * step or low
                    for i in range(41)
                ),
            }
        )
        hints = [family.expected_runtime_s({**defaults, field: v}) for v in grid]
        assert all(math.isfinite(h) and h > 0 for h in hints), field
        assert hints == sorted(hints), (family.name, field)
        assert hints[0] < hints[-1], f"{family.name}.{field} does not change the hint"


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_every_corner_of_the_parameter_space_is_safe(family: Any) -> None:
    """Every min/max corner x every literal choice: finite positive hint and
    manifest numbers, and parse_params either accepts or raises ParamsError."""
    int_fields = _int_fields(family)
    literals = _literal_fields(family)
    ranges = [(_int_bounds(family, f) or (0, 0)) for f in int_fields]
    lit_names = list(literals)
    for corner in itertools.product(*[(r[0], r[1]) for r in ranges]):
        for lits in itertools.product(*[literals[n] for n in lit_names]):
            raw = dict(zip(int_fields, corner, strict=True))
            raw.update(dict(zip(lit_names, lits, strict=True)))
            hint = family.expected_runtime_s(raw)
            assert math.isfinite(hint) and hint > 0, raw
            params = family._model(raw)
            ram = family.ram_gb(params)
            assert math.isfinite(ram) and ram > 0, raw
            vram = family.vram_gb(params)
            assert (vram is not None) == family.gpu, raw
            if vram is not None:
                assert math.isfinite(vram) and vram > 0
            try:
                family.parse_params(raw)
            except ParamsError:
                continue


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_defaults_are_the_smallest_run_inside_the_window(family: Any) -> None:
    hint = family.expected_runtime_s(family.smallest_params())
    assert MIN_RUNTIME_S <= hint <= 2.5 * MIN_RUNTIME_S
    # "smallest": shrinking any size parameter by one step falls below the window
    defaults = family.params_dict(family.smallest_params())
    shrinkable = 0
    for field in family.size_params:
        bounds = _int_bounds(family, field)
        if bounds and defaults[field] > bounds[0]:
            shrunk = {**defaults, field: max(bounds[0], int(defaults[field] * 0.5))}
            if family.expected_runtime_s(shrunk) < hint:
                shrinkable += 1
    assert shrinkable >= 1 or len(family.size_params) == 0


def test_hint_on_hardware_without_the_metric_is_a_typed_unavailable() -> None:
    nothing = HardwareHint()
    for family in FAMILIES:
        with pytest.raises(Unavailable):
            family.expected_runtime_s(family.smallest_params(), nothing)
    cpu_only = HardwareHint(
        cpu_fp32_gflops_1t=50,
        cpu_fp32_gflops_all=100,
        cpu_fp64_gflops_all=50,
        mem_triad_gb_s=10,
    )
    for family in FAMILIES:
        if family.gpu:
            with pytest.raises(Unavailable):
                family.expected_runtime_s(family.smallest_params(), cpu_only)
        else:
            assert family.expected_runtime_s(family.smallest_params(), cpu_only) > 0
    for bad in (0.0, -1.0):
        with pytest.raises(Unavailable):
            get_family("cpu.sort").expected_runtime_s(
                {}, HardwareHint(cpu_fp32_gflops_1t=bad)
            )


@pytest.mark.parametrize(("faster", "slower"), [(200.0, 100.0), (1000.0, 10.0)])
def test_a_faster_node_never_has_a_longer_hint(faster: float, slower: float) -> None:
    for family in FAMILIES:
        if family.gpu:
            continue
        fast = HardwareHint(
            cpu_fp32_gflops_1t=faster,
            cpu_fp32_gflops_all=faster,
            cpu_fp64_gflops_all=faster,
            mem_triad_gb_s=faster,
        )
        slow = HardwareHint(
            cpu_fp32_gflops_1t=slower,
            cpu_fp32_gflops_all=slower,
            cpu_fp64_gflops_all=slower,
            mem_triad_gb_s=slower,
        )
        assert family.expected_runtime_s({}, fast) < family.expected_runtime_s({}, slow)


# ----------------------------------------------------------------- seeds


@pytest.mark.parametrize("seed", [0, 1, 2**32 - 1])
def test_boundary_seeds_are_accepted(seed: int) -> None:
    assert "workload_params.json" in job_files("cpu.sort", {}, seed)


@pytest.mark.parametrize(
    "seed", [-1, 2**32, 2**64, True, False, 1.5, float("nan"), "1", None, [1], b"1"]
)
def test_bad_seeds_are_refused_in_job_files(seed: Any) -> None:
    with pytest.raises(ParamsError, match="seed"):
        job_files("cpu.sort", {}, seed)


@pytest.mark.parametrize(
    "seed",
    [
        -1,
        2**32,
        True,
        1.5,
        None,
        "7",
    ],
)
def test_run_refuses_bad_seeds_before_executing_anything(
    seed: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reached(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError(f"seed {seed!r} reached the kernel runner")

    monkeypatch.setattr(base_mod, "execute", reached)
    with pytest.raises(ParamsError):
        get_family("cpu.sort").run({}, seed)
