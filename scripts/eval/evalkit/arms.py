"""The compared arms: baselines B0-B6 (D416) and live ablations A5/A6 (D418).

An **arm** is what one job of the matrix is forced to: a placement (pinned to
the origin laptop, or a policy for the planner) and a **controller profile**
- the ``[controller.planner]`` settings the arm needs. Calibration (B4 vs B5),
the ML model (B6) and the disabled modes (A5/A6) are controller settings, not
per-job requests, so arms needing different profiles cannot run under one
controller configuration: the runner switches profile between blocks
(:mod:`.schedule` groups the arms of a profile together) through a hook the
operator provides (``--profile-hook``; the lab's restarts the controller
service with the profile's drop-in).

A7 (Wi-Fi vs Ethernet) is not an arm: the same campaign is run once per link
(``link:`` in the campaign file) and :mod:`.analysis` compares the two.

Rule policies (B1-B3) ignore calibration and ML, so they share the
``calibrated`` profile with B0 and B5 - one profile switch fewer per block.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

__all__ = [
    "ARMS",
    "PROFILES",
    "Arm",
    "Profile",
    "profile_env",
    "profile_toml",
]


@dataclass(frozen=True, slots=True)
class Profile:
    """``[controller.planner]`` settings one or more arms need."""

    name: str
    planner: Mapping[str, Any]
    """``calibration`` (``auto``/``off``), ``ml`` (``auto``/``off``) and
    ``disabled_modes`` (V1.0 G2, D418)."""
    description: str


PROFILES: Final[Mapping[str, Profile]] = {
    p.name: p
    for p in (
        Profile(
            "calibrated",
            {"calibration": "auto", "ml": "off", "disabled_modes": []},
            "planner v2 with the active calibration, no ML (V0.14)",
        ),
        Profile(
            "uncalibrated",
            {"calibration": "off", "ml": "off", "disabled_modes": []},
            "planner v2 on its raw analytical estimates",
        ),
        Profile(
            "ml",
            {"calibration": "auto", "ml": "auto", "disabled_modes": []},
            "planner v2 with calibration and the active ML model set (V0.15)",
        ),
        Profile(
            "ml_no_mode3",
            {"calibration": "auto", "ml": "auto", "disabled_modes": ["mode3"]},
            "the shipped planner with Mode 3 disabled (A5)",
        ),
        Profile(
            "ml_no_mode1",
            {"calibration": "auto", "ml": "auto", "disabled_modes": ["mode1"]},
            "the shipped planner with Mode 1 disabled (A6)",
        ),
    )
}


@dataclass(frozen=True, slots=True)
class Arm:
    """One compared configuration (module docstring)."""

    id: str
    label: str
    profile: str
    policy: str | None = None
    """The placement policy forced via ``placement.policy``; None = pinned."""
    local_pin: bool = False
    """B0: pinned to the origin laptop (``placement.local``)."""
    expect: Mapping[str, Any] = field(default_factory=dict)
    """What the arm's decisions must show (checked by the analysis):
    ``calibrated`` (bool) and ``ml`` (bool) - a decision that disagrees is
    counted as a profile mismatch, never silently mixed in."""

    def placement(self, origin_node_id: str, objective: str) -> dict[str, Any]:
        """The ``POST /v1/jobs`` ``placement`` of this arm."""
        if self.local_pin:
            return {"node_id": origin_node_id, "local": True}
        out: dict[str, Any] = {"policy": self.policy}
        if self.policy == "cost_model":
            out["objective"] = objective
        return out


ARMS: Final[Mapping[str, Arm]] = {
    a.id: a
    for a in (
        Arm("B0", "Local only", "calibrated", local_pin=True),
        Arm("B1", "round_robin", "calibrated", policy="round_robin"),
        Arm("B2", "random", "calibrated", policy="random"),
        Arm("B3", "least_loaded", "calibrated", policy="least_loaded"),
        Arm(
            "B4",
            "planner v2, uncalibrated",
            "uncalibrated",
            policy="cost_model",
            expect={"calibrated": False, "ml": False},
        ),
        Arm(
            "B5",
            "planner v2, calibrated",
            "calibrated",
            policy="cost_model",
            expect={"ml": False},
        ),
        Arm(
            "B6",
            "ML planner",
            "ml",
            policy="cost_model",
            expect={"ml": True},
        ),
        Arm("A5", "ML planner, Mode 3 disabled", "ml_no_mode3", policy="cost_model"),
        Arm("A6", "ML planner, Mode 1 disabled", "ml_no_mode1", policy="cost_model"),
    )
}


def profile_env(profile: Profile) -> dict[str, str]:
    """The profile as controller environment variables
    (``MESHFORGE_CONTROLLER__PLANNER__...``, pydantic-settings' nested form)."""
    out: dict[str, str] = {}
    for key in sorted(profile.planner):
        value = profile.planner[key]
        text = (
            "[" + ",".join(f'"{v}"' for v in value) + "]"
            if isinstance(value, list)
            else str(value)
        )
        out[f"MESHFORGE_CONTROLLER__PLANNER__{key.upper()}"] = text
    return out


def profile_toml(profile: Profile) -> str:
    """The profile as a ``[controller.planner]`` TOML drop-in."""
    lines = [f"# MeshForge evaluation profile {profile.name}: {profile.description}"]
    lines.append("[controller.planner]")
    for key in sorted(profile.planner):
        value = profile.planner[key]
        if isinstance(value, list):
            lines.append(f"{key} = [" + ", ".join(f'"{v}"' for v in value) + "]")
        else:
            lines.append(f'{key} = "{value}"')
    return "\n".join(lines) + "\n"
