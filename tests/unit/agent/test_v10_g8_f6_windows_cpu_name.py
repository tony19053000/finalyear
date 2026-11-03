"""V1.0 G8 F6 - the Windows CPU name comes from the registry, not PowerShell.

package-windows failed ``hardware show`` with "did not finish within 6 s": the
CPU probe started an inline PowerShell CIM query, and a stalled PowerShell plus
its kill (taskkill + reap) can exceed the whole discovery deadline. The name is
now read with ``winreg`` (``cpu_windows``); no child process at all.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest

from meshforge.agent.hardware import cpu
from meshforge.agent.hardware.probe import Budget, ProbeStatus

pytestmark = pytest.mark.unit


def test_registry_name_is_used_and_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cpu, "_registry_processor_name", lambda: "   Intel(R) Core(TM)  i7-1165G7 "
    )
    assert cpu._windows_model() == ("Intel(R) Core(TM) i7-1165G7", "windows-registry")


def test_unreadable_registry_falls_back_to_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cpu, "_registry_processor_name", lambda: "")
    monkeypatch.setattr(cpu.platform, "processor", lambda: "Intel64 Family 6")
    assert cpu._windows_model() == ("Intel64 Family 6", "platform")


def test_windows_cpu_probe_starts_no_process(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_process(*_a: Any, **_k: Any) -> None:
        raise AssertionError("the CPU probe must not start a process on Windows")

    monkeypatch.setattr(cpu, "_registry_processor_name", lambda: "Fake CPU")
    # psutil itself refuses a faked platform; its calls start no process anyway.
    monkeypatch.setattr(cpu, "_physical_cores", lambda logical: logical)
    monkeypatch.setattr(cpu, "_ram_bytes", lambda: 1)
    monkeypatch.setattr(subprocess, "Popen", no_process)
    monkeypatch.setattr(subprocess, "run", no_process)
    monkeypatch.setattr(sys, "platform", "win32")
    outcome = cpu.probe_cpu(Budget(5.0))
    monkeypatch.undo()
    assert outcome.status is ProbeStatus.OK, outcome.reason
    assert outcome.value is not None
    assert (outcome.value.model, outcome.value.source) == (
        "Fake CPU",
        "windows-registry",
    )


def test_not_windows_reads_no_registry() -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX")
    assert cpu._registry_processor_name() == ""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")
def test_real_registry_has_a_processor_name() -> None:
    from meshforge.agent.hardware.cpu_windows import processor_name_string

    assert processor_name_string().strip()
