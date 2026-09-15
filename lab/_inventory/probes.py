"""Collectors: one isolated probe per inventory section.

Every public ``probe_*`` returns a :data:`~_inventory.model.Probe`. :func:`collect`
wraps each one so an unexpected exception becomes a failed probe instead of a
crash (failures are data). Optional libraries (psutil, pynvml, torch) are used when
importable; torch and NVML run in a child Python process so a hang or native
crash in a GPU stack cannot take the inventory down.
"""

from __future__ import annotations

import importlib
import json
import os
import platform
import shutil
import socket
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any, Final

from _inventory import parse_common as pc
from _inventory import parse_linux as pl
from _inventory import parse_windows as pw
from _inventory.model import JSON, SCHEMA_VERSION, SECTIONS, Probe, fail, ok
from _inventory.problems import find_problems
from _inventory.runner import CmdResult, run, which

IS_WINDOWS: Final = sys.platform == "win32"
IS_LINUX: Final = sys.platform.startswith("linux")
#: Per-command caps; the run budget (runner.start_budget, ``--timeout``) may cut them.
TORCH_TIMEOUT_S: Final = 120.0
NVML_TIMEOUT_S: Final = 30.0
DOCKER_TIMEOUT_S: Final = 20.0
POWERSHELL_TIMEOUT_S: Final = 60.0

#: Root for Linux system files (/proc, /sys, /etc). Tests point it at a fake tree so a
#: whole Linux machine can be simulated on any OS.
FS_ROOT: Path = Path("/")

#: Child-process script for torch (import can take seconds and can crash natively).
TORCH_SCRIPT: Final = """
import json, importlib.util
out = {"installed": importlib.util.find_spec("torch") is not None}
if out["installed"]:
    import torch
    out["version"] = torch.__version__
    out["cuda_build"] = torch.version.cuda
    try:
        out["cuda_available"] = bool(torch.cuda.is_available())
        n = torch.cuda.device_count() if out["cuda_available"] else 0
        out["devices"] = [
            {"name": torch.cuda.get_device_name(i),
             "capability": ".".join(map(str, torch.cuda.get_device_capability(i))),
             "total_mib": torch.cuda.get_device_properties(i).total_memory // 2**20}
            for i in range(n)]
    except Exception as exc:
        out["cuda_available"] = False
        out["cuda_error"] = f"{type(exc).__name__}: {exc}"
print(json.dumps(out))
"""

#: Child-process script for NVML (nvidia-ml-py) — same isolation reasons.
NVML_SCRIPT: Final = """
import json, importlib.util
out = {"installed": importlib.util.find_spec("pynvml") is not None}
if out["installed"]:
    import pynvml as n
    def s(v): return v.decode() if isinstance(v, bytes) else str(v)
    try:
        n.nvmlInit()
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    else:
        try:
            out["driver_version"] = s(n.nvmlSystemGetDriverVersion())
            c = n.nvmlSystemGetCudaDriverVersion()
            out["cuda_driver_version"] = f"{c // 1000}.{(c % 1000) // 10}"
            gpus = []
            for i in range(n.nvmlDeviceGetCount()):
                h = n.nvmlDeviceGetHandleByIndex(i)
                cc = n.nvmlDeviceGetCudaComputeCapability(h)
                gpus.append({"index": i, "name": s(n.nvmlDeviceGetName(h)),
                             "vram_mib": n.nvmlDeviceGetMemoryInfo(h).total // 2**20,
                             "compute_capability": f"{cc[0]}.{cc[1]}",
                             "pci_bus_id": s(n.nvmlDeviceGetPciInfo(h).busId)})
            out["gpus"] = gpus
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            n.nvmlShutdown()
print(json.dumps(out))
"""


def optional_module(name: str) -> Any | None:
    """Import an optional library, or None when it is not installed."""
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _fs(rel: str) -> Path:
    """A Linux system path below :data:`FS_ROOT` (``_fs("proc/meminfo")``)."""
    return FS_ROOT / rel


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _cmd_fail(source: str, result: CmdResult) -> Probe:
    return fail(source, result.describe_failure())


def _tool_version(name: str, args: list[str] | None = None) -> dict[str, JSON]:
    path = which(name)
    if path is None:
        return {"available": False, "version": None, "error": "not found on PATH"}
    result = run(name, args or ["--version"])
    if not result.ok:
        return {"available": True, "version": None, "error": result.describe_failure()}
    return {
        "available": True,
        "version": pc.parse_version(result.stdout or result.stderr),
    }


