"""NVIDIA GPU inventory and live usage: NVML first, ``nvidia-smi`` as the fallback.

**NVML is primary** (``nvidia-ml-py``, in-process): it is the only source that is
cheap enough to poll every couple of seconds, so the 0.3.5 resource monitor keeps
one :class:`NvmlSession` open for the process lifetime and re-acquires it if the
driver goes away and comes back. ``nvidia-smi`` is the static-inventory fallback for
a machine that has a working driver but no ``pynvml`` (or an NVML that errors); it
is a subprocess with a hard timeout and is never used for periodic sampling.

**A machine with no NVIDIA GPU is normal**, not an error: both sources report
"unavailable" with a short reason, the GPU list is empty, and nothing raises. The
same is true for a driver that is installed but broken (Secure Boot blocking the
kernel module is the case the V0.2 lab run found).

Everything NVML raises is driver-defined (``pynvml.NVMLError`` subclasses created at
import time), so the guards here catch broadly on purpose and record the type name.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Any, Final

from meshforge.agent.hardware.limits import (
    GPU_COMPUTE_CAPABILITY_MAX_LENGTH,
    GPU_DRIVER_MAX_LENGTH,
    GPU_MODEL_MAX_LENGTH,
    MAX_GPUS,
)
from meshforge.agent.hardware.optional import pynvml_module
from meshforge.agent.hardware.probe import (
    Budget,
    ProbeOutcome,
    ProbeStatus,
    clip,
    run_command,
    which,
)
from meshforge.common.logging import get_logger

__all__ = [
    "GPU_PROBE_CAP_S",
    "NVIDIA_SMI_FIELDS",
    "NVIDIA_SMI_FIELDS_LEGACY",
    "GpuFacts",
    "GpuUsage",
    "NvmlSession",
    "merge_by_index",
    "parse_nvidia_smi_csv",
    "probe_gpus",
]

log = get_logger(__name__)

GPU_PROBE_CAP_S: Final = 3.0
"""Cap for the whole GPU probe; ``nvidia-smi`` on a wedged driver can hang."""

NVIDIA_SMI_FIELDS: Final[tuple[str, ...]] = (
    "index",
    "name",
    "memory.total",
    "driver_version",
    "compute_cap",
)
NVIDIA_SMI_FIELDS_LEGACY: Final[tuple[str, ...]] = (
    "index",
    "name",
    "memory.total",
    "driver_version",
)
"""Old drivers reject ``compute_cap``; the query is retried without it."""

_MIB: Final = 1024 * 1024
_NOT_AVAILABLE: Final[frozenset[str]] = frozenset(
    {"", "[n/a]", "n/a", "[not supported]", "not supported", "[unknown error]"}
)


@dataclass(frozen=True, slots=True)
class GpuFacts:
    """Static facts about one device. Strings are already clipped to the wire caps."""

    index: int
    model: str
    vram_bytes: int
    driver_version: str
    compute_capability: str
    source: str
    """``"nvml"`` or ``"nvidia-smi"`` - kept for the operator surface, not the wire."""


@dataclass(frozen=True, slots=True)
class GpuUsage:
    """One device's live usage (roadmap 0.3.5)."""

    index: int
    util_percent: float
    vram_used_bytes: int
    vram_total_bytes: int


