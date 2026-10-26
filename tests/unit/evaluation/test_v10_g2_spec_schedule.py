"""V1.0 G2 (D416/D417): the campaign file and block randomisation."""

from __future__ import annotations

import itertools
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from mf_eval_kit import load

load()

from evalkit.arms import (  # noqa: E402
    ARMS,
    PROFILES,
    profile_env,
    profile_env_file,
    profile_toml,
)
from evalkit.schedule import schedule, schedule_digest  # noqa: E402
from evalkit.spec import SpecError, load_spec, parse_spec, spec_digest  # noqa: E402

pytestmark = pytest.mark.unit


def _raw(**over: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "name": "camp",
        "seed": 3,
        "link": "wifi",
        "repetitions": 4,
        "arms": ["B0", "B1", "B4", "B5", "B6"],
        "families": [
            {"family": "cpu.primes", "sizes": {"s": {"limit": 100_000_000}}},
            {
                "family": "split.hash_files",
                "sizes": {"s": {"files": 256}, "m": {"files": 256, "rounds": 150}},
            },
        ],
    }
    raw.update(over)
    return raw


def test_a_valid_file_parses_and_its_digest_is_stable() -> None:
    text = """
name: v1-eval-wifi
seed: 20261027
link: wifi
repetitions: 10
arms: [B0, B5]
families:
  - family: cpu.primes
    sizes: {s: {limit: 100000000, repeats: 4}}
"""
    spec = load_spec(text)
    assert spec.evidence is True
    assert spec_digest(spec) == spec_digest(load_spec(text))
    assert spec_digest(spec) != spec_digest(
        load_spec(text.replace("seed: 20261027", "seed: 1"))
    )


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"arms": ["B9"]}, "unknown arm"),
        ({"arms": ["B0", "B0"]}, "listed twice"),
        ({"link": "lte"}, "link"),
        ({"repetitions": 0}, "repetitions"),
        ({"name": "Bad Name"}, "name"),
        ({"extra": 1}, "extra"),
        (
            {"families": [{"family": "cpu.nope", "sizes": {"s": {}}}]},
            "unknown workload family",
        ),
        (
            {"families": [{"family": "cpu.primes", "sizes": {"s": {"limit": 5}}}]},
            "limit",
        ),
        (
            # below the 5 s benchmark window: refused unless smoke
            {
                "families": [
                    {
                        "family": "cpu.primes",
                        "sizes": {"s": {"limit": 10_000_000, "repeats": 1}},
                    }
                ]
            },
            "a benchmark run must take",
        ),
    ],
)
def test_bad_files_are_refused_with_the_reason(
    over: dict[str, Any], needle: str
) -> None:
    with pytest.raises(SpecError) as info:
        parse_spec(_raw(**over))
    assert needle in str(info.value)


def test_smoke_lifts_only_the_runtime_window() -> None:
    tiny = [
        {"family": "cpu.primes", "sizes": {"s": {"limit": 10_000_000, "repeats": 1}}}
    ]
    spec = parse_spec(_raw(families=tiny, smoke=True))
    assert spec.evidence is False
    with pytest.raises(SpecError):  # bounds still apply
        parse_spec(
            _raw(
                families=[{"family": "cpu.primes", "sizes": {"s": {"limit": 1}}}],
                smoke=True,
            )
        )


def test_not_yaml_or_not_a_mapping_is_refused() -> None:
    with pytest.raises(SpecError):
        load_spec("name: [unclosed")
    with pytest.raises(SpecError):
        load_spec("- a\n- b\n")


def test_every_cell_runs_once_per_block_with_profiles_contiguous() -> None:
    spec = parse_spec(_raw())
    runs = schedule(spec)
    cells = [(f, s, a) for f, s in spec.cells() for a in spec.arms]
    assert len(runs) == len(cells) * spec.repetitions
    assert [r.order for r in runs] == list(range(len(runs)))
    for block in range(spec.repetitions):
        mine = [r for r in runs if r.block == block]
        assert sorted((r.family, r.size, r.arm) for r in mine) == sorted(cells)
        # a profile's runs are contiguous inside a block: one switch each
        profiles = [r.profile for r in mine]
        switches = sum(1 for a, b in itertools.pairwise(profiles) if a != b)
        assert switches == len(set(profiles)) - 1
        assert all(r.profile == ARMS[r.arm].profile for r in mine)
    # blocks follow each other: block b's runs all precede block b + 1's
    assert [r.block for r in runs] == sorted(r.block for r in runs)


