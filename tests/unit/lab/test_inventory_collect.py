"""lab/inventory.py (0.2.2) end to end in simulated machines, plus failure injection.

Every command goes through a fake ``run``/``which`` and Linux system files come from
a fake tree (``probes.FS_ROOT``), so these tests are hermetic and pass on Linux and
Windows alike. Simulated machines use recorded outputs (tests/unit/lab/fixtures):

* Laptop 1 today: Ubuntu 24.04, RTX 3050 with the driver blocked by Secure Boot;
* a Windows 11 laptop with an NVIDIA GPU, Wi-Fi + Ethernet, WSL, Docker Desktop off;
* a Windows 10 laptop with no NVIDIA GPU, no battery info, a failing CIM class.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar, Final

import pytest

import inventory
from _inventory import probes, runner
from _inventory.model import LAB_FIELDS, SECTIONS
from _inventory.runner import CmdResult
from _inventory.summary import render

FIXTURES: Final = Path(__file__).resolve().parent / "fixtures"
LAB_MD: Final = Path(__file__).resolve().parents[3] / "docs" / "LAB.md"
#: docs/LAB.md §1 rows that are human decisions or links, not machine facts.
NOT_DERIVABLE: Final = frozenset(
    {
        "Owner",
        "Role (proposed)",
        "IP reservation (Ethernet / Wi-Fi)",
        "Inventory JSON",
        "AI-agent access (SSH / Claude Code / human relay)",
    }
)
RTX3060_CSV: Final = (
    "0, NVIDIA GeForce RTX 3060 Laptop GPU, 6144, 566.36, 8.6, 00000000:01:00.0\r\n"
)
DOCKER_DOWN: Final = (
    'error during connect: Get "http://%2F%2F.%2Fpipe%2FdockerDesktopLinuxEngine/v1.51/version": '
    "open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified."
)

Responder = Callable[[str, list[str]], CmdResult]
Doc = dict[str, Any]


def fx(rel: str) -> str:
    return (FIXTURES / rel).read_text(encoding="utf-8")


def res(stdout: str = "", *, rc: int = 0, stderr: str = "") -> CmdResult:
    return CmdResult(("fake",), rc, stdout, stderr)


def missing(name: str, _args: list[str] | None = None) -> CmdResult:
    return CmdResult((name,), None, "", "", error=f"{name}: not found on PATH")


def install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    plat: str,
    responder: Responder,
    available: set[str],
    fs_root: Path,
) -> list[tuple[str, ...]]:
    """Simulate a machine; returns the list of commands the probes ran."""
    calls: list[tuple[str, ...]] = []

    def fake_run(name: str, args: list[str], **_: Any) -> CmdResult:
        tool = "python" if name == sys.executable else name
        calls.append((tool, *args))
        return responder(tool, list(args))

    monkeypatch.setattr(probes, "IS_LINUX", plat == "linux")
    monkeypatch.setattr(probes, "IS_WINDOWS", plat == "win32")
    monkeypatch.setattr(probes, "FS_ROOT", fs_root)
    monkeypatch.setattr(probes, "run", fake_run)
    monkeypatch.setattr(
        probes, "which", lambda n: f"/fake/bin/{n}" if n in available else None
    )
    monkeypatch.setattr(
        probes, "optional_module", lambda _name: None
    )  # plain `uv sync`
    monkeypatch.setattr(probes, "_windows_secure_boot_registry", lambda: None)
    return calls


def assert_valid(doc: Doc) -> None:
    """Schema: every section present, each an isolated probe result, JSON-serialisable."""
    assert doc["schema_version"] == 1
    assert sorted(doc["sections"]) == sorted(SECTIONS)  # written files sort keys
    for name, probe in doc["sections"].items():
        assert isinstance(probe["ok"], bool), name
        assert isinstance(probe["source"], str) and probe["source"], name
        if probe["ok"]:
            assert "value" in probe and "error" not in probe, name
        else:
            assert isinstance(probe["error"], str) and probe["error"], name
            assert "value" not in probe, name
    assert doc["failed_sections"] == sorted(
        n for n, p in doc["sections"].items() if not p["ok"]
    )
    problems = doc["problems"]
    assert problems == sorted(set(problems))
    assert all(isinstance(p, str) and p for p in problems)
    for name in doc["failed_sections"]:  # every failed section is also a problem
        assert any(p.startswith(f"{name}: probe failed") for p in problems), name
    json.dumps(doc, sort_keys=True, allow_nan=False)


def resolve(doc: Doc, path: str) -> Any:
    node: Any = doc
    for part in path.split("."):
        node = node[part]
    return node


def assert_lab_fields_present(doc: Doc) -> None:
    for row, paths in LAB_FIELDS.items():
        for path in paths:
            node = resolve(doc, path)
            assert node is not None, (row, path)
            if isinstance(node, dict) and "ok" in node:
                assert node["ok"], (row, path, node)


def git_answers(args: list[str]) -> CmdResult:
    if args[:2] == ["rev-parse", "--short"]:
        return res("4e44fc9\n")
    if args[:2] == ["rev-parse", "--abbrev-ref"]:
        return res("main\n")
    if args[:1] == ["status"]:
        return res(" M lab/inventory.py\n?? notes.txt\n")
    return res("git version 2.43.0\n")


# ------------------------------------------------------------------ fake machines


def make_linux_fs(root: Path) -> Path:
    files: dict[str, str] = {
        "etc/os-release": fx("linux/os-release_ubuntu2404.txt"),
        "proc/meminfo": fx("linux/meminfo.txt"),
        "proc/version": "Linux version 6.8.0-139-generic (buildd@lcy02) #139-Ubuntu SMP\n",
        "proc/sys/kernel/osrelease": "6.8.0-139-generic\n",
        "proc/modules": "snd_hda_intel 61440 3 - Live 0x0\nnvme 61440 2 - Live 0x0\n",
        "sys/devices/system/cpu/cpu0/cpufreq/base_frequency": "2300000\n",
        "sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq": "4700000\n",
        "sys/class/net/lo/type": "772\n",
        "sys/class/net/lo/operstate": "unknown\n",
        "sys/class/net/lo/address": "00:00:00:00:00:00\n",
        "sys/class/net/wlp0s20f3/type": "1\n",
        "sys/class/net/wlp0s20f3/operstate": "up\n",
        "sys/class/net/wlp0s20f3/address": "ac:19:8e:67:92:06\n",
        "sys/class/net/eno1/type": "1\n",
        "sys/class/net/eno1/operstate": "down\n",
        "sys/class/net/eno1/address": "e0:73:e7:d2:5a:54\n",
        "sys/class/net/eno1/speed": "-1\n",
        "sys/class/net/docker0/type": "1\n",
        "sys/class/net/docker0/operstate": "down\n",
        "sys/class/net/docker0/address": "66:04:cd:7d:75:b6\n",
        "sys/class/power_supply/ADP1/type": "Mains\n",
        "sys/class/power_supply/ADP1/online": "0\n",
        "sys/class/power_supply/BAT0/type": "Battery\n",
        "sys/class/power_supply/BAT0/status": "Discharging\n",
        "sys/class/power_supply/BAT0/capacity": "69\n",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for rel in (
        "sys/class/net/wlp0s20f3/wireless",
        "sys/class/net/wlp0s20f3/device",
        "sys/class/net/eno1/device",
    ):
        (root / rel).mkdir(parents=True, exist_ok=True)
    efivars = root / "sys/firmware/efi/efivars"
    efivars.mkdir(parents=True)
    (efivars / "SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c").write_bytes(
        bytes([6, 0, 0, 0, 1])
    )
    return root


LINUX_TOOLS: Final = {
    "git",
    "lscpu",
    "lspci",
    "ip",
    "nvidia-smi",
    "mokutil",
    "uv",
    "docker",
    "node",
}


def laptop1(name: str, args: list[str]) -> CmdResult:
    """Laptop 1 today: NVIDIA driver not loading (Secure Boot), no torch."""
    if name == "git":
        return git_answers(args)
    if name == "lscpu":
        return res(fx("linux/lscpu_i7_12650h.txt"))
    if name == "lspci":
        return res(fx("linux/lspci_mm_hybrid.txt"))
    if name == "ip":
        return res(fx("linux/ip_j_4_addr.json"))
    if name == "nvidia-smi":
        return res(fx("common/nvidia_smi_driver_not_loaded.txt"), rc=9)
    if name == "mokutil":
        return res("SecureBoot enabled\n")
    if name == "python":
        if "torch" in args[1]:
            return res('{"installed": false}\n')
        return res(
            '{"installed": true, "error": "NVMLError_DriverNotLoaded: Driver Not Loaded"}\n'
        )
    if name == "uv":
        return res("uv 0.10.12 (x86_64-unknown-linux-gnu)\n")
    if name == "docker":
        if args[0] == "version":
            return res(fx("common/docker_version_ok.json"))
        if args[0] == "info":
            return res(fx("common/docker_info_desktop_nvidia.json"))
        return res("Docker version 29.7.2, build a7dcaa6\n")
    if name == "node":
        return res("v22.22.1\n")
    return missing(name)


def windows_machine(bundle: str, *, nvidia: bool) -> Responder:
    def respond(name: str, args: list[str]) -> CmdResult:
        if name == "powershell":
            return res(fx(bundle))
        if name == "nvidia-smi" and nvidia:
            if args and args[0].startswith("--query-gpu"):
                return res(RTX3060_CSV)
            return res(fx("common/nvidia_smi_banner_566.txt"))
        if name == "wsl":
            return res(
                "  NAME            STATE           VERSION\r\n"
                "* Ubuntu-24.04    Running         2\r\n"
            )
        if name == "python":
            if "torch" in args[1] and nvidia:
                torch = {
                    "installed": True,
                    "version": "2.8.0+cu128",
                    "cuda_build": "12.8",
                    "cuda_available": True,
                    "devices": [
                        {
                            "name": "NVIDIA GeForce RTX 3060 Laptop GPU",
                            "capability": "8.6",
                            "total_mib": 6144,
                        }
                    ],
                }
                return res(json.dumps(torch) + "\r\n")
            return res('{"installed": false}\r\n')
        if name == "docker":
            if args[0] == "version":
                return res(
                    fx("common/docker_version_desktop_daemon_down.json"),
                    rc=1,
                    stderr=DOCKER_DOWN,
                )
            return res("", rc=1, stderr=DOCKER_DOWN)
        if name == "uv":
            return res("uv 0.10.12 (x86_64-pc-windows-msvc)\r\n")
        if name == "git":
            return git_answers(args)
        return missing(name)

    return respond


# ------------------------------------------------------------------ failure injection


@pytest.mark.parametrize("plat", ["linux", "win32", "darwin"])
def test_nothing_available_never_crashes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, plat: str
) -> None:
    install(
        monkeypatch,
        plat=plat,
        responder=missing,
        available=set(),
        fs_root=tmp_path / "empty",
    )
    doc = probes.collect(tmp_path)
    assert_valid(doc)
    s = doc["sections"]
    assert s["python"]["ok"] and s["disks"]["ok"]  # stdlib-only sections always work
    nv = s["nvidia"]["value"]
    assert nv["driver_working"] is False
    assert nv["nvidia_smi"] == {
        "ok": False,
        "available": False,
        "error": "nvidia-smi: not found on PATH",
    }
    assert s["docker"]["value"] == {"installed": False, "reachable": False}
    assert {"git", "uv", "torch", "memory"} <= set(doc["failed_sections"])
    assert "failed probes:" in render(doc)


@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError(2, "No such file or directory"),
        PermissionError(13, "Permission denied"),
        subprocess.TimeoutExpired(cmd="tool", timeout=15),
        OSError(8, "Exec format error"),
    ],
    ids=["missing", "denied", "timeout", "oserror"],
)
def test_subprocess_exceptions_are_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exc: BaseException
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise exc

    monkeypatch.setattr(subprocess, "Popen", FakePopen.raising(exc))
    monkeypatch.setattr(runner, "which", lambda n: f"/fake/bin/{n}")
    monkeypatch.setattr(probes, "which", lambda n: f"/fake/bin/{n}")
    monkeypatch.setattr(probes, "FS_ROOT", tmp_path / "empty")
    monkeypatch.setattr(probes, "optional_module", lambda _name: None)
    monkeypatch.setattr(probes, "_windows_secure_boot_registry", lambda: None)
    doc = probes.collect(tmp_path)
    assert_valid(doc)
    uv = doc["sections"]["uv"]
    assert uv["ok"] is False
    assert "timed out after 15 s" in uv["error"] or "could not run" in uv["error"]


def test_unexpected_probe_exception_is_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install(
        monkeypatch, plat="linux", responder=missing, available=set(), fs_root=tmp_path
    )

    def boom() -> Any:
        raise ZeroDivisionError("boom")

    monkeypatch.setattr(probes, "probe_cpu", boom)
    doc = probes.collect(tmp_path)
    assert_valid(doc)
    assert doc["sections"]["cpu"] == {
        "ok": False,
        "source": "cpu-probe",
        "error": "ZeroDivisionError: boom",
    }


def test_runner_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "which", lambda _n: None)
    result = runner.run("nope", ["--version"])
    assert not result.ok
    assert result.describe_failure() == "nope: not found on PATH"


class FakePopen:
    """Stand-in for ``subprocess.Popen``; records how the runner spawned it."""

    seen: ClassVar[dict[str, Any]] = {}
    returncode: int | None = None
    pid = 424242
    stdout = None
    stderr = None

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        FakePopen.seen = {**kwargs, "argv": argv}
        self.communicated: list[float | None] = []

    @staticmethod
    def raising(exc: BaseException) -> Callable[..., Any]:
        def boom(*_a: Any, **_k: Any) -> Any:
            raise exc

        return boom

    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
        FakePopen.seen.setdefault("timeouts", []).append(timeout)
        self.returncode = 3
        return b"", "bad thing ü".encode()

    def kill(self) -> None:
        FakePopen.seen["killed"] = True

    def wait(self, timeout: float | None = None) -> int:
        return -9


class HangingPopen(FakePopen):
    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
        FakePopen.seen.setdefault("timeouts", []).append(timeout)
        raise subprocess.TimeoutExpired(cmd="x", timeout=timeout or 0)


def test_runner_uses_argv_list_no_shell_c_locale_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "which", lambda n: f"/fake dir/{n}")
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    result = runner.run("tool", ["a b", "c"], timeout=7)
    seen = FakePopen.seen
    assert seen["argv"] == ["/fake dir/tool", "a b", "c"]
    assert not seen.get("shell", False)
    assert seen["timeouts"] == [7]
    assert seen["env"]["LC_ALL"] == "C"
    assert seen["stdin"] == subprocess.DEVNULL
    if sys.platform == "win32":
        assert seen["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert seen["start_new_session"] is True  # own process group -> killpg works
    assert (result.ok, result.returncode) == (False, 3)
    assert result.describe_failure() == "bad thing ü"


def test_runner_timeout_message_kills_and_remembers_hung_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed_groups: list[int] = []
    monkeypatch.setattr(runner, "which", lambda n: f"/fake/{n}")
    monkeypatch.setattr(subprocess, "Popen", HangingPopen)
    monkeypatch.setattr(runner, "_kill_tree", lambda p: killed_groups.append(p.pid))
    assert runner.run("nvidia-smi", []).error == "nvidia-smi: timed out after 15 s"
    assert killed_groups == [HangingPopen.pid]
    again = runner.run("nvidia-smi", ["--query"])
    assert again.error == f"nvidia-smi: {runner.SKIPPED_HUNG}"
    assert killed_groups == [HangingPopen.pid]  # not started a second time


def test_runner_python_helpers_are_not_remembered_as_hung(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "Popen", HangingPopen)
    monkeypatch.setattr(runner, "_kill_tree", lambda _p: None)
    for _ in range(2):
        result = runner.run(sys.executable, ["-c", "x"], remember_hang=False)
        assert result.error is not None and "timed out" in result.error


def test_runner_deadline_clips_then_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(runner, "_clock", lambda: clock[0])
    monkeypatch.setattr(runner, "which", lambda n: f"/fake/{n}")
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    runner.start_budget(60)  # reserve 9 s -> commands get 51 s in total
    assert runner.remaining() == pytest.approx(51.0)
    runner.run("a", [], timeout=15)
    clock[0] += 45.0  # 6 s left: a 15 s cap is cut to the time left
    runner.run("b", [], timeout=15)
    assert FakePopen.seen["timeouts"] == [pytest.approx(6.0)]
    monkeypatch.setattr(subprocess, "Popen", HangingPopen)
    monkeypatch.setattr(runner, "_kill_tree", lambda _p: None)
    timed_out = runner.run("c", [], timeout=15)
    assert timed_out.error == "c: timed out after 6 s (run deadline reached)"
    clock[0] += 6.0
    skipped = runner.run("d", [])
    assert skipped.error == f"d: {runner.SKIPPED_DEADLINE}"
    assert "skipped: run deadline exceeded" in skipped.describe_failure()


def test_runner_without_budget_has_no_deadline() -> None:
    assert runner.remaining() is None


def test_runner_decode_bom_and_utf16() -> None:
    assert runner._decode("﻿hello".encode(), "utf-8") == "hello"
    assert runner._decode("hi there".encode("utf-16-le"), "utf-8") == "hi there"
    assert runner._decode(b"\xff\xfe\xfa", "utf-8")  # invalid bytes never raise


# ------------------------------------------------------------------ simulated machines


def test_laptop1_driver_blocked_by_secure_boot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fs = make_linux_fs(tmp_path / "root")
    install(
        monkeypatch, plat="linux", responder=laptop1, available=LINUX_TOOLS, fs_root=fs
    )
    doc = probes.collect(tmp_path)
    assert_valid(doc)
    assert doc["failed_sections"] == []
    assert_lab_fields_present(doc)
    s = {k: v["value"] for k, v in doc["sections"].items()}
    assert s["os"]["name"].startswith("Ubuntu 24.04")
    assert s["wsl"]["is_wsl"] is False
    cpu = s["cpu"]
    assert (cpu["physical_cores"], cpu["logical_threads"]) == (10, 16)
    assert (cpu["base_mhz"], cpu["max_mhz"]) == (2300.0, 4700.0)
    assert [(g["vendor"], g["source"]) for g in s["gpus"]] == [
        ("Intel", "lspci"),
        ("NVIDIA", "lspci"),
    ]
    nv = s["nvidia"]
    assert nv["driver_working"] is False
    assert nv["nvidia_smi"]["exit_code"] == 9
    assert nv["nvidia_smi"]["error"].startswith("NVIDIA-SMI has failed")
    assert "DriverNotLoaded" in nv["nvml"]["error"]
    assert nv["linux"]["kernel_module_loaded"] is False
    assert s["secure_boot"]["state"] == "enabled"
    adapters = {a["name"]: a for a in s["network"]["adapters"]}
    wifi, eth = adapters["wlp0s20f3"], adapters["eno1"]
    assert (wifi["kind"], wifi["up"], wifi["ipv4"]) == (
        "wifi",
        True,
        ["192.168.31.13/24"],
    )
    assert (eth["kind"], eth["up"], eth["link_speed_mbit"]) == ("ethernet", False, None)
    assert eth["mac"] == "e0:73:e7:d2:5a:54"
    assert adapters["docker0"]["kind"] == "virtual"
    assert adapters["lo"]["kind"] == "loopback"
    assert s["power"]["on_ac"] is False
    assert s["git"] == {
        "commit": "4e44fc9",
        "branch": "main",
        "dirty": True,
        "modified_files": 1,
        "untracked_files": 1,
    }
    assert s["docker"]["reachable"] is True
    assert s["docker"]["info"]["nvidia_runtime"] is True
    assert s["torch"] == {"installed": False}
    assert s["tools"]["node"] == {"available": True, "version": "22.22.1"}
    assert doc["problems"] == [
        "nvidia: NVIDIA GPU present but the driver is not working "
        "(NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA "
        "driver. Make sure that the latest NVIDIA driver is installed and running.)"
    ]
    summary = render(doc)
    assert "nvidia-smi FAILED: NVIDIA-SMI has failed" in summary
    assert "failed probes: none" in summary
    assert "problems     : 1 (need a human)" in summary
    assert "  - nvidia: NVIDIA GPU present but the driver is not working" in summary


def test_windows_nvidia_laptop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = install(
        monkeypatch,
        plat="win32",
        responder=windows_machine("windows/cim_bundle_nvidia_laptop.json", nvidia=True),
        available={"powershell", "nvidia-smi", "wsl", "uv", "git", "docker"},
        fs_root=tmp_path / "no-linux-fs",
    )
    doc = probes.collect(tmp_path)
    assert_valid(doc)
    assert doc["failed_sections"] == []
    assert_lab_fields_present(doc)
    powershell_calls = [c for c in calls if c[0] == "powershell"]
    assert len(powershell_calls) == 1  # one cached CIM bundle for all sections
    assert "-EncodedCommand" in powershell_calls[0]
    s = {k: v["value"] for k, v in doc["sections"].items()}
    assert (s["os"]["name"], s["os"]["build"]) == ("Microsoft Windows 11 Home", "22631")
    assert (s["cpu"]["physical_cores"], s["cpu"]["base_mhz"]) == (8, 3201)
    assert s["memory"]["total_bytes"] == 16541507584
    assert [(g["vendor"], g["vram_mib"], g["source"]) for g in s["gpus"]] == [
        ("AMD", 512, "powershell"),
        ("NVIDIA", 6144, "nvidia-smi"),  # replaces the 4 GiB-capped CIM row
    ]
    nvidia_gpu = s["gpus"][1]
    assert (nvidia_gpu["compute_capability"], nvidia_gpu["cuda_driver_version"]) == (
        "8.6",
        "12.7",
    )
    nv = s["nvidia"]
    assert (nv["driver_working"], nv["driver_version"], nv["cuda_driver_version"]) == (
        True,
        "566.36",
        "12.7",
    )
    assert s["secure_boot"]["state"] == "unknown"
    assert s["wsl"]["distros"][0]["name"] == "Ubuntu-24.04"
    adapters = {a["name"]: a for a in s["network"]["adapters"]}
    assert adapters["Wi-Fi"]["link_speed_mbit"] == 866.7
    assert adapters["Ethernet"]["link_speed_mbit"] == 1000.0
    assert "49.36.10.77" not in json.dumps(doc)
    assert s["power"]["on_ac"] is True
    assert s["torch"]["cuda_available"] is True
    docker = s["docker"]
    assert (docker["installed"], docker["reachable"], docker["client_version"]) == (
        True,
        False,
        "28.3.3",
    )
    assert "error during connect" in docker["error"]
    assert len(doc["problems"]) == 1
    assert doc["problems"][0].startswith(
        "docker: installed but the daemon is not reachable (error during connect"
    )


def test_windows_without_nvidia_and_failing_cim_class(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install(
        monkeypatch,
        plat="win32",
        responder=windows_machine(
            "windows/cim_bundle_no_nvidia_ps51.json", nvidia=False
        ),
        available={"powershell", "uv", "git"},
        fs_root=tmp_path / "no-linux-fs",
    )
    doc = probes.collect(tmp_path)
    assert_valid(doc)
    assert doc["failed_sections"] == ["network"]  # net_ip class failed, psutil absent
    assert "MSFT_NetIPAddress" in doc["sections"]["network"]["error"]
    s = {k: v.get("value") for k, v in doc["sections"].items()}
    assert s["os"]["name"] == "Microsoft Windows 10 Pro"
    assert (s["cpu"]["model"], s["cpu"]["physical_cores"]) == (
        "Intel(R) Core(TM) i5-8250U CPU @ 1.60GHz",
        4,
    )
    assert [g["vendor"] for g in s["gpus"]] == ["Intel"]
    assert s["nvidia"]["driver_working"] is False
    assert s["nvidia"]["nvidia_smi"]["available"] is False
    assert s["secure_boot"]["state"] == "unsupported"
    assert s["power"] == {"on_ac": None, "has_battery": False, "batteries": []}
    assert s["docker"] == {"installed": False, "reachable": False}
    assert s["torch"] == {"installed": False}
    assert "network      : ERROR" in render(doc)


def test_powershell_unavailable_is_recorded_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = install(
        monkeypatch,
        plat="win32",
        responder=missing,
        available={"powershell"},
        fs_root=tmp_path,
    )
    doc = probes.collect(tmp_path)
    assert_valid(doc)
    for name in ("os", "cpu", "memory", "gpus", "network", "power", "secure_boot"):
        assert "powershell" in doc["sections"][name]["error"], name
    assert len([c for c in calls if c[0] == "powershell"]) == 1


def test_old_driver_without_compute_cap_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def respond(name: str, args: list[str]) -> CmdResult:
        if name == "nvidia-smi" and args and "compute_cap" in args[0]:
            return res('Field "compute_cap" is not a valid field to query.\n\n', rc=2)
        if name == "nvidia-smi" and args:
            return res("0, GeForce GTX 1050, 4096, 456.71, 00000000:01:00.0\n")
        if name == "nvidia-smi":
            return res(
                "| NVIDIA-SMI 456.71  Driver Version: 456.71  CUDA Version: 11.1 |\n"
            )
        return laptop1(name, args)

    install(
        monkeypatch,
        plat="linux",
        responder=respond,
        available=LINUX_TOOLS,
        fs_root=make_linux_fs(tmp_path / "root"),
    )
    doc = probes.collect(tmp_path)
    assert_valid(doc)
    gpus = doc["sections"]["gpus"]["value"]
    nvidia = [g for g in gpus if g["vendor"] == "NVIDIA"]
    assert len(nvidia) == 1
    assert nvidia[0]["source"] == "nvidia-smi"
    assert nvidia[0]["compute_capability"] is None
    assert nvidia[0]["cuda_driver_version"] == "11.1"
    assert doc["sections"]["nvidia"]["value"]["driver_working"] is True


def test_nvidia_smi_timeout_is_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def respond(name: str, args: list[str]) -> CmdResult:
        if name == "nvidia-smi":
            return CmdResult(
                (name,), None, "", "", error="nvidia-smi: timed out after 15 s"
            )
        return laptop1(name, args)

    install(
        monkeypatch,
        plat="linux",
        responder=respond,
        available=LINUX_TOOLS,
        fs_root=make_linux_fs(tmp_path / "root"),
    )
    doc = probes.collect(tmp_path)
    assert_valid(doc)
    smi = doc["sections"]["nvidia"]["value"]["nvidia_smi"]
    assert smi["ok"] is False
    assert "timed out" in smi["error"]


@pytest.mark.parametrize(
    ("result", "needle"),
    [
        (
            CmdResult(("python",), 0, "Segmentation fault\n", ""),
            "unparseable torch helper output",
        ),
        (
            CmdResult(("python",), -11, "", "Fatal Python error: Segmentation fault"),
            "Fatal Python error",
        ),
        (
            CmdResult(("python",), None, "", "", error="python: timed out after 120 s"),
            "timed out",
        ),
    ],
    ids=["garbage", "crash", "timeout"],
)
def test_broken_torch_is_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, result: CmdResult, needle: str
) -> None:
    def respond(name: str, args: list[str]) -> CmdResult:
        if name == "python" and "torch" in args[1]:
            return result
        return laptop1(name, args)

    install(
        monkeypatch,
        plat="linux",
        responder=respond,
        available=LINUX_TOOLS,
        fs_root=make_linux_fs(tmp_path / "root"),
    )
    doc = probes.collect(tmp_path)
    assert_valid(doc)
    assert doc["failed_sections"] == ["torch"]
    assert needle in doc["sections"]["torch"]["error"]


# ------------------------------------------------------------------ LAB.md sync


def lab_md_rows() -> set[str]:
    text = LAB_MD.read_text(encoding="utf-8")
    table = text.split("## 1. Machines", 1)[1].split("### How inventory feeds", 1)[0]
    rows = set()
    for line in table.splitlines():
        if line.startswith("| ") and not line.startswith(("| Field", "|---")):
            rows.add(line.split("|")[1].strip().replace("`", ""))
    return rows


def test_lab_fields_cover_every_derivable_lab_md_row() -> None:
    rows = lab_md_rows()
    assert rows >= NOT_DERIVABLE
    assert set(LAB_FIELDS) == rows - NOT_DERIVABLE


# ------------------------------------------------------------------ CLI


@pytest.fixture
def bare_machine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install(
        monkeypatch,
        plat="linux",
        responder=missing,
        available=set(),
        fs_root=tmp_path / "empty",
    )


@pytest.mark.usefixtures("bare_machine")
def test_cli_writes_json_to_new_dirs_with_spaces_and_unicode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "FINAL YEAR" / "réseau ü" / "host name.json"
    assert inventory.main(["--out", str(out)]) == 0
    raw = out.read_bytes()
    assert raw.endswith(b"\n") and b"\r\n" not in raw
    assert_valid(json.loads(raw.decode("utf-8")))
    captured = capsys.readouterr()
    assert captured.out == ""
    for label in (
        "hostname",
        "cpu",
        "ram",
        "gpus",
        "nvidia",
        "torch",
        "docker",
        "written to",
    ):
        assert label in captured.err


@pytest.mark.usefixtures("bare_machine")
def test_cli_stdout_pretty_sorted(capsys: pytest.CaptureFixture[str]) -> None:
    assert inventory.main(["--pretty", "--no-summary"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    doc = json.loads(captured.out)
    assert (
        captured.out
        == json.dumps(doc, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    )


@pytest.mark.usefixtures("bare_machine")
def test_cli_unwritable_output_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    assert inventory.main(["--out", str(blocker / "inv.json"), "--no-summary"]) == 1
    assert "cannot write" in capsys.readouterr().err


def test_cli_usage_error_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        inventory.main(["--no-such-flag"])
    assert info.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("value", ["abc", "0", "4.9", "-10", "3601", "nan"])
def test_cli_bad_timeout_exits_2(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as info:
        inventory.main(["--timeout", value])
    assert info.value.code == 2
    assert "--timeout" in capsys.readouterr().err


@pytest.mark.usefixtures("bare_machine")
def test_cli_timeout_starts_the_run_budget(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    budgets: list[float] = []
    monkeypatch.setattr(runner, "start_budget", budgets.append)
    assert inventory.main(["--no-summary"]) == 0
    assert inventory.main(["--no-summary", "--timeout", "90"]) == 0
    assert budgets == [runner.DEFAULT_TOTAL_S, 90.0] == [60.0, 90.0]
    capsys.readouterr()


def test_build_document_sanitises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = {
        "sections": {},
        "failed_sections": [],
        "exe": str(Path.home() / "My Stuff" / "python"),
        "ip": "49.36.10.77",
        "note": "retry with token=abc123",
    }
    monkeypatch.setattr(inventory, "collect", lambda _repo: fake)
    doc = inventory.build_document()
    assert doc["exe"] == os.path.join("~", "My Stuff", "python")
    assert doc["ip"] == "<public-ip>"
    assert "abc123" not in doc["note"]
    assert doc["redaction"] == "meshforge.common.redaction"


def test_dumps_is_deterministic_utf8() -> None:
    assert inventory.dumps({"b": 1, "a": "ü"}, pretty=False) == '{"a": "ü", "b": 1}\n'
