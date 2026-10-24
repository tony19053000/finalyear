"""G7-A (0.3.4, item 6): ``meshforge-agent hardware score|show`` with a fake runner.

Nothing is measured or probed: discovery returns fabricated facts and
``service.ScoreRunner`` is replaced by a fake, so these run in milliseconds on any
machine. Exit codes come from ``common/exit_codes``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from mf_bench_fakes import make_facts, make_gpu, make_scoreset
from typer.testing import CliRunner

from meshforge.agent.benchmarks import service
from meshforge.agent.benchmarks.cache import scorer_lock
from meshforge.agent.benchmarks.model import (
    HOST_METRICS,
    Metric,
    MetricReason,
    MetricStatus,
    ScoreSet,
)
from meshforge.agent.benchmarks.runner import HostFacts, RunnerConfig
from meshforge.agent.main import app
from meshforge.common.exit_codes import ExitCode

pytestmark = pytest.mark.unit

cli = CliRunner()
PROG = "meshforge-agent"
FACTS = make_facts(gpus=(make_gpu(0),))


class FakeRunner:
    calls = 0
    result: Any = None  # a ScoreSet factory, or an exception to raise

    def __init__(
        self,
        config: RunnerConfig,
        host: HostFacts,
        *,
        fingerprint: str,
        log_fields: Any = None,
    ) -> None:
        self.config = config
        self.host = host
        self.fingerprint = fingerprint

    async def run(self) -> ScoreSet:
        type(self).calls += 1
        result = type(self).result
        if isinstance(result, BaseException):
            raise result
        if result is not None:
            made: ScoreSet = result(self.fingerprint)
            return made
        return make_scoreset(fingerprint=self.fingerprint)


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    import meshforge.agent.hardware as hardware_pkg

    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MESHFORGE_DATA_DIR", str(data_dir))
    monkeypatch.setattr(hardware_pkg.HardwareProvider, "get", lambda self: FACTS)
    monkeypatch.setattr(service, "ScoreRunner", FakeRunner)
    FakeRunner.calls = 0
    FakeRunner.result = None
    return data_dir


def _score(*args: str) -> Any:
    return cli.invoke(app, ["hardware", "score", *args], prog_name=PROG)


@pytest.mark.extras("agent")
def test_score_measures_prints_and_writes_the_cache(env: Path) -> None:
    result = _score()
    assert result.exit_code == ExitCode.OK, result.output
    assert FakeRunner.calls == 1
    assert "scores: measured" in result.stdout
    assert "cpu_fp32_gflops_1t" in result.stdout
    assert "gpu0.gpu_fp32_tflops" in result.stdout
    assert "unavailable: torch_not_installed" in result.stdout
    assert (env / "scores.json").is_file()


@pytest.mark.extras("agent")
def test_score_json_shape(env: Path) -> None:
    result = _score("--json")
    assert result.exit_code == ExitCode.OK, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {"source", "cache_status", "cache_reason", "scores"}
    assert payload["source"] == "measured"
    scores = ScoreSet.model_validate(payload["scores"])
    assert scores.run_id.startswith("score_")
    assert (
        scores.fingerprint
        == json.loads((env / "scores.json").read_text(encoding="utf-8"))["scores"][
            "fingerprint"
        ]
    )


@pytest.mark.extras("agent")
def test_a_valid_cache_is_reused_unless_forced() -> None:
    first = json.loads(_score("--json").stdout)
    again = json.loads(_score("--json").stdout)
    assert FakeRunner.calls == 1
    assert again["source"] == "cache" and again["cache_status"] == "valid"
    assert again["scores"]["run_id"] == first["scores"]["run_id"]
    forced = json.loads(_score("--json", "--force").stdout)
    assert FakeRunner.calls == 2
    assert forced["scores"]["run_id"] != first["scores"]["run_id"]


@pytest.mark.extras("agent")
def test_lock_held_by_another_scorer_is_busy_not_a_traceback(env: Path) -> None:
    with scorer_lock(env):
        result = _score()
    assert result.exit_code == ExitCode.BUSY
    assert int(ExitCode.BUSY) == 7
    assert "already in progress" in result.output
    assert "measuring" not in result.output  # nothing started
    assert isinstance(result.exception, SystemExit)  # typer.Exit, not a crash
    assert FakeRunner.calls == 0
    assert not (env / "scores.json").exists()


@pytest.mark.extras("agent")
def test_no_blas_is_a_truthful_exit_0_with_unavailable_rows() -> None:
    def no_blas(fp: str) -> ScoreSet:
        return make_scoreset(
            fingerprint=fp,
            metrics=tuple(
                Metric.not_measured(
                    name, MetricStatus.UNAVAILABLE, MetricReason.NO_BLAS_BACKEND
                )
                for name in HOST_METRICS
            ),
        )

    FakeRunner.result = no_blas
    result = _score()
    assert result.exit_code == ExitCode.OK, result.output
    assert result.stdout.count("unavailable: no_blas_backend") == 4


@pytest.mark.extras("agent")
def test_cache_write_failure_prints_the_scores_and_exits_failure(env: Path) -> None:
    (env / "scores.json").mkdir(parents=True)  # os.replace onto a dir fails
    result = _score()
    assert result.exit_code == ExitCode.FAILURE
    assert "measured, but could not write scores.json" in result.output
    assert "scores: measured" in result.stdout


@pytest.mark.extras("agent")
def test_ctrl_c_is_reported_not_a_traceback() -> None:
    FakeRunner.result = KeyboardInterrupt()
    result = _score()
    assert result.exit_code == ExitCode.FAILURE
    assert "interrupted" in result.output
    assert isinstance(result.exception, SystemExit)


@pytest.mark.extras("agent")
def test_show_prints_none_cached_then_the_cached_scores() -> None:
    before = cli.invoke(app, ["hardware", "show"], prog_name=PROG)
    assert before.exit_code == ExitCode.OK, before.output
    assert "scores: none cached (missing)" in before.stdout
    _score()
    after = cli.invoke(app, ["hardware", "show"], prog_name=PROG)
    assert "scores: cached (valid)" in after.stdout
    assert "cpu_fp32_gflops_1t" in after.stdout
    payload = json.loads(
        cli.invoke(app, ["hardware", "show", "--json"], prog_name=PROG).stdout
    )
    assert payload["scores"]["cache_status"] == "valid"
    assert payload["cpu"]["model"] == "Fictional CPU 9000"


@pytest.mark.extras("agent")
def test_show_marks_a_cache_from_other_hardware_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meshforge.agent.hardware as hardware_pkg

    _score()
    upgraded = make_facts(model="Upgraded CPU", gpus=(make_gpu(0),))
    monkeypatch.setattr(hardware_pkg.HardwareProvider, "get", lambda self: upgraded)
    result = cli.invoke(app, ["hardware", "show"], prog_name=PROG)
    assert "scores: cached (stale: hardware or backend changed" in result.stdout
