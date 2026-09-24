"""V0.3 G6 tester round 2: settings edges for D27/D29/D26 (docs/briefs/V0.3-fabric-core.md
§5f, priority 4).

Every boundary the new validators accept/reject, plus the behaviour change an existing
``config.toml`` written before D29 hits: ``hello_timeout_s = 5`` (the old default) combined
with the new default ``cap_s`` (5 s, D27) now exceeds ``RECONNECT_ATTEMPT_BUDGET_S`` (8 s,
D29) and the agent refuses to start. That is an intentional, documented behaviour change
(``RECONNECT_ATTEMPT_BUDGET_S``'s docstring); this module proves the message is clear enough
for an operator to fix their file, not just that it fails.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from meshforge.agent.settings import (
    RECONNECT_ATTEMPT_BUDGET_S,
    RECONNECT_CAP_BOUND_S,
    AgentSection,
    AgentSettings,
    ReconnectSettings,
)
from meshforge.common.errors import ConfigError
from meshforge.controller.settings import ControllerSection, ControllerSettings

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


# --------------------------------------------------------- D27: cap_s bound


def test_cap_s_at_the_bound_is_accepted_one_step_past_is_not() -> None:
    ReconnectSettings(cap_s=RECONNECT_CAP_BOUND_S)  # 5.0 s: accepted
    with pytest.raises(ValidationError, match="cap_s"):
        ReconnectSettings(cap_s=RECONNECT_CAP_BOUND_S + 1e-9)


def test_cap_s_must_stay_positive() -> None:
    with pytest.raises(ValidationError):
        ReconnectSettings(cap_s=0.0)
    with pytest.raises(ValidationError):
        ReconnectSettings(cap_s=-1.0)


# ------------------------------------------------- D29: cap_s + hello_timeout_s


@pytest.mark.parametrize(
    ("cap_s", "hello_timeout_s"),
    [
        (RECONNECT_CAP_BOUND_S, 3.0),  # shipped: 5 + 3 = 8, exactly the budget
        (0.5, RECONNECT_ATTEMPT_BUDGET_S - 0.5),  # 0.5 is the default base_s floor
        (RECONNECT_CAP_BOUND_S, RECONNECT_ATTEMPT_BUDGET_S - RECONNECT_CAP_BOUND_S),
    ],
)
def test_budget_accepts_every_combination_exactly_at_the_edge(
    cap_s: float, hello_timeout_s: float
) -> None:
    AgentSection(hello_timeout_s=hello_timeout_s, reconnect={"cap_s": cap_s})


@pytest.mark.parametrize(
    ("cap_s", "hello_timeout_s"),
    [
        (RECONNECT_CAP_BOUND_S, 3.0 + 1e-6),  # one hair over the budget
        (RECONNECT_CAP_BOUND_S, RECONNECT_CAP_BOUND_S),  # 5 + 5 = 10 (old shipped combo)
        (RECONNECT_CAP_BOUND_S, RECONNECT_ATTEMPT_BUDGET_S),  # 5 + 8
    ],
)
def test_budget_rejects_every_combination_one_step_past_the_edge(
    cap_s: float, hello_timeout_s: float
) -> None:
    with pytest.raises(ValidationError, match="criterion 2"):
        AgentSection(hello_timeout_s=hello_timeout_s, reconnect={"cap_s": cap_s})


def test_old_config_toml_with_the_pre_d29_hello_timeout_now_fails_to_load(
    tmp_path: Path,
) -> None:
    """A ``config.toml`` written before D29, setting only ``hello_timeout_s = 5``
    (the old shipped default) and leaving ``[agent.reconnect]`` untouched (so it
    gets the new default ``cap_s`` = 5 s, D27), now exceeds the 8 s budget: 5 + 5
    = 10. This is a deliberate behaviour change (RECONNECT_ATTEMPT_BUDGET_S's
    docstring) - this test proves it is refused with a message an operator can
    act on (names both settings and the 8 s bound), not a bare pydantic dump."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[agent]\ncontroller_address = "10.0.0.7:47100"\nhello_timeout_s = 5\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as info:
        AgentSettings.load(cfg)
    message = str(info.value)
    assert "hello_timeout_s" in message or "cap_s" in message
    assert "8" in message  # the budget itself is named, not just "invalid"


def test_a_config_that_only_raises_cap_s_within_the_old_15s_world_still_works() -> None:
    """An operator who *lowers* hello_timeout_s to leave room stays unaffected -
    the fix does not block every non-default config, only ones that overrun the
    budget."""
    AgentSection(hello_timeout_s=1.0, reconnect={"cap_s": RECONNECT_CAP_BOUND_S})


# ---------------------------------------------------------- D26: bucket_grace_s


def test_bucket_grace_s_accepts_zero_and_the_documented_default() -> None:
    ControllerSection(resources={"bucket_grace_s": 0.0})
    s = ControllerSettings.load()
    assert s.controller.resources.bucket_grace_s == 20.0


def test_bucket_grace_s_rejects_negative_and_past_the_bound() -> None:
    with pytest.raises(ValidationError):
        ControllerSection(resources={"bucket_grace_s": -0.01})
    with pytest.raises(ValidationError):
        ControllerSection(resources={"bucket_grace_s": 3600.01})
    ControllerSection(resources={"bucket_grace_s": 3600.0})  # the bound itself


def test_a_long_bucket_grace_still_yields_a_positive_max_open_buckets_headroom() -> None:
    """D26: grace buckets must not count against ``max_open_buckets`` (that cap is
    for clock jumps). A large grace relative to a small bucket_s must still leave
    the Downsampler usable (finite, > the base cap)."""
    from meshforge.controller.resources import DEFAULT_MAX_OPEN_BUCKETS, Downsampler

    d = Downsampler(bucket_s=1.0, max_open_buckets=2, bucket_grace_s=3600.0)
    assert d.max_open_buckets > 2
    assert d.max_open_buckets == max(1, 2) + 3600  # ceil(3600 / 1.0)
    assert d.max_open_buckets < 10_000  # sane, not unbounded
