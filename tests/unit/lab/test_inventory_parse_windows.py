"""Windows parsers of lab/inventory.py (0.2.2): Get-CimInstance / Get-NetAdapter JSON."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable

import pytest

from _inventory import parse_windows as pw
from _inventory.runner import _decode

Fixture = Callable[[str], str]
NVIDIA_LAPTOP = "windows/cim_bundle_nvidia_laptop.json"
NO_NVIDIA_PS51 = "windows/cim_bundle_no_nvidia_ps51.json"


def _bundle(fixture_text: Fixture, name: str) -> dict[str, dict[str, object]]:
    return pw.parse_cim_bundle(fixture_text(name))


def _data(bundle: dict[str, dict[str, object]], key: str) -> list[object]:
    entry = bundle[key]
    assert entry["ok"] is True, entry
    data = entry["data"]
    assert isinstance(data, list)
    return data


def test_bundle_has_every_query_key(fixture_text: Fixture) -> None:
    bundle = _bundle(fixture_text, NVIDIA_LAPTOP)
    assert set(bundle) == set(pw.CIM_QUERIES)
    assert bundle["secure_boot"] == {
        "ok": False,
        "error": "Access was denied: Confirm-SecureBootUEFI",
    }


def test_bundle_unwraps_single_objects_and_ps51_value_count(
    fixture_text: Fixture,
) -> None:
    bundle = _bundle(fixture_text, NO_NVIDIA_PS51)
    assert (
        len(_data(bundle, "processor")) == 1
    )  # ConvertTo-Json unwrapped a 1-element array
    assert (
        len(_data(bundle, "video_controllers")) == 1
    )  # PS 5.1 {"value": [...], "Count": 1}
    assert _data(bundle, "battery") == []
    assert bundle["net_ip"]["ok"] is False


def test_bundle_tolerates_utf8_bom_and_noise() -> None:
    text = '﻿WARNING: something\r\n{"processor":{"ok":true,"data":null}}\r\n'
    assert pw.parse_cim_bundle(text) == {"processor": {"ok": True, "data": []}}


@pytest.mark.parametrize(
    "text", ["", "powershell : The term is not recognized", "[1, 2]"]
)
def test_bundle_rejects_non_object(text: str) -> None:
    with pytest.raises((ValueError, TypeError)):
        pw.parse_cim_bundle(text)


def test_processors(fixture_text: Fixture) -> None:
    value = pw.parse_processors(
        _data(_bundle(fixture_text, NVIDIA_LAPTOP), "processor")
    )
    assert value["model"] == "AMD Ryzen 7 5800H with Radeon Graphics"
    assert (value["physical_cores"], value["logical_threads"]) == (8, 16)
    assert value["base_mhz"] == 3201
    assert value["max_mhz"] is None


def test_processors_two_sockets_and_empty() -> None:
    row = {"Name": "Xeon", "NumberOfCores": 8, "NumberOfLogicalProcessors": 16}
    assert pw.parse_processors([row, row])["physical_cores"] == 16
    with pytest.raises(ValueError):
        pw.parse_processors([])


def test_os_and_memory(fixture_text: Fixture) -> None:
    bundle = _bundle(fixture_text, NVIDIA_LAPTOP)
    os_rows = _data(bundle, "operating_system")
    assert pw.parse_os(os_rows) == {
        "name": "Microsoft Windows 11 Home",
        "version": "10.0.22631",
        "build": "22631",
        "architecture": "64-bit",
    }
    mem = pw.parse_memory(os_rows, _data(bundle, "computer_system"))
    assert mem == {"total_bytes": 16541507584, "available_bytes": 7340032 * 1024}


def test_memory_fallbacks() -> None:
    mem = pw.parse_memory([{"TotalVisibleMemorySize": 1024}], [])
    assert mem["total_bytes"] == 1024 * 1024
    with pytest.raises(ValueError):
        pw.parse_memory([], [])


def test_video_controllers_hybrid_and_capped_vram(fixture_text: Fixture) -> None:
    gpus = pw.parse_video_controllers(
        _data(_bundle(fixture_text, NVIDIA_LAPTOP), "video_controllers")
    )
    amd, nvidia = gpus
    assert (amd["vendor"], amd["vram_mib"], amd["vram_note"]) == ("AMD", 512, None)
    assert nvidia["vendor"] == "NVIDIA"
    assert nvidia["vram_mib"] == 4095
    assert "4 GiB" in str(nvidia["vram_note"])


def test_video_controllers_no_nvidia(fixture_text: Fixture) -> None:
    gpus = pw.parse_video_controllers(
        _data(_bundle(fixture_text, NO_NVIDIA_PS51), "video_controllers")
    )
    assert [(g["vendor"], g["name"]) for g in gpus] == [
        ("Intel", "Intel(R) UHD Graphics 620")
    ]


def test_net_adapters_wifi_ethernet_virtual_bluetooth(fixture_text: Fixture) -> None:
    bundle = _bundle(fixture_text, NVIDIA_LAPTOP)
    adapters = {
        a["name"]: a
        for a in pw.parse_net_adapters(
            _data(bundle, "net_adapters"), _data(bundle, "net_ip")
        )
    }
    wifi, eth = adapters["Wi-Fi"], adapters["Ethernet"]
    assert (wifi["kind"], wifi["link_speed_mbit"], wifi["mac"]) == (
        "wifi",
        866.7,
        "f4:26:79:aa:bb:cc",
    )
    assert wifi["ipv4"] == ["192.168.31.42/24"]
    assert (eth["kind"], eth["link_speed_mbit"], eth["up"]) == (
        "ethernet",
        1000.0,
        True,
    )
    assert eth["ipv4"] == ["10.10.0.12/24"]  # public 49.36.10.77 dropped
    assert adapters["vEthernet (WSL (Hyper-V firewall))"]["kind"] == "virtual"
    bt = adapters["Bluetooth Network Connection"]
    assert (bt["kind"], bt["up"], bt["link_speed_mbit"]) == ("bluetooth", False, None)
    assert "49.36.10.77" not in json.dumps(adapters)


def test_net_adapter_speed_from_text_when_bits_missing(fixture_text: Fixture) -> None:
    bundle = _bundle(fixture_text, NO_NVIDIA_PS51)
    (wifi,) = pw.parse_net_adapters(_data(bundle, "net_adapters"), [])
    assert (wifi["kind"], wifi["link_speed_mbit"], wifi["ipv4"]) == ("wifi", 144.4, [])


@pytest.mark.parametrize(
    ("text", "bps", "mbit"),
    [
        ("1 Gbps", None, 1000.0),
        ("144.4 Mbps", None, 144.4),
        ("100 Kbps", 0, 0.1),
        ("", None, None),
    ],
)
def test_link_speed(text: str, bps: int | None, mbit: float | None) -> None:
    assert pw.parse_link_speed_mbit(text, bps) == mbit


def test_classify_adapter_by_description_when_medium_missing() -> None:
    assert (
        pw.classify_adapter({"InterfaceDescription": "Qualcomm Wireless Adapter"})
        == "wifi"
    )
    assert pw.classify_adapter({"InterfaceDescription": "USB Ethernet"}) == "ethernet"
    assert pw.classify_adapter({"InterfaceDescription": "Something"}) == "other"


def test_battery() -> None:
    assert pw.parse_battery([]) == {
        "on_ac": None,
        "has_battery": False,
        "batteries": [],
    }
    on_ac = pw.parse_battery([{"BatteryStatus": 2, "EstimatedChargeRemaining": 87}])
    assert on_ac == {
        "on_ac": True,
        "has_battery": True,
        "batteries": [{"percent": 87, "status": 2}],
    }
    assert pw.parse_battery([{"BatteryStatus": 1}])["on_ac"] is False


@pytest.mark.parametrize(
    ("entry", "state"),
    [
        ({"ok": True, "data": [True]}, "enabled"),
        ({"ok": True, "data": [False]}, "disabled"),
        (
            {"ok": False, "error": "Access was denied: Confirm-SecureBootUEFI"},
            "unknown",
        ),
        (
            {"ok": False, "error": "Cmdlet not supported on this platform: 0xC0000002"},
            "unsupported",
        ),
        (None, "unknown"),
    ],
)
def test_secure_boot_result(entry: dict[str, object] | None, state: str) -> None:
    assert pw.parse_secure_boot_result(entry)["state"] == state


def test_wsl_list_from_utf16_output() -> None:
    raw = (
        "  NAME              STATE           VERSION\r\n"
        "* Ubuntu-24.04      Running         2\r\n"
        "  docker-desktop    Stopped         2\r\n"
    ).encode("utf-16-le")
    distros = pw.parse_wsl_list(_decode(raw, "utf-16-le"))
    assert distros == [
        {"name": "Ubuntu-24.04", "state": "Running", "version": 2, "default": True},
        {"name": "docker-desktop", "state": "Stopped", "version": 2, "default": False},
    ]
    assert pw.parse_wsl_list("") == []


def test_script_selects_no_identifying_properties() -> None:
    script = pw.CIM_SCRIPT.lower()
    for forbidden in (
        "serialnumber",
        "username",
        "registereduser",
        "ssid",
        "uuid",
        "profile",
    ):
        assert forbidden not in script
    assert "remove-typedata system.array" in script


def test_encoded_command_round_trips() -> None:
    encoded = pw.encoded_command(pw.CIM_SCRIPT)
    assert base64.b64decode(encoded).decode("utf-16-le") == pw.CIM_SCRIPT
