"""Unit tests for ``spikes._s6.report`` (0.2.10): the markdown block is
table-safe (``cell()``), complete for empty input, and shows TBD laptops.
"""

from __future__ import annotations

from typing import Any

from spikes._harness.report import TEMPLATE_PATH, render_report
from spikes._harness.reps import CaseResult
from spikes._s6 import report as rep
from spikes._s6 import verdict as v


def _case(case_id: str, metric: str, values: list[float]) -> dict[str, Any]:
    result = CaseResult(case_id=case_id, label=case_id, metrics={metric: "ms"})
    result.samples = [{metric: x} for x in values]
    return result.to_json()


def _doc(
    host: str, cases: dict[str, Any], *, docker: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "spike": "S6",
        "topic": "S6-environments",
        "host": {"hostname": host, "label": host, "os": "Linux", "os_release": "6.8"},
        "timestamp_utc": "2026-09-20T10:00:00Z",
        "link": None,
        "params": {},
        "cases": cases,
        "errors": [],
        "extra": {
            "docker": docker
            or {
                "ok": True,
                "client_version": "29.7.2",
                "server_version": "29.7.2",
                "operating_system": "Docker Desktop",
                "docker_desktop": True,
                "wsl2_backend": True,
                "kernel_version": "6.6-microsoft-standard-WSL2",
                "runtimes": ["runc"],
                "nvidia_runtime": False,
                "cgroup_version": "2",
                "driver": "overlayfs",
            },
            "facts": {
                "system": "Windows",
                "release": "11",
                "is_wsl": False,
                "gpu": {"present": True, "devices": ["RTX | 4060"]},
            },
        },
    }


def test_render_empty_is_all_tbd() -> None:
    text = rep.render_environments([], {"h1": "L1"})
    assert "No S6 result files found" in text
    assert "| L1 | ? | TBD — no result file |" in text
    assert "| container start to exit, sandboxed (ms) | TBD |" in text
    assert "warm env job start overhead on L1: TBD (no result file)" in text
    assert "### Docker facts" in text and "### Diagnoses and fixes" in text
    assert "- L1: docker: TBD — no result file" in text


def test_render_escapes_pipes_and_newlines() -> None:
    cases = {
        "docker-start": _case("docker-start", "start_to_exit_ms", [200.0] * 5),
        "docker-gpu-smi": {
            "samples": [],
            "errors": [
                {
                    "phase": "setup",
                    "failure_type": "no_nvidia_runtime",
                    "message": "a | b\nline2",
                }
            ],
        },
    }
    text = rep.render_environments([_doc("h1", cases)], {"h1": "L1"})
    assert "a \\| b line2" in text
    assert "RTX \\| 4060" in text
    assert "Docker Desktop (WSL2 backend)" in text
    assert "runc (no nvidia runtime key)" in text
    assert (
        "| L1 | Windows 11 | usable | no — no_nvidia_runtime: a \\| b line2 |" in text
    )
    assert "venv-fallback" in text
    assert "- L1: gpu-in-container: no — no_nvidia_runtime" in text
    for line in text.splitlines():
        if line.startswith("|"):
            assert line.endswith("|"), line


def test_render_quick_run_is_marked() -> None:
    doc = _doc(
        "h1", {"docker-start": _case("docker-start", "start_to_exit_ms", [1.0, 2.0])}
    )
    doc["extra"]["quick"] = True
    text = rep.render_environments([doc])
    assert "| h1 (quick run) |" in text
    assert "INVALID (n<5)" in text
    assert "**INVALID (n=2)** 1.50 ± 0.71 (n=2)" in text


def test_render_fits_the_harness_template() -> None:
    doc = _doc(
        "h1", {"docker-start": _case("docker-start", "start_to_exit_ms", [200.0] * 5)}
    )
    extra = rep.render_environments([doc], {"h1": "L1", "h2": "L2"})
    text = render_report(
        TEMPLATE_PATH.read_text(encoding="utf-8"),
        [doc],
        topic="S6-environments",
        title="S6 execution environments",
        day="2026-09-20",
        task="0.2.10",
        spike="S6",
        extra_results=extra,
    )
    assert "# S6 execution environments — 2026-09-20" in text
    assert "### Environment verdicts per laptop" in text
    assert text.index("| Case / pair / payload |") < text.index(
        "### Environment verdicts per laptop"
    )
    assert "| L2 | ? | TBD — no result file |" in text


def test_cost_rows_cover_every_cost_case() -> None:
    ids = {case_id for case_id, _m, _l in v.COST_ROWS}
    assert ids == {
        "docker-start",
        "venv-start",
        "docker-pull-cold",
        "venv-cold",
        "venv-warm",
        "docker-gpu-torch",
    }
    text = rep.render_environments([_doc("h1", {})])
    for _c, _m, label in v.COST_ROWS:
        assert f"| {label} |" in text
