"""Unit tests for ``spikes._s6.verdict`` (0.2.10): verdicts are judged only
from the JSON, INVALID/TBD/skipped/n-a rules, the ADR-0004 recommendation
matrix, the cost cells, the calibration line, and corrupt result-file shapes
(never a crash, never a ``yes``/``docker`` from bad data).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from spikes._harness.reps import CaseResult
from spikes._s6 import verdict as v


def _case(
    case_id: str,
    metric: str,
    values: list[float],
    *,
    errors: list[dict[str, Any]] | None = None,
    warmup_errors: int = 0,
) -> dict[str, Any]:
    result = CaseResult(case_id=case_id, label=case_id, metrics={metric: "ms"})
    result.samples = [{metric: x} for x in values]
    for err in errors or []:
        result.errors.append({"rep": 0, "phase": "measure", "fatal": False, **err})
    for i in range(warmup_errors):
        result.errors.append(
            {
                "rep": i,
                "phase": "warmup",
                "failure_type": "timeout",
                "message": "w",
                "fatal": False,
            }
        )
    return result.to_json()


def _skipped(case_id: str, reason: str = "--skip-gpu") -> dict[str, Any]:
    result = CaseResult(
        case_id=case_id, label=case_id, metrics={"x": "ms"}, aborted=True
    )
    result.errors.append(
        {
            "rep": 0,
            "phase": "setup",
            "failure_type": "skipped_by_flag",
            "message": reason,
            "fatal": True,
        }
    )
    return result.to_json()


def _failed(case_id: str, failure_type: str, message: str = "boom") -> dict[str, Any]:
    result = CaseResult(
        case_id=case_id, label=case_id, metrics={"x": "ms"}, aborted=True
    )
    result.errors.append(
        {
            "rep": 0,
            "phase": "setup",
            "failure_type": failure_type,
            "message": message,
            "fatal": True,
        }
    )
    return result.to_json()


def _doc(
    cases: dict[str, Any] | None = None,
    *,
    docker_ok: bool = True,
    gpu_present: bool | None = True,
    host: str = "lab1",
    timestamp: str = "2026-09-20T10:00:00Z",
    extra_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    docker: dict[str, Any] = {
        "ok": True,
        "client_version": "29.7.2",
        "server_version": "29.7.2",
        "operating_system": "Ubuntu 24.04",
        "kernel_version": "6.8",
        "runtimes": ["nvidia", "runc"],
        "nvidia_runtime": True,
        "cgroup_version": "2",
        "driver": "overlayfs",
    }
    if not docker_ok:
        docker = {
            "ok": False,
            "failure_type": "docker_daemon_unreachable",
            "message": "Cannot connect",
            "fix": "start Docker Desktop",
        }
    gpu: dict[str, Any] = {"present": True, "devices": ["RTX 3050"]}
    if gpu_present is False:
        gpu = {
            "present": False,
            "devices": [],
            "failure_type": "no_gpu_on_host",
            "message": "nvidia-smi not found",
            "fix": "install driver",
        }
    elif gpu_present is None:
        gpu = {}
    extra: dict[str, Any] = {
        "docker": docker,
        "facts": {"system": "Linux", "release": "6.8", "is_wsl": False, "gpu": gpu},
        "quick": False,
    }
    if extra_override is not None:
        extra = extra_override
    return {
        "schema_version": 1,
        "spike": "S6",
        "topic": "S6-environments",
        "host": {"hostname": host, "label": host, "os": "Linux", "os_release": "6.8"},
        "timestamp_utc": timestamp,
        "link": None,
        "params": {},
        "cases": cases if cases is not None else {},
        "errors": [],
        "extra": extra,
        "_source": f"{host}.json",
    }


def _good_cases(n: int = 5) -> dict[str, Any]:
    return {
        "docker-start": _case(
            "docker-start", "start_to_exit_ms", [200.0 + i for i in range(n)]
        ),
        "docker-gpu-smi": _case("docker-gpu-smi", "start_to_exit_ms", [350.0] * n),
        "docker-gpu-torch": _case(
            "docker-gpu-torch", "start_to_ready_ms", [4000.0] * n
        ),
        "venv-start": _case("venv-start", "start_to_exit_ms", [100.0] * n),
        "docker-pull-cold": _case("docker-pull-cold", "pull_s", [9.5] * n),
        "venv-cold": _case("venv-cold", "total_s", [3.5] * n),
        "venv-warm": _case("venv-warm", "total_s", [0.1] * n),
    }


# ------------------------------------------------------------ case_verdict
def test_case_verdict_yes_needs_five_clean_measured_reps() -> None:
    assert v.case_verdict(_case("c", "m", [1, 2, 3, 4, 5])).value == v.YES
    assert v.case_verdict(_case("c", "m", [1, 2, 3, 4])).value == v.INVALID
    assert v.case_verdict(None).value == v.TBD


def test_case_verdict_warmup_failures_do_not_count() -> None:
    assert v.case_verdict(_case("c", "m", [1] * 5, warmup_errors=2)).value == v.YES


def test_case_verdict_measured_failure_is_flaky_with_both_numbers() -> None:
    case = _case(
        "c", "m", [1] * 4, errors=[{"failure_type": "timeout", "message": "t"}]
    )
    verdict = v.case_verdict(case)
    assert verdict.value == "flaky 4/5"
    assert verdict.detail == "timeout: t"


def test_case_verdict_no_samples_is_no_with_the_first_failure() -> None:
    verdict = v.case_verdict(
        _failed("c", "no_nvidia_runtime", "could not select [fix: toolkit]")
    )
    assert verdict.value == v.NO
    assert verdict.detail.startswith("no_nvidia_runtime: could not select")


def test_case_verdict_skipped_and_not_measured() -> None:
    assert v.case_verdict(_skipped("c", "--quick")) == v.Verdict(v.SKIPPED, "--quick")
    for failure in sorted(v.NOT_MEASURED_TYPES):
        verdict = v.case_verdict(_failed("c", failure))
        assert verdict.value == v.TBD, failure
        assert verdict.detail.startswith("not measured: " + failure)


def test_case_verdict_error_without_phase_counts_as_measured() -> None:
    case = _case("c", "m", [1] * 5)
    case["errors"] = [{"failure_type": "x", "message": "no phase"}]
    assert v.case_verdict(case).value == "flaky 5/6"


@pytest.mark.parametrize("bad", [42, "text", [1, 2], None])
def test_case_verdict_malformed_case_entry(bad: object) -> None:
    verdict = v.case_verdict(bad)
    assert verdict.value in (v.MALFORMED, v.TBD)
    assert verdict.value != v.YES


@pytest.mark.parametrize("bad", [{"a": 1}, "abc", 7, True])
def test_case_verdict_malformed_samples(bad: object) -> None:
    case = _case("c", "m", [1] * 5)
    case["samples"] = bad
    verdict = v.case_verdict(case)
    assert verdict.value == v.MALFORMED
    assert "samples is" in verdict.detail


def test_case_verdict_malformed_errors_field_never_yes() -> None:
    case = _case("c", "m", [1] * 5)
    case["errors"] = "broken"
    verdict = v.case_verdict(case)
    assert verdict.value.startswith("flaky")
    assert "malformed error record" in verdict.detail
    case["errors"] = [None]
    assert v.case_verdict(case).value.startswith("flaky")


# ---------------------------------------------------------- cost / summary
def test_cost_cell_formats() -> None:
    assert (
        v.cost_cell(_case("c", "m", [1.0, 2.0, 3.0, 4.0, 5.0]), "m")
        == "3.00 ± 1.58 (n=5)"
    )
    assert (
        v.cost_cell(_case("c", "m", [1.0, 2.0]), "m")
        == "**INVALID (n=2)** 1.50 ± 0.71 (n=2)"
    )
    assert v.cost_cell(None, "m") == v.TBD
    assert v.cost_cell(_skipped("c"), "m") == v.SKIPPED
    assert (
        v.cost_cell(_failed("c", "no_nvidia_runtime"), "m") == "err: no_nvidia_runtime"
    )
    assert (
        v.cost_cell(_failed("c", "insufficient_disk"), "m") == "err: insufficient_disk"
    )
    assert v.cost_cell("garbage", "m") == v.MALFORMED
    assert v.cost_cell(_case("c", "m", [1.0] * 5), "other_metric") == v.TBD


def test_cost_cell_malformed_summary_is_tbd_not_crash() -> None:
    case = _case("c", "m", [1.0] * 5)
    case["summary"] = "broken"
    assert v.cost_cell(case, "m") == v.TBD
    case["summary"] = {"m": {"n": "five", "mean": 1}}
    assert v.cost_cell(case, "m") == v.TBD
    case["summary"] = {"m": {"n": 5, "mean": "x"}}
    assert v.cost_cell(case, "m") == v.TBD
    case["summary"] = {"m": {"n": -1}}
    assert v.cost_cell(case, "m") == v.TBD
    case["summary"] = {"m": {"n": True}}
    assert v.cost_cell(case, "m") == v.TBD


def test_valid_mean_only_from_valid_clean_rows() -> None:
    assert v.valid_mean(_case("c", "m", [2.0] * 5), "m") == 2.0
    assert v.valid_mean(_case("c", "m", [2.0] * 4), "m") is None
    flaky = _case("c", "m", [2.0] * 5, errors=[{"failure_type": "x", "message": "m"}])
    assert v.valid_mean(flaky, "m") is None
    # samples say 5 but the stored summary claims fewer: the summary decides
    case = _case("c", "m", [2.0] * 5)
    case["summary"]["m"]["n"] = 3
    assert v.valid_mean(case, "m") is None


# --------------------------------------------------------- docker verdict
def test_docker_usable_from_clean_start() -> None:
    assert v.docker_verdict(_doc(_good_cases())) == v.Verdict(v.USABLE)


def test_docker_unusable_from_facts_failure_with_fix() -> None:
    doc = _doc(
        {"docker-start": _failed("docker-start", "docker_daemon_unreachable", "x")},
        docker_ok=False,
    )
    verdict = v.docker_verdict(doc)
    assert verdict.value == v.UNUSABLE
    assert "docker_daemon_unreachable" in verdict.detail
    assert "[fix: start Docker Desktop]" in verdict.detail


def test_docker_unusable_from_failed_start_with_facts_ok() -> None:
    doc = _doc(
        {
            "docker-start": _failed(
                "docker-start", "sandbox_flag_refused", "--pids-limit: unknown flag"
            )
        }
    )
    verdict = v.docker_verdict(doc)
    assert verdict.value == v.UNUSABLE
    assert verdict.detail.startswith("sandbox_flag_refused: --pids-limit")


def test_docker_invalid_skipped_flaky_tbd_pass_through() -> None:
    assert (
        v.docker_verdict(
            _doc({"docker-start": _case("docker-start", "m", [1] * 2)})
        ).value
        == v.INVALID
    )
    assert (
        v.docker_verdict(
            _doc({"docker-start": _skipped("docker-start", "--skip-docker")})
        ).value
        == v.SKIPPED
    )
    flaky = _case(
        "docker-start",
        "m",
        [1] * 5,
        errors=[{"failure_type": "timeout", "message": "t"}],
    )
    assert v.docker_verdict(_doc({"docker-start": flaky})).value == "flaky 5/6"
    assert v.docker_verdict(_doc({})).value == v.TBD
    assert (
        v.docker_verdict(
            _doc({"docker-start": _failed("docker-start", "image_pull_failed")})
        ).value
        == v.TBD
    )


def test_docker_tbd_case_but_facts_failed_is_unusable() -> None:
    assert v.docker_verdict(_doc({}, docker_ok=False)).value == v.UNUSABLE


def test_docker_usable_flags_inconsistent_facts() -> None:
    doc = _doc(_good_cases(), docker_ok=False)
    verdict = v.docker_verdict(doc)
    assert verdict.value == v.USABLE
    assert "?" in verdict.detail


# ------------------------------------------------------------ gpu verdicts
def test_gpu_verdicts_yes_no_na_skipped() -> None:
    assert v.gpu_verdict(_doc(_good_cases()), "docker-gpu-smi").value == v.YES
    doc = _doc(
        {
            "docker-gpu-smi": _failed(
                "docker-gpu-smi", "no_nvidia_runtime", "could not select"
            )
        }
    )
    verdict = v.gpu_verdict(doc, "docker-gpu-smi")
    assert verdict.value == v.NO and "no_nvidia_runtime" in verdict.detail
    assert (
        v.gpu_verdict(_doc(_good_cases(), gpu_present=False), "docker-gpu-smi").value
        == v.NOT_APPLICABLE
    )
    no_gpu = _doc(
        {"docker-gpu-smi": _failed("docker-gpu-smi", "no_gpu_on_host", "x")},
        gpu_present=None,
    )
    assert v.gpu_verdict(no_gpu, "docker-gpu-smi").value == v.NOT_APPLICABLE
    assert (
        v.gpu_verdict(
            _doc({"docker-gpu-torch": _skipped("docker-gpu-torch")}), "docker-gpu-torch"
        ).value
        == v.SKIPPED
    )
    assert v.gpu_verdict(_doc({}), "docker-gpu-torch").value == v.TBD


def test_gpu_on_host_reads_only_bools() -> None:
    assert v.gpu_on_host(_doc(gpu_present=True)) is True
    assert v.gpu_on_host(_doc(gpu_present=False)) is False
    assert v.gpu_on_host(_doc(gpu_present=None)) is None
    assert (
        v.gpu_on_host(_doc(extra_override={"facts": {"gpu": {"present": "yes"}}}))
        is None
    )
    assert v.gpu_on_host(_doc(extra_override={"facts": "broken"})) is None
    assert v.gpu_on_host({"extra": []}) is None


# ---------------------------------------------------------- recommendation
@pytest.mark.parametrize(
    ("docker", "gpu", "present", "expected"),
    [
        (v.Verdict(v.USABLE), v.Verdict(v.YES), True, v.RECOMMEND_DOCKER),
        (v.Verdict(v.USABLE), v.Verdict(v.NOT_APPLICABLE), False, v.RECOMMEND_DOCKER),
        (v.Verdict(v.USABLE), v.Verdict(v.TBD), False, v.RECOMMEND_DOCKER),
        (
            v.Verdict(v.USABLE),
            v.Verdict(v.NO, "no_nvidia_runtime"),
            True,
            v.RECOMMEND_VENV,
        ),
        (v.Verdict(v.USABLE), v.Verdict(v.SKIPPED, "--skip-gpu"), True, v.TBD),
        (v.Verdict(v.USABLE), v.Verdict(v.INVALID), True, v.TBD),
        (v.Verdict(v.USABLE), v.Verdict("flaky 3/5"), True, v.TBD),
        (v.Verdict(v.USABLE), v.Verdict(v.TBD), True, v.TBD),
        (v.Verdict(v.USABLE), v.Verdict(v.TBD), None, v.TBD),
        (v.Verdict(v.UNUSABLE, "daemon"), v.Verdict(v.YES), True, v.RECOMMEND_VENV),
        (v.Verdict(v.UNUSABLE, "daemon"), v.Verdict(v.TBD), False, v.RECOMMEND_VENV),
        (v.Verdict("flaky 4/5", "t"), v.Verdict(v.YES), True, v.RECOMMEND_VENV),
        (v.Verdict(v.INVALID), v.Verdict(v.YES), True, v.TBD),
        (v.Verdict(v.INVALID), v.Verdict(v.INVALID), False, v.TBD),
        (v.Verdict(v.SKIPPED), v.Verdict(v.SKIPPED), True, v.TBD),
        (v.Verdict(v.MALFORMED), v.Verdict(v.YES), True, v.TBD),
        (v.Verdict(v.TBD), v.Verdict(v.TBD), None, v.TBD),
    ],
)
def test_recommendation_matrix(
    docker: v.Verdict, gpu: v.Verdict, present: bool | None, expected: str
) -> None:
    result = v.recommendation(docker, gpu, gpu_present=present)
    assert result.value == expected
    if expected != v.RECOMMEND_DOCKER:
        assert result.detail


def test_recommendation_never_docker_from_invalid_rows() -> None:
    doc = _doc(_good_cases(n=4))
    hv = v.host_verdict(doc, "L1")
    assert hv.docker.value == v.INVALID
    assert hv.recommendation.value == v.TBD


# ------------------------------------------------------------- calibration
def test_calibration_line_computed_from_valid_rows_only() -> None:
    line = v.calibration_line(_doc(_good_cases()), "L1")
    assert line.startswith(
        "warm env job start overhead on L1: venv 0.10 s (within), container 0.20 s (within)"
    )
    assert "≤ 3 s target" in line
    assert "PyTorch-CUDA container start to CUDA ready 4.00 s" in line


def test_calibration_line_exceeds_and_tbd() -> None:
    cases = _good_cases()
    cases["docker-start"] = _case("docker-start", "start_to_exit_ms", [5000.0] * 5)
    cases["venv-start"] = _case("venv-start", "start_to_exit_ms", [100.0] * 3)
    del cases["docker-gpu-torch"]
    line = v.calibration_line(_doc(cases), "L1")
    assert "container 5.00 s (EXCEEDS)" in line
    assert "venv TBD (venv-start: **INVALID (n=3)**" in line
    assert "PyTorch" not in line
    assert v.TARGET_START_OVERHEAD_S == 3.0


def test_calibration_line_no_cases() -> None:
    line = v.calibration_line(_doc({}), "L1")
    assert "venv TBD (venv-start: TBD)" in line
    assert "container TBD (docker-start: TBD)" in line


# ------------------------------------------------------------ host verdicts
def test_host_verdict_full_good_run() -> None:
    hv = v.host_verdict(_doc(_good_cases()), "L1")
    assert hv.docker.value == v.USABLE
    assert hv.gpu_in_container.value == v.YES
    assert hv.torch_cuda.value == v.YES
    assert hv.recommendation.value == v.RECOMMEND_DOCKER
    assert hv.costs["container start to exit, sandboxed (ms)"] == "202.00 ± 1.58 (n=5)"
    assert (
        hv.costs["PyTorch-CUDA container start to CUDA ready (ms)"]
        == "4000.00 ± 0.00 (n=5)"
    )
    assert hv.fixes() == []
    assert hv.docker_facts["runtimes"] == "nvidia, runc (nvidia present)"
    assert hv.docker_facts["host_gpu"] == "RTX 3050"
    assert hv.to_json()["recommendation"]["value"] == "docker"


def test_host_verdict_windows_daemon_down() -> None:
    cases = {
        "docker-start": _failed(
            "docker-start",
            "docker_daemon_unreachable",
            "Cannot connect [fix: start Docker Desktop]",
        ),
        "docker-gpu-smi": _failed(
            "docker-gpu-smi", "docker_daemon_unreachable", "Cannot connect"
        ),
        "docker-gpu-torch": _failed(
            "docker-gpu-torch", "docker_daemon_unreachable", "Cannot connect"
        ),
        "venv-start": _case("venv-start", "start_to_exit_ms", [100.0] * 5),
    }
    hv = v.host_verdict(_doc(cases, docker_ok=False), "L2")
    assert hv.docker.value == v.UNUSABLE
    assert hv.gpu_in_container.value == v.NO
    assert hv.recommendation.value == v.RECOMMEND_VENV
    fixes = hv.fixes()
    assert any(
        "docker: unusable" in f and "[fix: start Docker Desktop]" in f for f in fixes
    )
    assert any(f.startswith("runtime recommendation: venv-fallback") for f in fixes)
    assert (
        "container TBD (docker-start: err: docker_daemon_unreachable)" in hv.calibration
    )


def test_host_verdict_no_gpu_laptop_recommends_docker() -> None:
    cases = _good_cases()
    cases["docker-gpu-smi"] = _failed(
        "docker-gpu-smi", "no_gpu_on_host", "nvidia-smi not found"
    )
    cases["docker-gpu-torch"] = _failed(
        "docker-gpu-torch", "no_gpu_on_host", "nvidia-smi not found"
    )
    hv = v.host_verdict(_doc(cases, gpu_present=False), "L3")
    assert hv.gpu_in_container.value == v.NOT_APPLICABLE
    assert hv.torch_cuda.value == v.NOT_APPLICABLE
    assert hv.recommendation.value == v.RECOMMEND_DOCKER
    assert hv.docker_facts["host_gpu"] == "none"


def test_host_verdicts_labels_newest_wins_and_tbd_rows() -> None:
    old = _doc(_good_cases(n=3), host="LAB-1", timestamp="2026-09-19T10:00:00Z")
    new = _doc(_good_cases(), host="lab-1", timestamp="2026-09-20T10:00:00Z")
    new["_source"] = "lab-1__2.json"
    rows = v.host_verdicts([old, new], {"lab-1": "L1", "lab-2": "L2"})
    assert [r.host for r in rows] == ["L1", "L2"]
    assert rows[0].docker.value == v.USABLE  # newest run, not the file order
    assert rows[0].sources == ["LAB-1.json", "lab-1__2.json"]
    assert rows[1].docker.value == v.TBD
    assert rows[1].recommendation.value == v.TBD
    assert rows[1].costs[v.COST_ROWS[0][2]] == v.TBD
    assert rows[1].docker_facts == {}


def test_host_verdicts_older_file_listed_later_does_not_win() -> None:
    new = _doc(_good_cases(), host="h", timestamp="2026-09-20T10:00:00Z")
    old = _doc(_good_cases(n=2), host="h", timestamp="2026-09-19T10:00:00Z")
    old["_source"] = "h__old.json"
    rows = v.host_verdicts([new, old])
    assert rows[0].docker.value == v.USABLE


# ------------------------------------------------------- corrupt shapes
def _corruptions() -> list[tuple[str, Any, bool]]:
    """(key, bad value, must-not-be-usable). The last flag is False for the
    corruptions that leave five clean samples in place (only the summary is
    broken): the verdict is then still about the repetitions, but no cost
    number may be invented."""
    return [
        ("extra", "text", False),
        ("extra", [], False),
        ("extra", None, False),
        ("extra", {"docker": "x", "facts": 5}, False),
        ("extra", {"docker": {"ok": "yes"}, "facts": {"gpu": "none"}}, False),
        ("extra", {"docker": {"ok": False, "failure_type": 7, "fix": None}}, False),
        ("extra", {"facts": {"gpu": {"present": True, "devices": "RTX"}}}, False),
        ("cases", "text", True),
        ("cases", [], True),
        ("cases", {"docker-start": 3, "docker-gpu-smi": None, "venv-start": "x"}, True),
        ("cases", {"docker-start": {"samples": {"a": 1}}}, True),
        ("cases", {"docker-start": {"samples": [1, 2, 3, 4, 5], "errors": {}}}, True),
        ("cases", {"docker-start": {"samples": [{}] * 5, "summary": []}}, False),
        (
            "cases",
            {"docker-start": {"samples": [{}] * 5, "summary": {"start_to_exit_ms": 7}}},
            False,
        ),
        (
            "cases",
            {
                "docker-start": {
                    "samples": [{}] * 5,
                    "summary": {"start_to_exit_ms": {"n": 5}},
                }
            },
            False,
        ),
        (
            "cases",
            {"docker-start": {"samples": [{}] * 5, "errors": [{"phase": 3}]}},
            True,
        ),
        ("cases", {"docker-start": {"samples": [{}] * 5, "errors": ["bad"]}}, True),
        ("host", "text", False),
        ("host", {}, False),
        ("timestamp_utc", None, False),
        ("timestamp_utc", 12, False),
        ("_source", None, False),
    ]


@pytest.mark.parametrize(("key", "bad", "must_not_be_usable"), _corruptions())
def test_corrupt_result_shapes_never_crash_and_never_approve(
    key: str, bad: object, must_not_be_usable: bool
) -> None:
    doc = copy.deepcopy(_doc(_good_cases()))
    doc[key] = bad
    hv = v.host_verdict(doc, "L1")
    rows = v.host_verdicts([doc], {"lab1": "L1"})
    assert rows[0].host == "L1"
    if must_not_be_usable:
        assert hv.docker.value != v.USABLE
        assert hv.recommendation.value != v.RECOMMEND_DOCKER
    if key == "cases":
        assert (
            "container TBD" in hv.calibration
            or "container 0.20 s" not in hv.calibration
        )
    for value in hv.docker_facts.values():
        assert isinstance(value, str)
    assert isinstance(hv.calibration, str)
    hv.to_json()


def test_summary_with_five_samples_but_no_valid_summary_is_not_usable() -> None:
    doc = _doc(_good_cases())
    doc["cases"]["docker-start"]["summary"] = {
        "start_to_exit_ms": {"n": 5, "mean": None}
    }
    hv = v.host_verdict(doc, "L1")
    assert hv.docker.value == v.USABLE  # the verdict is about clean repetitions
    assert hv.costs["container start to exit, sandboxed (ms)"] == "— ± — (n=5)"
    assert "container TBD" in hv.calibration  # but no number is invented
