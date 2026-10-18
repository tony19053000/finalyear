"""V0.9 G2 tester round 2 (split from ``..._checks.py``, G3 item 0.2): PARAM_LIMITS
parity with the pydantic models, the seed rule everywhere, and ``runner.main``
on bad, corrupt and valid requests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

np = pytest.importorskip("numpy")


from meshforge.bench.workloads.kernels import runner  # noqa: E402
from meshforge.bench.workloads.kernels._support import (  # noqa: E402
    KernelParamsError,
)

pytestmark = pytest.mark.unit


# ------------------------------------------------------- PARAM_LIMITS (D6)


def test_param_limits_parity_with_pydantic_for_every_family() -> None:
    from meshforge.bench.workloads import get_family, list_families

    fams = list_families()
    assert (
        {f.name for f in fams} == set(runner.PARAM_LIMITS) == set(runner.KERNEL_MODULES)
    )
    for fam in fams:
        model = fam.params_model
        limits = runner.PARAM_LIMITS[fam.name]
        assert set(model.model_fields) == set(limits), fam.name
        for name, field in model.model_fields.items():
            lim = limits[name]
            if isinstance(lim, tuple):
                assert set(lim) == set(_literal_args(field.annotation)), (
                    fam.name,
                    name,
                )
            else:
                le = next((m.le for m in field.metadata if hasattr(m, "le")), None)
                assert le == lim, (fam.name, name, le, lim)
        # accepted at the max, refused one past (pydantic and runner agree)
        for name, lim in limits.items():
            if isinstance(lim, tuple):
                continue
            dflt = fam.smallest_params().model_dump()
            over = {**dflt, name: lim + 1}
            with pytest.raises(ValueError):  # pydantic ValidationError
                fam.params_model(**over)
            with pytest.raises(KernelParamsError):
                runner.check_params(fam.name, over)
    _ = get_family


def _literal_args(ann: Any) -> tuple[Any, ...]:
    import typing

    args = typing.get_args(ann)
    if args and typing.get_origin(ann) is typing.Literal:
        return args
    if args:  # Annotated / Optional
        for a in args:
            r = _literal_args(a)
            if r:
                return r
    # enum
    try:
        return tuple(m.value for m in ann)
    except TypeError:
        return ()


@pytest.mark.parametrize("bad", [True, False, 1.0, "4", None, [1], 0, -1, 10**30])
def test_check_params_rejects_type_confusion(bad: Any) -> None:
    with pytest.raises(KernelParamsError):
        runner.check_params("cpu.sort", {"n": bad, "repeats": 1})


@pytest.mark.parametrize(
    "seed", [True, False, 1.0, "1", None, -1, 2**32, 10**40, [], {}]
)
def test_check_seed_rejects(seed: Any) -> None:
    with pytest.raises(KernelParamsError):
        runner.check_seed(seed)


def test_seed_rule_identical_across_run_job_files_and_runner() -> None:
    from meshforge.bench.workloads import ParamsError, get_family
    from meshforge.bench.workloads.materialise import job_files

    fam = get_family("cpu.sort")
    for seed in (0, 2**32 - 1, 2**32, -1, True, 1.5, "1", None):
        results = []
        for fn in (
            fam.check_seed,
            lambda s: job_files(fam, fam.smallest_params(), s),
            runner.check_seed,
        ):
            try:
                fn(seed)
                results.append(True)
            except (ParamsError, KernelParamsError):
                results.append(False)
        assert len(set(results)) == 1, (seed, results)


def test_transformer_width_multiple_and_choices() -> None:
    with pytest.raises(KernelParamsError):
        runner.check_params("ml.transformer_infer", {"width": 65})
    with pytest.raises(KernelParamsError):
        runner.check_params("gpu.matmul", {"dtype": "bfloat16"})
    with pytest.raises(KernelParamsError):
        runner.check_params("cpu.zlib", {"level": 5})
    with pytest.raises(KernelParamsError):
        runner.check_params("cpu.sort", {"n": 5, "bogus": 1})


def test_main_bad_params_variants_exit2_and_write_result(tmp_path: Path) -> None:
    cases = [
        {"family": "cpu.sort", "params": {"n": 10**12, "repeats": 1}, "seed": 1},
        {"family": "cpu.sort", "params": {"n": 5, "repeats": 1, "x": 1}, "seed": 1},
        {"family": "cpu.sort", "params": {"n": 5, "repeats": 1}, "seed": -1},
        {"family": "cpu.sort", "params": {"n": 5, "repeats": 1}, "seed": True},
        {"family": "nope", "params": {}, "seed": 1},
        {"family": "cpu.sort", "params": {"n": 5.0, "repeats": 1}, "seed": 1},
        [1, 2],
    ]
    for i, doc in enumerate(cases):
        d = tmp_path / str(i)
        d.mkdir()
        (d / runner.PARAMS_FILE).write_text(json.dumps(doc))
        assert runner.main(d) == runner.EXIT_BAD_PARAMS, doc
        res = json.loads((d / runner.RESULT_FILE).read_text())
        assert res["status"] == "bad_params" and res["verified"] is False


def test_main_corrupt_json_and_missing_file(tmp_path: Path) -> None:
    (tmp_path / runner.PARAMS_FILE).write_text("{not json")
    assert runner.main(tmp_path) == runner.EXIT_BAD_PARAMS
    other = tmp_path / "o"
    other.mkdir()
    assert runner.main(other) == runner.EXIT_BAD_PARAMS


def test_main_valid_tiny_run_exit0(tmp_path: Path) -> None:
    (tmp_path / runner.PARAMS_FILE).write_text(
        json.dumps({"family": "cpu.sort", "params": {"n": 1, "repeats": 1}, "seed": 0})
    )
    assert runner.main(tmp_path) == 0
