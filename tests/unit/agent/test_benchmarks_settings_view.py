"""G7-A (0.3.4, item 5): ``[agent.scores]`` validation, and the score renderer."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from mf_bench_fakes import make_scoreset, ok_metric
from pydantic import ValidationError

from meshforge.agent.benchmarks.model import Metric, MetricName
from meshforge.agent.benchmarks.view import format_value, score_lines
from meshforge.agent.settings import AgentSettings, ScoreSettings
from meshforge.common.errors import ConfigError

pytestmark = pytest.mark.unit


def test_defaults_match_the_brief() -> None:
    s = ScoreSettings()
    assert (
        s.enabled,
        s.start_delay_s,
        s.budget_s,
        s.case_timeout_s,
        s.cache_poll_s,
    ) == (
        True,
        15.0,
        90.0,
        30.0,
        10.0,
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"start_delay_s": -1},
        {"start_delay_s": 3601},
        {"budget_s": 0.5},
        {"budget_s": 1801},
        {"case_timeout_s": 0},
        {"case_timeout_s": 601},
        {"cache_poll_s": 0.01},
        {"cache_poll_s": 3601},
        {"budget_s": 20, "case_timeout_s": 30},
        {"unknown_key": 1},
    ],
)
def test_bounds_are_enforced(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ScoreSettings(**bad)


def test_boundaries_are_accepted() -> None:
    ScoreSettings(start_delay_s=0, budget_s=1, case_timeout_s=1, cache_poll_s=0.05)
    ScoreSettings(
        start_delay_s=3600, budget_s=1800, case_timeout_s=600, cache_poll_s=3600
    )


def test_env_and_toml_reach_the_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    config = tmp_path / "config.toml"
    config.write_text(
        "[agent.scores]\nenabled = false\nbudget_s = 45\n", encoding="utf-8"
    )
    monkeypatch.setenv("MESHFORGE_AGENT__SCORES__START_DELAY_S", "2.5")
    settings = AgentSettings.load(config)
    assert settings.agent.scores.enabled is False
    assert settings.agent.scores.budget_s == 45
    assert settings.agent.scores.start_delay_s == 2.5


def test_a_bad_toml_value_is_a_config_error(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[agent.scores]\ncase_timeout_s = -3\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        AgentSettings.load(config)


# --------------------------------------------------------------------- view
def test_format_value_keeps_three_significant_digits() -> None:
    assert [format_value(v) for v in (312.4, 19.29, 1.816, 0.5)] == [
        "312",
        "19.3",
        "1.82",
        "0.50",
    ]


def test_score_lines_show_every_metric_and_why_not() -> None:
    lower = Metric(
        **{
            **ok_metric(MetricName.MEM_TRIAD_GB_S, 8.88).model_dump(),
            "lower_bound": True,
            "threads_effective": 1,
        }
    )
    scores = make_scoreset(
        metrics=(
            ok_metric(MetricName.CPU_FP32_GFLOPS_1T, 97.3),
            lower,
            *make_scoreset().metrics[2:],
        )
    )
    text = "\n".join(score_lines(scores, source="measured"))
    assert "97.3 ± 0.97 GFLOP/s" in text
    assert "lower bound, 1 thread(s)" in text
    assert "gpu0.gpu_fp32_tflops" in text and "unavailable: torch_not_installed" in text
    assert "on battery no" in text and "priority lowered" in text