def _text(value: Any) -> str:
    """NVML returns ``bytes`` on some driver versions and ``str`` on others."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _clean(text: str) -> str:
    stripped = text.strip()
    return "" if stripped.lower() in _NOT_AVAILABLE else stripped


def parse_nvidia_smi_csv(text: str, fields: tuple[str, ...]) -> list[GpuFacts]:
    """Parse ``--format=csv,noheader,nounits`` output. Raises ``ValueError`` on a
    row that has not one column per field (a driver error message, a cut line)."""
    out: list[GpuFacts] = []
    for row in csv.reader(io.StringIO(text.strip())):
        if not row or all(not cell.strip() for cell in row):
            continue
        if len(row) != len(fields):
            raise ValueError(
                f"nvidia-smi returned {len(row)} columns, expected {len(fields)}"
            )
        raw = dict(zip(fields, (_clean(cell) for cell in row), strict=True))
        index_text = raw.get("index", "")
        if not index_text.isdigit():
            raise ValueError("nvidia-smi returned a non-numeric device index")
        vram_mib = raw.get("memory.total", "")
        out.append(
            GpuFacts(
                index=int(index_text),
                model=clip(raw.get("name", ""), GPU_MODEL_MAX_LENGTH),
                vram_bytes=int(float(vram_mib)) * _MIB if _is_number(vram_mib) else 0,
                driver_version=clip(
                    raw.get("driver_version", ""), GPU_DRIVER_MAX_LENGTH
                ),
                compute_capability=clip(
                    raw.get("compute_cap", ""), GPU_COMPUTE_CAPABILITY_MAX_LENGTH
                ),
                source="nvidia-smi",
            )
        )
    return out


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def merge_by_index(
    primary: list[GpuFacts], secondary: list[GpuFacts]
) -> list[GpuFacts]:
    """``primary`` wins per device index; ``secondary`` fills indices it does not have."""
    merged = {gpu.index: gpu for gpu in secondary}
    merged.update({gpu.index: gpu for gpu in primary})
    return sorted(merged.values(), key=lambda gpu: gpu.index)[:MAX_GPUS]


class NvmlSession:
    """An open NVML handle set, or a recorded reason why there is none.

    Not thread-safe by itself. Every caller (discovery and the resource sampler,
    which share one session) goes through the same
    :class:`~meshforge.agent.hardware.deadline.DeadlineThread`, so exactly one
    call touches it at a time - including after a wedged call was abandoned, since
    that one still holds the slot.

    **No call here can be interrupted**: NVML is synchronous FFI, so a driver that
    hangs instead of erroring hangs its thread. The deadline belongs *around* this
    object, not inside it.
    """

    __slots__ = ("_driver_version", "_handles", "_nvml", "reason")

    def __init__(self) -> None:
        self._nvml: Any | None = None
        self._handles: list[Any] = []
        self._driver_version: str = ""
        self.reason: str = "not initialised"

    @property
    def available(self) -> bool:
        return self._nvml is not None

    @property
    def driver_version(self) -> str:
        return self._driver_version

    def open(self) -> bool:
        """Initialise NVML and take a handle per device. False + ``reason`` on failure."""
        if self._nvml is not None:
            return True
        nvml = pynvml_module()
        if nvml is None:
            self.reason = "nvidia-ml-py (pynvml) is not installed"
            return False
        try:
            nvml.nvmlInit()
        except Exception as exc:  # noqa: BLE001 - NVML errors are driver-defined
            self.reason = f"nvmlInit failed: {type(exc).__name__}"
            return False
        try:
            count = int(nvml.nvmlDeviceGetCount())
            self._handles = [
                nvml.nvmlDeviceGetHandleByIndex(i) for i in range(min(count, MAX_GPUS))
            ]
            self._driver_version = clip(
                _text(nvml.nvmlSystemGetDriverVersion()), GPU_DRIVER_MAX_LENGTH
            )
        except Exception as exc:  # noqa: BLE001 - NVML errors are driver-defined
            self.reason = f"NVML enumeration failed: {type(exc).__name__}"
            self._handles = []
            _shutdown_quietly(nvml)
            return False
        self._nvml = nvml
        self.reason = ""
        return True

    def close(self) -> None:
        """Shut NVML down; safe to call twice and after a failed :meth:`open`."""
        nvml, self._nvml = self._nvml, None
        self._handles = []
        if nvml is not None:
            _shutdown_quietly(nvml)
        self.reason = "closed"

    def _invalidate(self, reason: str) -> None:
        """A device call failed: drop the handles so the next sample re-acquires them."""
        self.close()
        self.reason = reason

    def devices(self) -> list[GpuFacts]:
        """Static facts for every device; ``[]`` when NVML is not open."""
        nvml = self._nvml
        if nvml is None:
            return []
        out: list[GpuFacts] = []
        for index, handle in enumerate(self._handles):
            try:
                memory = nvml.nvmlDeviceGetMemoryInfo(handle)
                model = _text(nvml.nvmlDeviceGetName(handle))
                try:
                    major, minor = nvml.nvmlDeviceGetCudaComputeCapability(handle)
                    capability = f"{int(major)}.{int(minor)}"
                except Exception:  # noqa: BLE001 - optional on old drivers
                    capability = ""
            except Exception as exc:  # noqa: BLE001 - NVML errors are driver-defined
                self._invalidate(f"NVML device query failed: {type(exc).__name__}")
                return out
            out.append(
                GpuFacts(
                    index=index,
                    model=clip(model, GPU_MODEL_MAX_LENGTH),
                    vram_bytes=int(memory.total),
                    driver_version=self._driver_version,
                    compute_capability=clip(
                        capability, GPU_COMPUTE_CAPABILITY_MAX_LENGTH
                    ),
                    source="nvml",
                )
            )
        return out

    def usage(self) -> list[GpuUsage]:
        """Live usage per device; ``[]`` (and ``reason`` set) when NVML is not usable."""
        nvml = self._nvml
        if nvml is None and not self.open():
            return []
        nvml = self._nvml
        if nvml is None:  # pragma: no cover - open() returned True
            return []
        out: list[GpuUsage] = []
        for index, handle in enumerate(self._handles):
            try:
                rates = nvml.nvmlDeviceGetUtilizationRates(handle)
                memory = nvml.nvmlDeviceGetMemoryInfo(handle)
            except Exception as exc:  # noqa: BLE001 - NVML errors are driver-defined
                self._invalidate(f"NVML usage query failed: {type(exc).__name__}")
                return out
            out.append(
                GpuUsage(
                    index=index,
                    util_percent=float(rates.gpu),
                    vram_used_bytes=int(memory.used),
                    vram_total_bytes=int(memory.total),
                )
            )
        return out


def _shutdown_quietly(nvml: Any) -> None:
    """``nvmlShutdown()`` that cannot raise (it is called on failure paths)."""
    try:
        nvml.nvmlShutdown()
    except Exception as exc:  # noqa: BLE001 - shutdown failure is not actionable
        log.debug("agent.hardware.nvml_shutdown_failed", cause=type(exc).__name__)


def _nvidia_smi_devices(timeout_s: float) -> tuple[list[GpuFacts], str]:
    """``(devices, reason)``; an empty list with a reason when the tool cannot answer."""
    if which("nvidia-smi") is None:
        return [], "nvidia-smi is not on PATH"
    for fields in (NVIDIA_SMI_FIELDS, NVIDIA_SMI_FIELDS_LEGACY):
        result = run_command(
            "nvidia-smi",
            [f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits"],
            timeout_s=timeout_s,
        )
        if result.ok:
            try:
                return parse_nvidia_smi_csv(result.stdout, fields), ""
            except ValueError as exc:
                return [], f"nvidia-smi output unparseable: {exc}"
        failure = result.describe_failure()
        if "field" not in failure.lower() or fields is NVIDIA_SMI_FIELDS_LEGACY:
            # A driver failure reads the same either way; only an unknown-field
            # complaint is worth the legacy retry.
            return [], f"nvidia-smi failed: {failure[:160]}"
    return [], "nvidia-smi query failed"


def probe_gpus(
    budget: Budget,
    *,
    cap_s: float = GPU_PROBE_CAP_S,
    session: NvmlSession | None = None,
) -> ProbeOutcome[list[GpuFacts]]:
    """NVIDIA devices on this machine (possibly none - that is ``UNAVAILABLE``, not
    an error). ``session`` is the monitor's NVML session when it already exists."""
    if budget.expired:
        return ProbeOutcome("gpu", ProbeStatus.TIMEOUT, reason="discovery budget spent")
    started = budget.now()
    try:
        return _probe_gpus(budget, cap_s, session, started)
    except Exception as exc:  # noqa: BLE001 - one field, never the agent (D10)
        return ProbeOutcome(
            "gpu",
            ProbeStatus.ERROR,
            value=[],
            reason=f"{type(exc).__name__}: {exc}"[:200],
            duration_s=budget.now() - started,
        )


