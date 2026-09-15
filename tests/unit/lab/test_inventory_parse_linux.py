"""Linux parsers of lab/inventory.py (0.2.2) against outputs recorded on Laptop 1."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from _inventory import parse_linux as pl

Fixture = Callable[[str], str]


def test_lscpu_hybrid_intel_laptop(fixture_text: Fixture) -> None:
    value = pl.parse_lscpu(fixture_text("linux/lscpu_i7_12650h.txt"))
    assert value["model"] == "12th Gen Intel(R) Core(TM) i7-12650H"
    assert value["physical_cores"] == 10
    assert value["logical_threads"] == 16
    assert value["sockets"] == 1
    assert value["max_mhz"] == 4700.0
    assert value["min_mhz"] == 400.0


def test_lscpu_multi_socket_without_frequencies() -> None:
    text = "Architecture: aarch64\nCPU(s): 8\nModel name: Cortex-A72\n"
    text += "Core(s) per socket: 4\nSocket(s): 2\n"
    value = pl.parse_lscpu(text)
    assert value["physical_cores"] == 8
    assert value["max_mhz"] is None


def test_lscpu_unrecognised_output_raises() -> None:
    with pytest.raises(ValueError):
        pl.parse_lscpu("lscpu: failed to determine number of CPUs")


def test_cpuinfo_fallback(fixture_text: Fixture) -> None:
    value = pl.parse_cpuinfo(fixture_text("linux/cpuinfo_4threads.txt"))
    assert value["logical_threads"] == 4
    assert "i7-12650H" in str(value["model"])
    assert 1 <= value["physical_cores"] <= 4


def test_cpuinfo_empty_raises() -> None:
    with pytest.raises(ValueError):
        pl.parse_cpuinfo("")


def test_khz_file() -> None:
    assert pl.parse_khz_file("2300000\n") == 2300.0
    assert pl.parse_khz_file("") is None
    assert pl.parse_khz_file("0") is None


def test_meminfo(fixture_text: Fixture) -> None:
    text = fixture_text("linux/meminfo.txt")
    kib = int(text.splitlines()[0].split()[1])
    value = pl.parse_meminfo(text)
    assert value["total_bytes"] == kib * 1024
    assert isinstance(value["available_bytes"], int)


def test_meminfo_missing_total_raises() -> None:
    with pytest.raises(ValueError):
        pl.parse_meminfo("MemFree: 1 kB\n")


def test_os_release(fixture_text: Fixture) -> None:
    info = pl.parse_os_release(fixture_text("linux/os-release_ubuntu2404.txt"))
    assert info["NAME"] == "Ubuntu"
    assert info["VERSION_ID"] == "24.04"
    assert info["PRETTY_NAME"].startswith("Ubuntu 24.04")


def test_os_release_tolerates_bad_quoting() -> None:
    info = pl.parse_os_release('# comment\nNAME="Broken\nID=x\n\n')
    assert info["ID"] == "x"
    assert info["NAME"] == "Broken"


@pytest.mark.parametrize(
    ("proc_version", "env", "expected"),
    [
        (
            "Linux version 6.8.0-139-generic (buildd@lcy02) #139-Ubuntu SMP",
            None,
            (False, None),
        ),
        (
            "Linux version 5.15.153.1-microsoft-standard-WSL2 (root@x) #1 SMP",
            None,
            (True, 2),
        ),
        (
            "Linux version 4.4.0-19041-Microsoft (Microsoft@Microsoft.com)",
            None,
            (True, 1),
        ),
        ("Linux version 6.1.0 custom", "Ubuntu-24.04", (True, 1)),
    ],
)
def test_detect_wsl(
    proc_version: str, env: str | None, expected: tuple[bool, int | None]
) -> None:
    value = pl.detect_wsl(proc_version, "", env)
    assert (value["is_wsl"], value["wsl_version"]) == expected


def test_ip_json_keeps_private_addresses(fixture_text: Fixture) -> None:
    ips = pl.parse_ip_json(fixture_text("linux/ip_j_4_addr.json"))
    assert ips["lo"] == [{"address": "127.0.0.1", "prefixlen": 8}]
    assert ips["wlp0s20f3"][0]["address"] == "192.168.31.13"


def test_ip_json_drops_public_addresses() -> None:
    text = (
        '[{"ifname":"eth0","addr_info":[{"family":"inet","local":"49.36.10.77","prefixlen":32},'
        '{"family":"inet","local":"10.0.0.5","prefixlen":24},'
        '{"family":"inet6","local":"fe80::1","prefixlen":64}]}]'
    )
    assert pl.parse_ip_json(text) == {
        "eth0": [{"address": "10.0.0.5", "prefixlen": 24}]
    }


def test_ip_json_rejects_non_list() -> None:
    with pytest.raises(TypeError):
        pl.parse_ip_json('{"not": "a list"}')


@pytest.mark.parametrize(
    ("name", "device", "wireless", "type_code", "kind"),
    [
        ("lo", False, False, "772", "loopback"),
        ("wlp0s20f3", True, True, "1", "wifi"),
        ("eno1", True, False, "1\n", "ethernet"),
        ("docker0", False, False, "1", "virtual"),
        ("wwan0", True, False, "519", "other"),
    ],
)
def test_classify_sys_net(
    name: str, device: bool, wireless: bool, type_code: str, kind: str
) -> None:
    assert (
        pl.classify_sys_net(
            name, has_device=device, has_wireless=wireless, type_code=type_code
        )
        == kind
    )


@pytest.mark.parametrize(
    ("text", "expected"), [("1000\n", 1000), ("-1\n", None), ("", None), (None, None)]
)
def test_sys_speed(text: str | None, expected: int | None) -> None:
    assert pl.parse_sys_speed(text) == expected


def test_iw_link_bitrate_only() -> None:
    text = (
        "Connected to aa:bb:cc:dd:ee:ff (on wlp0s20f3)\n\tSSID: MyHomeNetwork\n"
        "\tfreq: 5180\n\ttx bitrate: 866.7 MBit/s VHT-MCS 9 80MHz short GI VHT-NSS 2\n"
    )
    assert pl.parse_iw_link(text) == 866.7
    assert pl.parse_iw_link("Not connected.") is None


def test_lspci_hybrid_graphics(fixture_text: Fixture) -> None:
    gpus = pl.parse_lspci_mm(fixture_text("linux/lspci_mm_hybrid.txt"))
    assert [(g["vendor"], g["name"]) for g in gpus] == [
        ("Intel", "Alder Lake-P GT1 [UHD Graphics]"),
        ("NVIDIA", "GA107M [GeForce RTX 3050 Mobile]"),
    ]


def test_lspci_no_gpu_and_garbage() -> None:
    text = '00:1f.6 "Ethernet controller" "Intel Corporation" "I219-V"\nunbalanced "quote\n'
    assert pl.parse_lspci_mm(text) == []


@pytest.mark.parametrize(
    ("vendor", "short"),
    [
        ("NVIDIA Corporation", "NVIDIA"),
        ("Advanced Micro Devices, Inc. [AMD/ATI]", "AMD"),
        ("Intel Corporation", "Intel"),
        ("Moore Threads", "Moore Threads"),
        (None, None),
    ],
)
def test_normalise_vendor(vendor: str | None, short: str | None) -> None:
    assert pl.normalise_vendor(vendor) == short


@pytest.mark.parametrize(
    ("text", "state"),
    [
        ("SecureBoot enabled\n", "enabled"),
        ("SecureBoot disabled\nPlatform is in Setup Mode\n", "disabled"),
        ("This system doesn't support Secure Boot\n", "unsupported"),
        ("EFI variables are not supported on this system\n", "unsupported"),
        ("Failed to read SecureBoot\n", "unknown"),
    ],
)
def test_mokutil_sb_state(text: str, state: str) -> None:
    assert pl.parse_mokutil_sb_state(text) == state


def test_secureboot_efivar() -> None:
    assert pl.parse_secureboot_efivar(bytes([6, 0, 0, 0, 1])) == "enabled"
    assert pl.parse_secureboot_efivar(bytes([6, 0, 0, 0, 0])) == "disabled"
    assert pl.parse_secureboot_efivar(b"") == "unknown"


def test_power_laptop_on_battery() -> None:
    value = pl.parse_power_supplies(
        {
            "ADP1": {"type": "Mains", "online": "0"},
            "BAT0": {"type": "Battery", "status": "Discharging", "capacity": "69"},
        }
    )
    assert value == {
        "on_ac": False,
        "has_battery": True,
        "batteries": [{"percent": 69, "status": "Discharging"}],
    }


def test_power_plugged_in_and_desktop() -> None:
    plugged = pl.parse_power_supplies(
        {
            "AC": {"type": "Mains", "online": "1"},
            "BAT1": {"type": "Battery", "status": "Charging"},
        }
    )
    assert plugged["on_ac"] is True
    desktop = pl.parse_power_supplies({})
    assert desktop == {"on_ac": None, "has_battery": False, "batteries": []}
    battery_only = pl.parse_power_supplies(
        {"BAT0": {"type": "Battery", "status": "Full"}}
    )
    assert battery_only["on_ac"] is True