# --------------------------------------------------------------------- Windows CIM


@cache
def _cim_bundle() -> tuple[dict[str, dict[str, JSON]] | None, str]:
    """Run PowerShell once; the bundle, or the error text (a failure is cached too,
    so a missing or hanging PowerShell costs one timeout, not one per section)."""
    result = run(
        "powershell",
        [
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            pw.encoded_command(pw.CIM_SCRIPT),
        ],
        timeout=POWERSHELL_TIMEOUT_S,
    )
    if not result.ok:
        return None, f"powershell CIM query failed: {result.describe_failure()}"
    try:
        return pw.parse_cim_bundle(result.stdout), ""
    except (ValueError, TypeError) as exc:
        return None, f"powershell CIM output unparseable: {exc}"


def windows_cim() -> dict[str, dict[str, JSON]]:
    """The one PowerShell CIM bundle (raises RuntimeError when it failed)."""
    bundle, error = _cim_bundle()
    if bundle is None:
        raise RuntimeError(error)
    return bundle


def _cim(key: str) -> list[JSON]:
    entry = windows_cim().get(key)
    if entry is None:
        raise RuntimeError(f"{key}: missing from PowerShell output")
    if not entry["ok"]:
        raise RuntimeError(f"{key}: {entry['error']}")
    data: list[JSON] = entry["data"]
    return data


# --------------------------------------------------------------------- probes