def test_the_order_is_randomised_per_block_but_deterministic() -> None:
    spec = parse_spec(_raw(repetitions=6))
    runs = schedule(spec)
    assert schedule_digest(runs) == schedule_digest(
        schedule(parse_spec(_raw(repetitions=6)))
    )
    orders = {
        tuple(r.run_id.split("-", 1)[1] for r in runs if r.block == b) for b in range(6)
    }
    assert len(orders) > 1  # not the same order every block
    other = schedule(parse_spec(_raw(repetitions=6, seed=4)))
    assert schedule_digest(other) != schedule_digest(runs)


def test_every_run_has_its_own_workload_seed() -> None:
    runs = schedule(parse_spec(_raw()))
    seeds = Counter(r.workload_seed for r in runs)
    assert max(seeds.values()) == 1
    assert all(0 <= r.workload_seed < 2**31 for r in runs)
    assert len({r.run_id for r in runs}) == len(runs)


def test_profiles_render_for_the_hook() -> None:
    toml = profile_toml(PROFILES["ml_no_mode3"])
    assert "[controller.planner]" in toml
    assert 'disabled_modes = ["mode3"]' in toml
    assert 'calibration = "auto"' in toml and 'ml = "auto"' in toml
    env = profile_env(PROFILES["uncalibrated"])
    assert env["MESHFORGE_CONTROLLER__PLANNER__CALIBRATION"] == "off"
    assert env["MESHFORGE_CONTROLLER__PLANNER__DISABLED_MODES"] == "[]"


def test_profile_drop_ins_are_valid_controller_settings() -> None:
    import tomllib

    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.controller.settings import PlannerSettings

    for profile in PROFILES.values():
        table = tomllib.loads(profile_toml(profile))["controller"]["planner"]
        settings = PlannerSettings(**table)
        assert list(settings.disabled_modes) == profile.planner["disabled_modes"]


def test_arm_placements() -> None:
    assert ARMS["B0"].placement("node_x", "min_time") == {
        "node_id": "node_x",
        "local": True,
    }
    assert ARMS["B1"].placement("node_x", "min_time") == {"policy": "round_robin"}
    assert ARMS["B5"].placement("node_x", "min_network") == {
        "policy": "cost_model",
        "objective": "min_network",
    }


def _systemd_env(text: str) -> dict[str, str]:
    """What systemd's EnvironmentFile parser yields for the lines the
    profile file uses (``KEY='value'``: the single quotes are removed)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        assert value.startswith("'") and value.endswith("'"), line
        out[key] = value[1:-1]
    return out


def test_profile_env_files_configure_the_controller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The env file the hook installs overrides the TOML: loading the
    controller settings with it gives exactly the profile (A5/A6 included)."""
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.controller.settings import ControllerSettings

    for name in list(os.environ):
        if name.startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    for profile in PROFILES.values():
        values = _systemd_env(profile_env_file(profile))
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        planner = ControllerSettings.load(data_dir=tmp_path / "d").controller.planner
        assert planner.calibration == profile.planner["calibration"]
        assert planner.ml == profile.planner["ml"]
        assert list(planner.disabled_modes) == profile.planner["disabled_modes"]


@pytest.mark.skipif(sys.platform == "win32", reason="a Linux controller hook")
def test_the_lab_hook_accepts_profile_files_and_refuses_others(tmp_path: Path) -> None:
    hook = Path(__file__).resolve().parents[3] / "lab" / "eval_profile.sh"

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(hook), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    for profile in PROFILES.values():
        path = tmp_path / f"{profile.name}.env"
        path.write_text(profile_env_file(profile), "utf-8")
        done = run("--check", str(path))
        assert done.returncode == 0, done.stderr
    evil = tmp_path / "evil.env"
    evil.write_text("LD_PRELOAD='/tmp/x.so'\n", "utf-8")
    assert run("--check", str(evil)).returncode == 2
    assert run("Bad-Name", str(evil)).returncode == 2
    assert run("calibrated", str(evil)).returncode == 2
    assert run("calibrated", str(tmp_path / "missing.env")).returncode == 2
    assert run().returncode == 2
