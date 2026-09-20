"""V0.3 G4 (roadmap 0.3.3): hardware discovery - parsers, guards, budget, clipping.

Nothing here asserts anything about *this* machine: no hostname, no GPU, no driver
version, no real mount path. The NVML/nvidia-smi inputs are captured strings, the
psutil/NVML seams are replaced with in-test fakes, and the only real subprocess is a
short-lived ``sys.executable`` that the timeout test kills.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from meshforge.agent.hardware import cpu as cpu_mod
from meshforge.agent.hardware import disks as disks_mod
from meshforge.agent.hardware import gpu as gpu_mod
from meshforge.agent.hardware import software as software_mod
from meshforge.agent.hardware import summary as summary_mod
from meshforge.agent.hardware.cpu import (
    parse_cpuinfo_model,
    parse_meminfo_total_bytes,
    parse_win32_processor,
)
from meshforge.agent.hardware.disks import DiskFacts, is_fixed_filesystem
from meshforge.agent.hardware.gpu import (
    NVIDIA_SMI_FIELDS,
    NVIDIA_SMI_FIELDS_LEGACY,
    GpuFacts,
    NvmlSession,
    merge_by_index,
    parse_nvidia_smi_csv,
    probe_gpus,
)
from meshforge.agent.hardware.limits import (
    CAPABILITY_MAX_LENGTH,
    CPU_MODEL_MAX_LENGTH,
    GPU_COMPUTE_CAPABILITY_MAX_LENGTH,
    GPU_DRIVER_MAX_LENGTH,
    GPU_MODEL_MAX_LENGTH,
    HARDWARE_SUMMARY_MAX_BYTES,
    MAX_CAPABILITIES,
    MAX_GPUS,
    clip_capabilities,
    clip_hardware,
)
from meshforge.agent.hardware.probe import (
    Budget,
    CmdResult,
    ProbeStatus,
    guarded,
    run_command,
)
from meshforge.agent.hardware.summary import HardwareFacts, HardwareProvider, discover
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = pytest.mark.unit


class FakeClock:
    """Monotonic seconds under test control (never a real sleep)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --------------------------------------------------------------------- budget


def test_budget_slices_shrink_and_expire() -> None:
    clock = FakeClock()
    budget = Budget(5.0, clock=clock)
    assert budget.slice_s(3.0) == 3.0
    clock.advance(3.0)
    assert budget.slice_s(3.0) == pytest.approx(2.0)
    assert bool(budget.expired) is False
    clock.advance(2.0)
    assert bool(budget.expired) is True
    assert budget.remaining_s == 0.0


def test_budget_rejects_a_non_positive_total() -> None:
    with pytest.raises(ValueError, match="total_s"):
        Budget(0.0)


def test_guarded_turns_an_exception_into_one_failed_field() -> None:
    def boom(_timeout_s: float) -> str:
        raise OSError("device not ready")

    outcome = guarded("thing", Budget(5.0, clock=FakeClock()), 1.0, boom)
    assert outcome.status is ProbeStatus.ERROR
    assert outcome.value is None
    assert "OSError" in outcome.reason and "device not ready" in outcome.reason


def test_guarded_reports_unavailable_for_a_missing_source() -> None:
    def absent(_timeout_s: float) -> str | None:
        return None

    outcome = guarded("thing", Budget(5.0, clock=FakeClock()), 1.0, absent)
    assert outcome.status is ProbeStatus.UNAVAILABLE


def test_guarded_does_not_start_a_probe_once_the_budget_is_spent() -> None:
    clock = FakeClock()
    budget = Budget(1.0, clock=clock)
    clock.advance(1.0)
    calls: list[float] = []

    def record(timeout_s: float) -> str:
        calls.append(timeout_s)
        return "ran"

    outcome = guarded("thing", budget, 1.0, record)
    assert outcome.status is ProbeStatus.TIMEOUT
    assert calls == []


def test_guarded_maps_a_timeout_error_to_the_timeout_status() -> None:
    def slow(_timeout_s: float) -> str:
        raise TimeoutError("nvidia-smi: timed out after 1 s")

    outcome = guarded("gpu", Budget(5.0, clock=FakeClock()), 1.0, slow)
    assert outcome.status is ProbeStatus.TIMEOUT
    assert "timed out" in outcome.reason


