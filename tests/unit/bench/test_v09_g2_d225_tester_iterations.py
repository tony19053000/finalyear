"""Tester attacks on D225 item 3 (compute_iterations) and the determinism /
pin claims."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from meshforge.bench.workloads import build_job, get_family, list_families
from meshforge.bench.workloads.base import WorkloadResult
from meshforge.bench.workloads.kernels import runner

pytestmark = pytest.mark.unit


def _doc(family: str = "cpu.zlib", **overrides: Any) -> dict[str, Any]:
    fam = get_family(family)
    params = fam.params_dict({"size_mb": 8, "level": 1, "repeats": 3})
    document = runner.execute(family, params, 1)
    document.update(overrides)
    return document


def _verdict(document: dict[str, Any], family: str = "cpu.zlib") -> Any:
    return get_family(family).verify(WorkloadResult.model_validate(document))


def test_honest_document_verifies() -> None:
    assert _verdict(_doc()).ok


def test_old_workload_json_without_the_field_fails_loudly() -> None:
    document = _doc()
    del document["compute_iterations"]
    verdict = _verdict(document)
    assert not verdict.ok
    assert any("compute_iterations" in p for p in verdict.problems)


def test_null_field_fails() -> None:
    verdict = _verdict(_doc(compute_iterations=None))
    assert not verdict.ok


@pytest.mark.parametrize("wrong", [1, 2, 4, 1000])
def test_mismatching_count_fails(wrong: int) -> None:
    verdict = _verdict(_doc(compute_iterations=wrong))
    assert not verdict.ok
    assert any("expected 3" in p for p in verdict.problems)


@pytest.mark.parametrize("bad", [0, -1])
def test_non_positive_count_is_refused_by_the_model(bad: int) -> None:
    with pytest.raises(Exception, match="compute_iterations"):
        WorkloadResult.model_validate(_doc(compute_iterations=bad))


@pytest.mark.parametrize("bad", ["three", [3], {"n": 3}, 3.5])
def test_wrong_types_are_refused_by_the_model(bad: Any) -> None:
    with pytest.raises(Exception, match="compute_iterations"):
        WorkloadResult.model_validate(_doc(compute_iterations=bad))


@pytest.mark.parametrize("coerced", [3.0, "3", True])
def test_lax_coercions_are_refused_by_the_model(coerced: Any) -> None:
    """T2 fixed: the field is strict - 3.0 / "3" / true are not integers."""
    with pytest.raises(Exception, match="compute_iterations"):
        WorkloadResult.model_validate(_doc(compute_iterations=coerced))


def test_true_is_not_silently_accepted_as_one_iteration() -> None:
    """compute_iterations: true must not pass as 1 for a run with repeats=1."""
    fam = get_family("cpu.zlib")
    params = fam.params_dict({"size_mb": 8, "level": 1, "repeats": 1})
    document = runner.execute("cpu.zlib", params, 1)
    document["compute_iterations"] = True
    try:
        verdict = fam.verify(WorkloadResult.model_validate(document))
    except Exception:  # noqa: BLE001 - refusing at validation is also loud
        return
    assert not verdict.ok, "bool true accepted as the integer 1"


def test_params_edited_after_the_run_are_caught_by_the_count() -> None:
    document = _doc()
    document["params"] = {**document["params"], "repeats": 9}
    verdict = _verdict(document)
    assert not verdict.ok


def test_sha256_is_one_iteration_and_other_counts_fail() -> None:
    fam = get_family("cpu.sha256")
    document = runner.execute("cpu.sha256", fam.params_dict({}), 1)
    assert document["compute_iterations"] == 1
    assert fam.verify(WorkloadResult.model_validate(document)).ok
    document["compute_iterations"] = 2
    assert not fam.verify(WorkloadResult.model_validate(document)).ok


def test_every_family_has_a_loop_param_with_a_valid_default() -> None:
    for family in list_families():
        params = family.params_dict({})
        count = runner.compute_iterations(family.name, params)
        assert isinstance(count, int) and count >= 1, family.name
        name = runner.ITERATION_PARAM[family.name]
        if name is not None:
            assert count == params[name]


@pytest.mark.parametrize("value", [None, "3", 3.0, True, False, 0, -2, [], {}])
def test_compute_iterations_helper_rejects_non_positive_ints(value: Any) -> None:
    assert runner.compute_iterations("cpu.zlib", {"repeats": value}) is None


def test_compute_iterations_is_a_number_of_loops_the_kernel_really_runs() -> None:
    """compute_ns covers the whole loop: time must scale with the loop count."""
    fam = get_family("cpu.zlib")
    one = runner.execute(
        "cpu.zlib", fam.params_dict({"size_mb": 8, "level": 1, "repeats": 1}), 1
    )
    many = runner.execute(
        "cpu.zlib", fam.params_dict({"size_mb": 8, "level": 1, "repeats": 8}), 1
    )
    assert many["compute_iterations"] == 8 and one["compute_iterations"] == 1
    assert many["compute_ns"] > 3 * one["compute_ns"]


def test_failed_and_bad_param_documents_carry_no_count(tmp_path: Path) -> None:
    (tmp_path / runner.PARAMS_FILE).write_text(
        json.dumps({"family": "cpu.zlib", "params": {"repeats": 0}, "seed": 1}),
        encoding="utf-8",
    )
    runner.main(tmp_path)
    doc = json.loads((tmp_path / runner.RESULT_FILE).read_text(encoding="utf-8"))
    assert doc["compute_iterations"] is None
    assert doc["schema_version"] == 1 == runner.RESULT_SCHEMA_VERSION


def test_check_failed_document_has_count_but_is_not_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meshforge.bench.workloads.kernels import cpu

    kernel = cpu.KERNELS["cpu.zlib"]
    bad = kernel.__class__(
        kernel.prepare,
        kernel.compute,
        lambda s, o: type(kernel.check(s, o))(False, "x", False, "forced"),
        kernel.warmup,
    )
    monkeypatch.setitem(cpu.KERNELS, "cpu.zlib", bad)
    document = _doc()
    assert document["status"] == "check_failed"
    assert not _verdict(document).ok


def test_build_job_is_still_byte_identical_and_seed_sensitive(tmp_path: Path) -> None:
    for name in ("cpu.zlib", "ml.cnn_infer"):
        fam = get_family(name)
        a = build_job(fam, fam.params_dict({}), 3, tmp_path / f"a-{name}")
        b = build_job(fam, fam.params_dict({}), 3, tmp_path / f"b-{name}")
        files_a = {
            p.relative_to(a.path): p.read_bytes()
            for p in a.path.rglob("*")
            if p.is_file()
        }
        files_b = {
            p.relative_to(b.path): p.read_bytes()
            for p in b.path.rglob("*")
            if p.is_file()
        }
        assert files_a == files_b and files_a
        assert a.content_digest == b.content_digest