def probe_git(repo: Path) -> Probe:
    head = run("git", ["rev-parse", "--short", "HEAD"], cwd=repo)
    if not head.ok:
        return _cmd_fail("git", head)
    status = run("git", ["status", "--porcelain"], cwd=repo)
    value: dict[str, JSON] = {"commit": head.stdout.strip()}
    if status.ok:
        value.update(pc.parse_git_status(status.stdout))
    else:
        value.update(dirty=None, status_error=status.describe_failure())
    branch = run("git", ["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    value["branch"] = branch.stdout.strip() if branch.ok else None
    return ok("git", value)


def probe_os() -> Probe:
    base: dict[str, JSON] = {
        "system": platform.system(),
        "release": platform.release(),
        "kernel": platform.version() if IS_WINDOWS else platform.release(),
        "machine": platform.machine(),
    }
    if IS_LINUX:
        rel = _read(_fs("etc/os-release"))
        if rel is None:
            return ok(
                "platform", {**base, "name": platform.platform(), "version": None}
            )
        info = pl.parse_os_release(rel)
        return ok(
            "os-release",
            {
                **base,
                "name": info.get("PRETTY_NAME") or info.get("NAME"),
                "id": info.get("ID"),
                "version": info.get("VERSION_ID"),
                "build": None,
            },
        )
    if IS_WINDOWS:
        return ok("powershell", {**base, **pw.parse_os(_cim("operating_system"))})
    return ok(
        "platform", {**base, "name": platform.platform(), "version": platform.version()}
    )


def probe_wsl() -> Probe:
    if IS_LINUX:
        value = pl.detect_wsl(
            _read(_fs("proc/version")) or "",
            _read(_fs("proc/sys/kernel/osrelease")) or "",
            os.environ.get("WSL_DISTRO_NAME"),
        )
        return ok("proc", value)
    if IS_WINDOWS:
        if which("wsl") is None:
            return ok(
                "wsl.exe", {"is_wsl": False, "wsl_installed": False, "distros": []}
            )
        result = run("wsl", ["-l", "-v"], encoding="utf-16-le")
        if not result.ok:
            # Exit code != 0 also means "WSL installed but no distributions".
            return ok(
                "wsl.exe",
                {
                    "is_wsl": False,
                    "wsl_installed": True,
                    "distros": [],
                    "detail": result.describe_failure(),
                },
            )
        distros = pw.parse_wsl_list(result.stdout)
        return ok(
            "wsl.exe", {"is_wsl": False, "wsl_installed": True, "distros": distros}
        )
    return ok("platform", {"is_wsl": False})


def probe_cpu() -> Probe:
    psutil = optional_module("psutil")
    if IS_WINDOWS:
        value = pw.parse_processors(_cim("processor"))
        source = "powershell"
    elif IS_LINUX:
        lscpu = run("lscpu", [])
        if lscpu.ok:
            value, source = pl.parse_lscpu(lscpu.stdout), "lscpu"
        else:
            text = _read(_fs("proc/cpuinfo"))
            if text is None:
                return _cmd_fail("lscpu", lscpu)
            value, source = pl.parse_cpuinfo(text), "proc"
        cpufreq = _fs("sys/devices/system/cpu/cpu0/cpufreq")
        value["base_mhz"] = pl.parse_khz_file(_read(cpufreq / "base_frequency") or "")
        if not value.get("max_mhz"):
            value["max_mhz"] = pl.parse_khz_file(
                _read(cpufreq / "cpuinfo_max_freq") or ""
            )
    else:
        value, source = {"model": platform.processor() or None}, "platform"
    if psutil is not None and not value.get("physical_cores"):
        value["physical_cores"] = psutil.cpu_count(logical=False)
    if not value.get("logical_threads"):
        value["logical_threads"] = os.cpu_count()
    return ok(source, value)


def probe_memory() -> Probe:
    psutil = optional_module("psutil")
    if psutil is not None:
        vm = psutil.virtual_memory()
        return ok(
            "psutil",
            {"total_bytes": int(vm.total), "available_bytes": int(vm.available)},
        )
    if IS_LINUX:
        text = _read(_fs("proc/meminfo"))
        if text is None:
            return fail("proc", "/proc/meminfo unreadable")
        return ok("proc", pl.parse_meminfo(text))
    if IS_WINDOWS:
        return ok(
            "powershell",
            pw.parse_memory(_cim("operating_system"), _cim("computer_system")),
        )
    return fail("none", "no memory source on this platform without psutil")


def probe_disks(repo: Path) -> Probe:
    usage = shutil.disk_usage(repo)
    value: dict[str, JSON] = {
        "repo_drive": {
            "path": str(repo.anchor if IS_WINDOWS else repo),
            "total_bytes": usage.total,
            "free_bytes": usage.free,
        },
        "partitions": None,
    }
    psutil = optional_module("psutil")
    source = "shutil"
    if psutil is not None:
        parts: list[dict[str, JSON]] = []
        for p in psutil.disk_partitions(all=False):
            if "loop" in p.device or p.fstype in {"squashfs", "overlay", "tmpfs"}:
                continue
            try:
                u = psutil.disk_usage(p.mountpoint)
            except OSError:
                continue
            parts.append(
                {
                    "mountpoint": p.mountpoint,
                    "fstype": p.fstype,
                    "total_bytes": int(u.total),
                    "free_bytes": int(u.free),
                }
            )
        value["partitions"] = sorted(parts, key=lambda d: str(d["mountpoint"]))
        source = "shutil+psutil"
    return ok(source, value)


def _nvidia_smi() -> dict[str, JSON]:
    """Run nvidia-smi once for the GPU list and once for the CUDA banner."""
    if which("nvidia-smi") is None:
        return {
            "ok": False,
            "available": False,
            "error": "nvidia-smi: not found on PATH",
        }
    for fields in (pc.NVIDIA_QUERY_FIELDS, pc.NVIDIA_QUERY_FIELDS_LEGACY):
        result = run(
            "nvidia-smi",
            [f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits"],
        )
        if result.ok:
            try:
                gpus = pc.parse_nvidia_smi_csv(result.stdout, fields)
            except ValueError as exc:
                return {"ok": False, "available": True, "error": str(exc)}
            banner = run("nvidia-smi", [])
            cuda = (
                pc.parse_nvidia_smi_cuda_version(banner.stdout) if banner.ok else None
            )
            return {
                "ok": True,
                "available": True,
                "gpus": gpus,
                "cuda_driver_version": cuda,
            }
        text = result.describe_failure()
        if "field" not in text.lower() or "compute_cap" not in fields:
            # Driver failure (e.g. Secure Boot blocked module): same text either way.
            return {
                "ok": False,
                "available": True,
                "exit_code": result.returncode,
                "error": text,
            }
    return {"ok": False, "available": True, "error": "nvidia-smi query failed"}


def _nvml() -> dict[str, JSON]:
    result = run(
        sys.executable, ["-c", NVML_SCRIPT], timeout=NVML_TIMEOUT_S, remember_hang=False
    )
    if not result.ok:
        return {"ok": False, "error": result.describe_failure()}
    try:
        data = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {
            "ok": False,
            "error": f"unparseable NVML helper output: {result.stdout[:200]}",
        }
    if not data.get("installed"):
        return {
            "ok": False,
            "installed": False,
            "error": "nvidia-ml-py (pynvml) not installed",
        }
    if "error" in data:
        return {"ok": False, "installed": True, "error": data["error"]}
    return {"ok": True, "installed": True, **data}


def _linux_nvidia_module() -> dict[str, JSON]:
    modules = _read(_fs("proc/modules")) or ""
    loaded = any(line.split(" ", 1)[0] == "nvidia" for line in modules.splitlines())
    version = _read(_fs("proc/driver/nvidia/version"))
    return {
        "kernel_module_loaded": loaded,
        "proc_driver_version": version.strip().splitlines()[0] if version else None,
    }


@cache
def nvidia_status() -> dict[str, JSON]:
    """nvidia-smi + NVML (+ Linux kernel module) status; cached, shared by two sections."""
    status: dict[str, JSON] = {"nvidia_smi": _nvidia_smi(), "nvml": _nvml()}
    if IS_LINUX:
        status["linux"] = _linux_nvidia_module()
    return status


def probe_nvidia() -> Probe:
    status = nvidia_status()
    smi, nvml = status["nvidia_smi"], status["nvml"]
    working = bool(smi.get("ok") or nvml.get("ok"))
    driver = (
        (smi.get("gpus") or [{}])[0].get("driver_version") if smi.get("ok") else None
    )
    value = {
        **status,
        "driver_working": working,
        "driver_version": driver or nvml.get("driver_version"),
        "cuda_driver_version": smi.get("cuda_driver_version")
        or nvml.get("cuda_driver_version"),
    }
    return ok("nvidia-smi+nvml", value)


def _merge_gpus(
    base: list[dict[str, JSON]], nvidia: list[dict[str, JSON]]
) -> list[JSON]:
    """OS-listed GPUs with NVIDIA rows replaced by richer nvidia-smi/NVML rows."""
    if not nvidia:
        return list(base)
    others = [g for g in base if g.get("vendor") != "NVIDIA"]
    return [*others, *nvidia]


def probe_gpus() -> Probe:
    status = nvidia_status()
    smi, nvml = status["nvidia_smi"], status["nvml"]
    nvidia_rows: list[dict[str, JSON]] = []
    if smi.get("ok"):
        nvidia_rows = [dict(g) for g in smi["gpus"]]
        cuda = smi.get("cuda_driver_version") or nvml.get("cuda_driver_version")
        for g in nvidia_rows:
            g["cuda_driver_version"] = cuda
            g["source"] = "nvidia-smi"
    elif nvml.get("ok"):
        for g in nvml.get("gpus") or []:
            nvidia_rows.append(
                {
                    **g,
                    "vendor": "NVIDIA",
                    "driver_version": nvml.get("driver_version"),
                    "cuda_driver_version": nvml.get("cuda_driver_version"),
                    "source": "nvml",
                }
            )
    if IS_LINUX:
        lspci = run("lspci", ["-mm"])
        if not lspci.ok and not nvidia_rows:
            return _cmd_fail("lspci", lspci)
        base = pl.parse_lspci_mm(lspci.stdout) if lspci.ok else []
        for g in base:
            g["source"] = "lspci"
        return ok("lspci+nvidia", _merge_gpus(base, nvidia_rows))
    if IS_WINDOWS:
        try:
            base = pw.parse_video_controllers(_cim("video_controllers"))
        except RuntimeError:
            if not nvidia_rows:
                raise
            base = []
        for g in base:
            g["source"] = "powershell"
        return ok("powershell+nvidia", _merge_gpus(base, nvidia_rows))
    return ok("nvidia", nvidia_rows)


def probe_secure_boot() -> Probe:
    if IS_LINUX:
        if which("mokutil") is not None:
            result = run("mokutil", ["--sb-state"])
            text = f"{result.stdout}\n{result.stderr}"
            state = pl.parse_mokutil_sb_state(text)
            if state != "unknown":
                return ok("mokutil", {"state": state, "detail": text.strip()[:200]})
        efivars = _fs("sys/firmware/efi/efivars")
        if not _fs("sys/firmware/efi").exists():
            return ok(
                "sysfs", {"state": "unsupported", "detail": "legacy BIOS boot (no EFI)"}
            )
        for var in sorted(efivars.glob("SecureBoot-*")):
            try:
                return ok(
                    "efivars",
                    {
                        "state": pl.parse_secureboot_efivar(var.read_bytes()),
                        "detail": None,
                    },
                )
            except OSError as exc:
                return ok("efivars", {"state": "unknown", "detail": str(exc)})
        return ok(
            "efivars", {"state": "unknown", "detail": "SecureBoot efivar not found"}
        )
    if IS_WINDOWS:
        reg = _windows_secure_boot_registry()
        if reg is not None:
            return ok("registry", {"state": reg, "detail": None})
        return ok(
            "powershell", pw.parse_secure_boot_result(windows_cim().get("secure_boot"))
        )
    return ok(
        "none", {"state": "unknown", "detail": "not implemented on this platform"}
    )


def _windows_secure_boot_registry() -> str | None:
    """HKLM SecureBoot\\State\\UEFISecureBootEnabled (readable without admin)."""
    if sys.platform == "win32":
        import winreg

        key_path = r"SYSTEM\CurrentControlSet\Control\SecureBoot\State"
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                value, _ = winreg.QueryValueEx(key, "UEFISecureBootEnabled")
        except OSError:
            return None
        return "enabled" if value == 1 else "disabled"
    return None


def probe_python() -> Probe:
    return ok(
        "sys",
        {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
            "in_virtualenv": sys.prefix != sys.base_prefix,
            "bits": 64 if sys.maxsize > 2**32 else 32,
        },
    )


def probe_uv() -> Probe:
    info = _tool_version("uv")
    if info["version"] is None:
        return fail("uv", str(info.get("error")))
    return ok("uv", info["version"])


def probe_torch() -> Probe:
    result = run(
        sys.executable,
        ["-c", TORCH_SCRIPT],
        timeout=TORCH_TIMEOUT_S,
        remember_hang=False,
    )
    if not result.ok:
        return _cmd_fail("python-subprocess", result)
    try:
        data = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return fail(
            "python-subprocess",
            f"unparseable torch helper output: {result.stdout[:200]}",
        )
    return ok("python-subprocess", data)


def probe_docker() -> Probe:
    if which("docker") is None:
        return ok("docker", {"installed": False, "reachable": False})
    fmt = "{{json .}}"
    version = run("docker", ["version", "--format", fmt], timeout=DOCKER_TIMEOUT_S)
    value: dict[str, JSON] = {"installed": True}
    try:
        value.update(pc.parse_docker_version(version.stdout))
    except (ValueError, TypeError):
        value.update(client_version=None, server_version=None)
    info = run("docker", ["info", "--format", fmt], timeout=DOCKER_TIMEOUT_S)
    info_value: dict[str, JSON] | None = None
    if info.ok:
        try:
            info_value = pc.parse_docker_info(info.stdout)
        except ValueError as exc:
            value["info_error"] = str(exc)
    value["reachable"] = bool(value.get("server_version")) and info.ok
    value["info"] = info_value
    if not value["reachable"]:
        value["error"] = (
            version.describe_failure() if not version.ok else info.describe_failure()
        )[:1000]
    return ok("docker", value)


def _linux_network() -> list[dict[str, JSON]]:
    ips: dict[str, list[dict[str, JSON]]] = {}
    ip_result = run("ip", ["-j", "-4", "addr"])
    if ip_result.ok:
        ips = pl.parse_ip_json(ip_result.stdout)
    adapters: list[dict[str, JSON]] = []
    root = _fs("sys/class/net")
    for dev in sorted(root.iterdir()):
        name = dev.name
        kind = pl.classify_sys_net(
            name,
            has_device=(dev / "device").exists(),
            has_wireless=(dev / "wireless").exists() or (dev / "phy80211").exists(),
            type_code=_read(dev / "type"),
        )
        operstate = (_read(dev / "operstate") or "").strip()
        speed: float | None = pl.parse_sys_speed(_read(dev / "speed"))
        speed_source = "sysfs" if speed else None
        if kind == "wifi" and speed is None and which("iw") is not None:
            iw = run("iw", ["dev", name, "link"])
            bitrate = pl.parse_iw_link(iw.stdout) if iw.ok else None
            if bitrate is not None:
                speed, speed_source = bitrate, "iw tx bitrate"
        adapters.append(
            {
                "name": name,
                "kind": kind,
                "mac": (_read(dev / "address") or "").strip() or None,
                "ipv4": [f"{a['address']}/{a['prefixlen']}" for a in ips.get(name, [])],
                "link_speed_mbit": speed,
                "link_speed_source": speed_source,
                "up": operstate == "up",
                "status": operstate or None,
            }
        )
    return adapters


def _psutil_network(psutil: Any) -> list[dict[str, JSON]]:
    from _inventory.sanitize import is_shareable_ipv4

    stats = psutil.net_if_stats()
    out: list[dict[str, JSON]] = []
    for name, addrs in sorted(psutil.net_if_addrs().items()):
        st = stats.get(name)
        macs = [a.address for a in addrs if a.family == psutil.AF_LINK]
        ipv4 = [
            f"{a.address}/{a.netmask}"
            for a in addrs
            if a.family == socket.AF_INET and is_shareable_ipv4(a.address)
        ]
        out.append(
            {
                "name": name,
                "kind": "unknown",
                "mac": macs[0].replace("-", ":").lower() if macs else None,
                "ipv4": ipv4,
                "link_speed_mbit": (st.speed or None) if st else None,
                "up": bool(st.isup) if st else None,
                "status": None,
            }
        )
    return out


def _os_network() -> Probe:
    if IS_LINUX:
        return ok("sysfs+ip", {"adapters": _linux_network()})
    if IS_WINDOWS:
        adapters = pw.parse_net_adapters(_cim("net_adapters"), _cim("net_ip"))
        return ok("powershell", {"adapters": adapters})
    raise RuntimeError("no OS network source for this platform")


def probe_network() -> Probe:
    """OS sources first (they know Wi-Fi vs Ethernet); psutil as the fallback."""
    try:
        return _os_network()
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        psutil = optional_module("psutil")
        if psutil is None:
            raise
        value = _psutil_network(psutil)
        return ok("psutil", {"adapters": value, "os_source_error": str(exc)})


def probe_power() -> Probe:
    psutil = optional_module("psutil")
    if IS_LINUX:
        root = _fs("sys/class/power_supply")
        if root.is_dir():
            entries = {
                d.name: {
                    f: (_read(d / f) or "").strip()
                    for f in ("type", "online", "status", "capacity")
                    if (d / f).exists()
                }
                for d in root.iterdir()
            }
            return ok("sysfs", pl.parse_power_supplies(entries))
    if IS_WINDOWS:
        return ok("powershell", pw.parse_battery(_cim("battery")))
    if psutil is not None:
        batt = psutil.sensors_battery()
        if batt is None:
            return ok("psutil", {"on_ac": None, "has_battery": False, "batteries": []})
        return ok(
            "psutil",
            {
                "on_ac": batt.power_plugged,
                "has_battery": True,
                "batteries": [{"percent": round(batt.percent), "status": None}],
            },
        )
    return fail("none", "no power source readable")


def probe_tools() -> Probe:
    names = [
        "git",
        "node",
        "npm",
        "gh",
        "iperf3",
        "protoc",
        "psql",
        "nvidia-smi",
        "docker",
    ]
    if IS_WINDOWS:
        names.append("wsl")
    value: dict[str, JSON] = {}
    for name in names:
        if name in {"nvidia-smi", "wsl"}:
            value[name] = {"available": which(name) is not None}
        elif name == "iperf3":
            value[name] = _tool_version(name, ["-v"])
        else:
            value[name] = _tool_version(name)
    return ok("PATH", value)


# --------------------------------------------------------------------- orchestration


def _guarded(name: str, fn: Callable[[], Probe]) -> Probe:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - failures are data; one probe never crashes the run
        return fail(f"{name}-probe", f"{type(exc).__name__}: {exc}")


def collect(repo: Path) -> dict[str, JSON]:
    """Run every probe and return the (unsanitised) document."""
    probes: dict[str, Callable[[], Probe]] = {
        "git": lambda: probe_git(repo),
        "os": probe_os,
        "wsl": probe_wsl,
        "cpu": probe_cpu,
        "memory": probe_memory,
        "disks": lambda: probe_disks(repo),
        "gpus": probe_gpus,
        "nvidia": probe_nvidia,
        "secure_boot": probe_secure_boot,
        "python": probe_python,
        "uv": probe_uv,
        "torch": probe_torch,
        "docker": probe_docker,
        "network": probe_network,
        "power": probe_power,
        "tools": probe_tools,
    }
    assert tuple(probes) == SECTIONS  # noqa: S101 - programming invariant
    sections = {name: _guarded(name, fn) for name, fn in probes.items()}
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "hostname": socket.gethostname(),
        "platform": sys.platform,
        "sections": sections,
        "failed_sections": sorted(n for n, p in sections.items() if not p["ok"]),
        "problems": find_problems(sections),
    }