# ------------------------------------------------------------------ commands


def test_run_command_reports_a_missing_executable_without_raising() -> None:
    result = run_command("meshforge-no-such-tool-xyz", [], timeout_s=1.0)
    assert not result.ok
    assert "not found on PATH" in result.describe_failure()


def test_run_command_kills_a_child_that_does_not_answer() -> None:
    result = run_command(
        sys.executable, ["-c", "import time; time.sleep(30)"], timeout_s=0.06
    )
    assert not result.ok
    assert "timed out" in result.describe_failure()


def test_run_command_refuses_to_start_without_a_time_slice() -> None:
    result = run_command(sys.executable, ["-c", "pass"], timeout_s=0.0)
    assert not result.ok
    assert "no time budget left" in result.describe_failure()


# ----------------------------------------------------------------- cpu/disks


def test_parse_cpuinfo_model_takes_the_first_model_name() -> None:
    text = "processor\t: 0\nmodel name\t: Fake CPU  9999  @ 1.00GHz\nprocessor\t: 1\n"
    assert parse_cpuinfo_model(text) == "Fake CPU 9999 @ 1.00GHz"


def test_parse_cpuinfo_model_falls_back_to_an_arm_style_model_line() -> None:
    assert parse_cpuinfo_model("Hardware\t: Fake ARM Board\n") == "Fake ARM Board"


def test_parse_cpuinfo_model_returns_empty_for_junk() -> None:
    assert parse_cpuinfo_model("") == ""
    assert parse_cpuinfo_model("not a cpuinfo file at all") == ""


def test_parse_meminfo_total_bytes_converts_kibibytes() -> None:
    assert parse_meminfo_total_bytes("MemTotal:  1024 kB\nMemFree: 1 kB\n") == 1048576
    assert parse_meminfo_total_bytes("MemFree: 1 kB\n") == 0
    assert parse_meminfo_total_bytes("MemTotal:  not-a-number kB\n") == 0


def test_parse_win32_processor_takes_the_first_non_empty_line() -> None:
    assert parse_win32_processor("\r\n  Fake  Windows  CPU \r\n") == "Fake Windows CPU"
    assert parse_win32_processor("   \n\n") == ""


@pytest.mark.parametrize(
    ("device", "mountpoint", "fstype", "opts", "keep"),
    [
        ("/dev/sda1", "/", "ext4", "rw,relatime", True),
        ("C:\\", "C:\\", "NTFS", "rw,fixed", True),
        ("/dev/loop3", "/snap/core/1", "squashfs", "ro", False),
        ("tmpfs", "/run", "tmpfs", "rw", False),
        ("server:/export", "/mnt/nfs", "nfs4", "rw", False),
        ("D:\\", "D:\\", "CDFS", "ro,cdrom", False),
        ("/dev/sdb1", "/media/usb", "vfat", "rw,removable", False),
        ("overlay", "/var/lib/docker/overlay2/x", "overlay", "rw", False),
        ("/dev/sdc1", "/data", "", "rw", False),
    ],
)
def test_is_fixed_filesystem_rules(
    device: str, mountpoint: str, fstype: str, opts: str, keep: bool
) -> None:
    assert (
        is_fixed_filesystem(
            device=device, mountpoint=mountpoint, fstype=fstype, opts=opts
        )
        is keep
    )


