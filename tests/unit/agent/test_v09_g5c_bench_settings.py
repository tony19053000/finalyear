"""V0.9 G5c item 0.3: ``[agent.bench] preempt_revert_timeout_s``."""

from __future__ import annotations

from pathlib import Path

import pytest

from meshforge.agent.settings import AgentSettings, BenchSettings

pytestmark = pytest.mark.unit


def test_default_is_ten_seconds() -> None:
    assert BenchSettings().preempt_revert_timeout_s == 10.0


@pytest.mark.parametrize("bad", [0, -1, 601])
def test_out_of_range_is_refused(bad: float) -> None:
    with pytest.raises(ValueError, match="preempt_revert_timeout_s"):
        BenchSettings(preempt_revert_timeout_s=bad)


def test_it_loads_from_the_agent_section(tmp_path: Path) -> None:
    settings = AgentSettings.load(
        data_dir=tmp_path,
        agent={
            "controller_address": "127.0.0.1:1",
            "bench": {"preempt_revert_timeout_s": 2.5},
        },
    )
    assert settings.agent.bench.preempt_revert_timeout_s == 2.5
