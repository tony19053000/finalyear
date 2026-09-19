"""Unit tests for the S7 environment snapshot (0.2.11).

OS independent: the Linux sysfs trees are simulated in ``tmp_path`` and
``IS_LINUX`` is patched, so these run on Windows too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spikes._s7 import environment as env


def _sysfs(root: Path, governors: dict[str, str], cache: dict[int, str]) -> Path:
    for name, governor in governors.items():
        freq = root / name / "cpufreq"
        freq.mkdir(parents=True)
        (freq / "scaling_governor").write_text(governor + "\n", encoding="utf-8")
        (freq / "energy_performance_preference").write_text(
            "balance_performance\n", encoding="utf-8"
        )
    for i, (level, size) in enumerate(cache.items()):
        index = root / "cpu0" / "cache" / f"index{i}"
        index.mkdir(parents=True, exist_ok=True)
        (index / "level").write_text(str(level), encoding="utf-8")
        (index / "size").write_text(size, encoding="utf-8")
    return root


@pytest.mark.parametrize(
    ("probe", "expected"),
    [
        ({"ok": True, "value": {"on_ac": False}}, True),
        ({"ok": True, "value": {"on_ac": True}}, False),
        ({"ok": True, "value": {"on_ac": None}}, None),
        ({"ok": True, "value": {}}, None),
        ({"ok": False, "error": "no power source readable"}, None),
        (None, None),
        ({"ok": True, "value": "not a dict"}, None),
    ],
)
def test_on_battery_reads_the_inventory_power_probe(
    probe: dict[str, object] | None, expected: bool | None
) -> None:
    assert env.on_battery(probe) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("24 MiB", 24 * 2**20),
        ("32M", 32 * 2**20),
        ("12288K", 12288 * 2**10),
        ("1G", 2**30),
        ("1024", 1024),
        ("", None),
        (None, None),
        ("not a size", None),
        ("0K", None),
    ],
)
def test_parse_cache_size(text: str | None, expected: int | None) -> None:
    assert env.parse_cache_size(text) == expected


def test_last_level_cache_picks_the_highest_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(env, "IS_LINUX", True)
    root = _sysfs(tmp_path, {}, {1: "48K", 2: "1280K", 3: "24576K"})
    assert env.last_level_cache_bytes(root) == 24576 * 2**10


def test_last_level_cache_is_none_without_sysfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(env, "IS_LINUX", True)
    assert env.last_level_cache_bytes(tmp_path) is None
    monkeypatch.setattr(env, "IS_LINUX", False)
    assert env.last_level_cache_bytes(tmp_path) is None


def test_last_level_cache_survives_garbage_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(env, "IS_LINUX", True)
    root = _sysfs(tmp_path, {}, {3: "16M"})
    broken = root / "cpu0" / "cache" / "index9"
    broken.mkdir(parents=True)
    (broken / "level").write_text("not a level", encoding="utf-8")
    (broken / "size").write_text("??", encoding="utf-8")
    assert env.last_level_cache_bytes(root) == 16 * 2**20


def test_cpu_governors_reports_every_cpu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(env, "IS_LINUX", True)
    root = _sysfs(tmp_path, {"cpu0": "performance", "cpu1": "powersave"}, {})
    value = env.cpu_governors(root)
    assert value["governors"] == {"cpu0": "performance", "cpu1": "powersave"}
    assert value["distinct"] == ["performance", "powersave"]
    assert value["energy_performance_preference"] == ["balance_performance"]


def test_cpu_governors_without_cpufreq(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(env, "IS_LINUX", True)
    (tmp_path / "cpu0").mkdir()
    value = env.cpu_governors(tmp_path)
    assert value["governors"] is None
    assert "no cpufreq" in str(value["error"])


def test_cpu_governors_off_linux(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(env, "IS_LINUX", False)
    assert env.cpu_governors(tmp_path) == {"source": "unavailable", "governors": None}


def test_thermal_zones_read_millidegrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(env, "IS_LINUX", True)
    zone = tmp_path / "thermal_zone0"
    zone.mkdir()
    (zone / "temp").write_text("54000\n", encoding="utf-8")
    (zone / "type").write_text("x86_pkg_temp\n", encoding="utf-8")
    bad = tmp_path / "thermal_zone1"
    bad.mkdir()
    (bad / "temp").write_text("not a number", encoding="utf-8")
    zones = env.linux_thermal_zones(tmp_path)
    assert zones["thermal_zone0"] == {"type": "x86_pkg_temp", "temp_c": 54.0}
    assert zones["thermal_zone1"]["temp_c"] is None
    assert env.linux_thermal_zones(tmp_path / "missing") is None


def test_parse_nvidia_smi_row() -> None:
    row = env.parse_nvidia_smi_row("61, 1740, 6001, 0x0000000000000001, 42.35")
    assert row["temperature_c"] == 61.0
    assert row["sm_clock_mhz"] == 1740.0
    assert row["throttle_reasons_hex"] == "0x0000000000000001"
    assert row["power_draw_w"] == pytest.approx(42.35)


def test_parse_nvidia_smi_row_handles_not_supported() -> None:
    row = env.parse_nvidia_smi_row("55, [N/A], N/A, [Not Supported], ")
    assert row["temperature_c"] == 55.0
    assert row["sm_clock_mhz"] is None
    assert row["memory_clock_mhz"] is None
    assert row["throttle_reasons_hex"] is None
    assert row["power_draw_w"] is None


def test_snapshot_shape_with_an_injected_power_probe() -> None:
    doc = env.snapshot("before", power={"ok": True, "value": {"on_ac": False}})
    assert doc["when"] == "before"
    assert doc["on_battery"] is True
    assert set(doc) == {"when", "power", "on_battery", "cpu", "thermal_zones", "nvidia"}