def test_list_mounts_falls_back_to_the_root_without_psutil(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(disks_mod, "psutil_module", lambda: None)
    mounts = disks_mod.list_mounts()
    assert len(mounts) == 1  # the platform root, whatever it is called here


def test_disk_usage_skips_a_mount_that_vanished() -> None:
    facts = disks_mod.disk_usage(["/definitely/not/a/mount/point/xyz"])
    assert facts == []


# ---------------------------------------------------------------------- gpu


CSV_TWO_GPUS = (
    "0, NVIDIA GeForce RTX 3050 Laptop GPU, 4096, 555.01, 8.6\n"
    "1, NVIDIA GeForce GTX 1650, 4096, 555.01, 7.5\n"
)


def test_parse_nvidia_smi_csv_reads_every_device() -> None:
    gpus = parse_nvidia_smi_csv(CSV_TWO_GPUS, NVIDIA_SMI_FIELDS)
    assert [g.index for g in gpus] == [0, 1]
    assert gpus[0].model == "NVIDIA GeForce RTX 3050 Laptop GPU"
    assert gpus[0].vram_bytes == 4096 * 1024 * 1024
    assert gpus[0].driver_version == "555.01"
    assert gpus[0].compute_capability == "8.6"
    assert gpus[0].source == "nvidia-smi"


def test_parse_nvidia_smi_csv_handles_no_devices() -> None:
    assert parse_nvidia_smi_csv("", NVIDIA_SMI_FIELDS) == []
    assert parse_nvidia_smi_csv("\n  \n", NVIDIA_SMI_FIELDS) == []


def test_parse_nvidia_smi_csv_accepts_the_legacy_field_set() -> None:
    gpus = parse_nvidia_smi_csv("0, Old GPU, 2048, 390.157\n", NVIDIA_SMI_FIELDS_LEGACY)
    assert gpus[0].compute_capability == ""
    assert gpus[0].vram_bytes == 2048 * 1024 * 1024


def test_parse_nvidia_smi_csv_maps_not_supported_to_empty() -> None:
    gpus = parse_nvidia_smi_csv(
        "0, Some GPU, 2048, 390.157, [Not Supported]\n", NVIDIA_SMI_FIELDS
    )
    assert gpus[0].compute_capability == ""


def test_parse_nvidia_smi_csv_rejects_a_driver_error_message() -> None:
    text = "NVIDIA-SMI has failed because it couldn't communicate with the driver\n"
    with pytest.raises(ValueError, match="columns"):
        parse_nvidia_smi_csv(text, NVIDIA_SMI_FIELDS)


def test_parse_nvidia_smi_csv_rejects_a_truncated_line() -> None:
    with pytest.raises(ValueError, match="columns"):
        parse_nvidia_smi_csv("0, NVIDIA Thing, 4096\n", NVIDIA_SMI_FIELDS)


def test_parse_nvidia_smi_csv_rejects_a_non_numeric_index() -> None:
    with pytest.raises(ValueError, match="index"):
        parse_nvidia_smi_csv("x, A, 1, 2, 3\n", NVIDIA_SMI_FIELDS)


def test_parse_nvidia_smi_csv_clips_a_pathological_model_string() -> None:
    gpus = parse_nvidia_smi_csv(f"0, {'M' * 500}, 1, 2, 3.5\n", NVIDIA_SMI_FIELDS)
    assert len(gpus[0].model) == GPU_MODEL_MAX_LENGTH


def test_merge_by_index_prefers_the_primary_source() -> None:
    nvml = [GpuFacts(0, "From NVML", 1, "d", "8.6", "nvml")]
    smi = [
        GpuFacts(0, "From smi", 2, "d", "8.6", "nvidia-smi"),
        GpuFacts(1, "Second", 3, "d", "7.5", "nvidia-smi"),
    ]
    merged = merge_by_index(nvml, smi)
    assert [g.index for g in merged] == [0, 1]
    assert merged[0].source == "nvml"
    assert merged[1].source == "nvidia-smi"


def test_merge_by_index_caps_the_device_count() -> None:
    many = [GpuFacts(i, "g", 1, "d", "8.6", "nvml") for i in range(MAX_GPUS + 5)]
    assert len(merge_by_index(many, [])) == MAX_GPUS


class FakeNvml:
    """The handful of NVML entry points the session uses."""

    class Memory:
        def __init__(self, total: int, used: int) -> None:
            self.total = total
            self.used = used

    class Rates:
        def __init__(self, gpu: int) -> None:
            self.gpu = gpu

    def __init__(self, *, devices: int = 1, fail_on: str | None = None) -> None:
        self.devices = devices
        self.fail_on = fail_on
        self.inits = 0
        self.shutdowns = 0

    def _maybe_fail(self, name: str) -> None:
        if self.fail_on == name:
            raise RuntimeError(f"NVML_ERROR in {name}")

    def nvmlInit(self) -> None:  # noqa: N802 - NVML's own spelling
        self.inits += 1
        self._maybe_fail("init")

    def nvmlShutdown(self) -> None:  # noqa: N802
        self.shutdowns += 1

    def nvmlDeviceGetCount(self) -> int:  # noqa: N802
        self._maybe_fail("count")
        return self.devices

    def nvmlDeviceGetHandleByIndex(self, index: int) -> int:  # noqa: N802
        return index

    def nvmlSystemGetDriverVersion(self) -> bytes:  # noqa: N802
        return b"555.01"

    def nvmlDeviceGetName(self, handle: int) -> bytes:  # noqa: N802
        return b"Fake NVML GPU"

    def nvmlDeviceGetMemoryInfo(self, handle: int) -> FakeNvml.Memory:  # noqa: N802
        self._maybe_fail("memory")
        return FakeNvml.Memory(total=8 * 2**30, used=2 * 2**30)

    def nvmlDeviceGetCudaComputeCapability(self, handle: int) -> tuple[int, int]:  # noqa: N802
        self._maybe_fail("capability")
        return (8, 6)

    def nvmlDeviceGetUtilizationRates(self, handle: int) -> FakeNvml.Rates:  # noqa: N802
        self._maybe_fail("rates")
        return FakeNvml.Rates(gpu=42)


def use_nvml(monkeypatch: pytest.MonkeyPatch, nvml: Any | None) -> None:
    monkeypatch.setattr(gpu_mod, "pynvml_module", lambda: nvml)


def test_nvml_session_reports_devices_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_nvml(monkeypatch, FakeNvml(devices=2))
    session = NvmlSession()
    assert session.open() is True
    devices = session.devices()
    assert [d.index for d in devices] == [0, 1]
    assert devices[0].model == "Fake NVML GPU"
    assert devices[0].compute_capability == "8.6"
    usage = session.usage()
    assert usage[0].util_percent == 42.0
    assert usage[0].vram_used_bytes == 2 * 2**30
    session.close()
    assert session.available is False


def test_nvml_session_without_the_library_is_unavailable_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_nvml(monkeypatch, None)
    session = NvmlSession()
    assert session.open() is False
    assert "pynvml" in session.reason
    assert session.devices() == []
    assert session.usage() == []


def test_nvml_session_survives_an_init_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_nvml(monkeypatch, FakeNvml(fail_on="init"))
    session = NvmlSession()
    assert session.open() is False
    assert "nvmlInit failed" in session.reason


def test_nvml_session_shuts_down_after_a_failed_enumeration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nvml = FakeNvml(fail_on="count")
    use_nvml(monkeypatch, nvml)
    session = NvmlSession()
    assert session.open() is False
    assert nvml.shutdowns == 1
    assert "enumeration" in session.reason


def test_nvml_session_invalidates_itself_when_a_device_call_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_nvml(monkeypatch, FakeNvml(fail_on="rates"))
    session = NvmlSession()
    assert session.open() is True
    assert session.usage() == []
    assert session.available is False
    assert "usage query failed" in session.reason


def test_nvml_device_without_compute_capability_still_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_nvml(monkeypatch, FakeNvml(fail_on="capability"))
    session = NvmlSession()
    assert session.open() is True
    devices = session.devices()
    assert devices[0].compute_capability == ""


def test_probe_gpus_uses_nvidia_smi_when_nvml_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_nvml(monkeypatch, None)
    monkeypatch.setattr(gpu_mod, "which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(
        gpu_mod,
        "run_command",
        lambda name, args, *, timeout_s: CmdResult((name, *args), 0, CSV_TWO_GPUS, ""),
    )
    outcome = probe_gpus(Budget(5.0, clock=FakeClock()))
    assert outcome.ok
    assert outcome.value is not None
    assert [g.source for g in outcome.value] == ["nvidia-smi", "nvidia-smi"]


def test_probe_gpus_on_a_machine_without_nvidia_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_nvml(monkeypatch, None)
    monkeypatch.setattr(gpu_mod, "which", lambda name: None)
    outcome = probe_gpus(Budget(5.0, clock=FakeClock()))
    assert outcome.status is ProbeStatus.UNAVAILABLE
    assert outcome.value == []
    assert "pynvml" in outcome.reason and "nvidia-smi" in outcome.reason


def test_probe_gpus_reports_a_driver_failure_as_a_reason_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_nvml(monkeypatch, None)
    monkeypatch.setattr(gpu_mod, "which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(
        gpu_mod,
        "run_command",
        lambda name, args, *, timeout_s: CmdResult(
            (name, *args), 9, "", "NVIDIA-SMI has failed because it couldn't talk"
        ),
    )
    outcome = probe_gpus(Budget(5.0, clock=FakeClock()))
    assert outcome.status is ProbeStatus.UNAVAILABLE
    assert "nvidia-smi failed" in outcome.reason


def test_probe_gpus_never_raises_even_if_a_source_explodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode() -> Any:
        raise ZeroDivisionError("boom")

    monkeypatch.setattr(gpu_mod, "pynvml_module", explode)
    outcome = probe_gpus(Budget(5.0, clock=FakeClock()))
    assert outcome.status is ProbeStatus.ERROR
    assert "ZeroDivisionError" in outcome.reason


# ------------------------------------------------------------------ software


def test_probe_software_reports_the_python_tag_and_the_gpu_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(software_mod, "which", lambda name: None)
    monkeypatch.setattr(software_mod, "module_installed", lambda name: False)
    outcome = software_mod.probe_software(Budget(5.0, clock=FakeClock()), has_gpu=True)
    assert outcome.ok
    assert outcome.value is not None
    assert "cuda" in outcome.value
    assert any(tag.startswith("python") for tag in outcome.value)
    assert "docker" not in outcome.value


def test_probe_software_tags_docker_only_when_a_daemon_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        software_mod,
        "which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    monkeypatch.setattr(software_mod, "module_installed", lambda name: False)
    # A daemon that does not answer: returncode != 0.
    monkeypatch.setattr(
        software_mod,
        "run_command",
        lambda name, args, *, timeout_s: CmdResult((name,), 1, "", "no daemon"),
    )
    outcome = software_mod.probe_software(Budget(5.0, clock=FakeClock()), has_gpu=False)
    assert outcome.value is not None
    assert "docker" not in outcome.value

    monkeypatch.setattr(
        software_mod,
        "run_command",
        lambda name, args, *, timeout_s: CmdResult((name,), 0, "27.1.1\n", ""),
    )
    outcome = software_mod.probe_software(Budget(5.0, clock=FakeClock()), has_gpu=False)
    assert outcome.value is not None
    assert "docker" in outcome.value


def test_probe_software_records_a_torch_probe_that_did_not_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(software_mod, "which", lambda name: None)
    monkeypatch.setattr(software_mod, "module_installed", lambda name: name == "torch")
    monkeypatch.setattr(
        software_mod,
        "run_command",
        lambda name, args, *, timeout_s: CmdResult(
            (name,), None, "", "", error="timed out"
        ),
    )
    outcome = software_mod.probe_software(Budget(5.0, clock=FakeClock()), has_gpu=False)
    assert outcome.status is ProbeStatus.TIMEOUT
    assert outcome.value is not None
    assert "torch" in outcome.value and "torch-cuda" not in outcome.value
    assert "torch-cuda" in outcome.reason


# -------------------------------------------------------------------- limits


def test_clip_capabilities_sorts_deduplicates_and_keeps_the_shape_marker() -> None:
    tags = clip_capabilities(["docker", "v0.3", "cuda", "docker", "  ", "v0.3"])
    assert tags == ["v0.3", "cuda", "docker"]


def test_clip_capabilities_caps_the_count_and_the_tag_length() -> None:
    tags = clip_capabilities(
        ["v0.3", *[f"tag{i:03d}" for i in range(MAX_CAPABILITIES)]]
    )
    assert len(tags) == MAX_CAPABILITIES
    assert tags[0] == "v0.3"
    long = clip_capabilities(["x" * 500])
    assert len(long[0]) == CAPABILITY_MAX_LENGTH


def test_clip_hardware_clips_every_string_cap() -> None:
    summary = agent_pb2.HardwareSummary(
        cpu_model="c" * 500,
        cpu_cores=8,
        cpu_threads=16,
        ram_bytes=1,
        capabilities=["v0.3"],
    )
    summary.gpus.append(
        agent_pb2.HardwareSummary.Gpu(
            index=0,
            model="m" * 500,
            vram_bytes=1,
            driver_version="d" * 500,
            compute_capability="8.6.7.5.3.0.9" * 10,
        )
    )
    clipped = clip_hardware(summary)
    assert len(clipped.cpu_model) == CPU_MODEL_MAX_LENGTH
    assert len(clipped.gpus[0].model) == GPU_MODEL_MAX_LENGTH
    assert len(clipped.gpus[0].driver_version) == GPU_DRIVER_MAX_LENGTH
    assert len(clipped.gpus[0].compute_capability) == GPU_COMPUTE_CAPABILITY_MAX_LENGTH
    assert len(summary.cpu_model) == 500  # the input is not mutated


def test_clip_hardware_caps_the_device_count_and_the_total_size() -> None:
    summary = agent_pb2.HardwareSummary(cpu_model="x")
    for index in range(MAX_GPUS + 4):
        summary.gpus.append(
            agent_pb2.HardwareSummary.Gpu(index=index, model="a-gpu", vram_bytes=1)
        )
    clipped = clip_hardware(summary)
    assert len(clipped.gpus) == MAX_GPUS
    assert clipped.ByteSize() <= HARDWARE_SUMMARY_MAX_BYTES


# ------------------------------------------------------------------ discover


def test_discover_on_a_machine_with_nothing_still_yields_a_valid_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cpu_mod, "psutil_module", lambda: None)
    monkeypatch.setattr(disks_mod, "psutil_module", lambda: None)
    monkeypatch.setattr(gpu_mod, "pynvml_module", lambda: None)
    monkeypatch.setattr(gpu_mod, "which", lambda name: None)
    monkeypatch.setattr(software_mod, "which", lambda name: None)
    monkeypatch.setattr(software_mod, "module_installed", lambda name: False)

    facts = discover(timeout_s=5.0, clock=FakeClock())
    summary = facts.to_summary()
    assert summary.cpu_threads > 0
    assert list(summary.gpus) == []
    assert summary.capabilities[0] == "v0.3"
    assert any("gpu" in reason for reason in facts.degraded)


