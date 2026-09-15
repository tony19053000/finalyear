"""Cross-platform parsers of lab/inventory.py (0.2.2): nvidia-smi, docker, git, versions."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from _inventory import parse_common as pc

Fixture = Callable[[str], str]


def test_nvidia_smi_single_gpu() -> None:
    text = "0, NVIDIA GeForce RTX 3050 Laptop GPU, 4096, 595.44.02, 8.6, 00000000:01:00.0\n"
    (gpu,) = pc.parse_nvidia_smi_csv(text, pc.NVIDIA_QUERY_FIELDS)
    assert gpu == {
        "index": 0,
        "name": "NVIDIA GeForce RTX 3050 Laptop GPU",
        "vendor": "NVIDIA",
        "vram_mib": 4096,
        "driver_version": "595.44.02",
        "compute_capability": "8.6",
        "pci_bus_id": "00000000:01:00.0",
    }


def test_nvidia_smi_two_gpus_with_na_fields() -> None:
    text = (
        "0, NVIDIA GeForce RTX 3060 Laptop GPU, 6144, 566.36, 8.6, 00000000:01:00.0\r\n"
        "1, Tesla K80, [N/A], 470.82.01, [N/A], 00000000:02:00.0\r\n\r\n"
    )
    gpus = pc.parse_nvidia_smi_csv(text, pc.NVIDIA_QUERY_FIELDS)
    assert [g["index"] for g in gpus] == [0, 1]
    assert gpus[1]["compute_capability"] is None
    assert gpus[1]["vram_mib"] is None


def test_nvidia_smi_legacy_fields() -> None:
    text = "0, GeForce GTX 1050, 4096, 456.71, 00000000:01:00.0\n"
    (gpu,) = pc.parse_nvidia_smi_csv(text, pc.NVIDIA_QUERY_FIELDS_LEGACY)
    assert gpu["compute_capability"] is None
    assert gpu["driver_version"] == "456.71"


def test_nvidia_smi_driver_failure_text_is_not_csv(fixture_text: Fixture) -> None:
    text = fixture_text("common/nvidia_smi_driver_not_loaded.txt")
    assert text.startswith("NVIDIA-SMI has failed")
    with pytest.raises(ValueError, match="unexpected nvidia-smi output"):
        pc.parse_nvidia_smi_csv(text, pc.NVIDIA_QUERY_FIELDS)


def test_nvidia_smi_no_gpu_output_is_empty() -> None:
    assert pc.parse_nvidia_smi_csv("", pc.NVIDIA_QUERY_FIELDS) == []


def test_cuda_version_from_banner(fixture_text: Fixture) -> None:
    assert (
        pc.parse_nvidia_smi_cuda_version(
            fixture_text("common/nvidia_smi_banner_566.txt")
        )
        == "12.7"
    )
    failure = fixture_text("common/nvidia_smi_driver_not_loaded.txt")
    assert pc.parse_nvidia_smi_cuda_version(failure) is None


@pytest.mark.parametrize(
    ("text", "version"),
    [
        ("git version 2.43.0\n", "2.43.0"),
        ("git version 2.45.1.windows.1\r\n", "2.45.1.windows.1"),
        ("v22.22.1\n", "22.22.1"),
        ("uv 0.10.12 (x86_64-unknown-linux-gnu)\n", "0.10.12"),
        ("iperf 3.16 (cJSON 1.7.15)\nLinux host 6.8.0", "3.16"),
        ("gh version 2.45.0 (2024-03-04)\n", "2.45.0"),
        ("no version here", None),
    ],
)
def test_parse_version(text: str, version: str | None) -> None:
    assert pc.parse_version(text) == version


def test_git_status_counts_without_names() -> None:
    value = pc.parse_git_status(" M lab/inventory.py\n?? secret-notes.txt\nA  b.py\n")
    assert value == {"dirty": True, "modified_files": 2, "untracked_files": 1}
    assert pc.parse_git_status("") == {
        "dirty": False,
        "modified_files": 0,
        "untracked_files": 0,
    }
    only_untracked = pc.parse_git_status("?? x\n")
    assert only_untracked["dirty"] is False


def test_docker_version_ok(fixture_text: Fixture) -> None:
    value = pc.parse_docker_version(fixture_text("common/docker_version_ok.json"))
    assert value["client_version"] == value["server_version"] == "29.7.2"
    assert value["server_platform"] == "Docker Engine - Community"


def test_docker_version_daemon_down(fixture_text: Fixture) -> None:
    value = pc.parse_docker_version(
        fixture_text("common/docker_version_desktop_daemon_down.json")
    )
    assert value["client_version"] == "28.3.3"
    assert value["client_os"] == "windows"
    assert value["server_version"] is None


def test_docker_version_garbage_raises() -> None:
    with pytest.raises(ValueError):
        pc.parse_docker_version("error during connect: open //./pipe/docker_engine")


def test_docker_info_selected_fields_only(fixture_text: Fixture) -> None:
    value = pc.parse_docker_info(fixture_text("common/docker_info_desktop_nvidia.json"))
    assert value["operating_system"] == "Docker Desktop"
    assert value["runtimes"] == ["io.containerd.runc.v2", "nvidia", "runc"]
    assert value["nvidia_runtime"] is True
    assert value["server_errors"] == []
    text = str(value)
    for dropped in ("docker-desktop", "http.docker.internal", "6f1c2a9e"):
        assert dropped not in text  # host name, proxies, engine ID are not kept