def _probe_gpus(
    budget: Budget, cap_s: float, session: NvmlSession | None, started: float
) -> ProbeOutcome[list[GpuFacts]]:
    owned = session is None
    nvml_session = session or NvmlSession()
    nvml_devices: list[GpuFacts] = []
    nvml_reason = ""
    if nvml_session.open():
        nvml_devices = nvml_session.devices()
        nvml_reason = nvml_session.reason or ""
    else:
        nvml_reason = nvml_session.reason
    if owned:
        nvml_session.close()

    smi_devices: list[GpuFacts] = []
    smi_reason = ""
    if not nvml_devices:
        smi_devices, smi_reason = _nvidia_smi_devices(budget.slice_s(cap_s))

    devices = merge_by_index(nvml_devices, smi_devices)
    duration = budget.now() - started
    if devices:
        return ProbeOutcome("gpu", ProbeStatus.OK, value=devices, duration_s=duration)
    reason = "; ".join(part for part in (nvml_reason, smi_reason) if part)
    log.info(
        "agent.hardware.no_gpu",
        reason=reason or "no NVIDIA device found",
    )
    return ProbeOutcome(
        "gpu",
        ProbeStatus.UNAVAILABLE,
        value=[],
        reason=reason or "no NVIDIA device found",
        duration_s=duration,
    )