def test_discover_stops_probing_once_the_budget_is_spent() -> None:
    clock = FakeClock()

    def creeping() -> float:
        clock.advance(10.0)
        return clock.now

    facts = discover(timeout_s=1.0, clock=creeping)
    assert {outcome.status for outcome in facts.probes} == {ProbeStatus.TIMEOUT}
    assert facts.cpu.logical_threads >= 0  # the fallback CPU facts


def test_hardware_provider_caches_until_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[float] = []

    def fake_discover(
        *, timeout_s: float, nvml: Any = None, clock: Any = None
    ) -> HardwareFacts:
        calls.append(timeout_s)
        return HardwareFacts(cpu=cpu_mod.CpuFacts("m", 1, 2, 3, "test"))

    monkeypatch.setattr(summary_mod, "discover", fake_discover)
    provider = HardwareProvider(timeout_s=2.5)
    assert provider.cached is None
    first = provider.get()
    assert provider.get() is first
    assert calls == [2.5]
    provider.refresh()
    assert calls == [2.5, 2.5]


def test_hardware_facts_to_summary_is_clipped_and_marker_first() -> None:
    facts = HardwareFacts(
        cpu=cpu_mod.CpuFacts("c" * 400, 4, 8, 16, "test"),
        gpus=(GpuFacts(0, "g", 2, "d", "8.6", "nvml"),),
        disks=(DiskFacts("/", "ext4", 10, 5),),
        capabilities=("docker", "cuda"),
    )
    summary = facts.to_summary()
    assert len(summary.cpu_model) == CPU_MODEL_MAX_LENGTH
    assert summary.capabilities[0] == "v0.3"
    assert set(summary.capabilities) == {"v0.3", "docker", "cuda"}
    assert summary.cpu_cores == 4
    assert summary.cpu_threads == 8
