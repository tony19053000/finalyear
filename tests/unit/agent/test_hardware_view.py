"""V0.3 G5 deliverable C: the agent-local hardware view and the NB-11 path leak.

Two things are pinned here:

* :func:`meshforge.agent.hardware.probe.guarded` must not put a filesystem path in
  ``ProbeOutcome.reason`` - the field's own docstring says "never a full user
  path", and ``hardware show`` prints it. The leak is reproduced with a **real**
  ``OSError`` raised by really opening a file that does not exist, not a mock.
* the rendering (text and JSON) is built from fabricated facts, so nothing here
  depends on this machine's CPU, GPUs, disks or hostname.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from meshforge.agent.hardware.cpu import CpuFacts
from meshforge.agent.hardware.disks import DiskFacts
from meshforge.agent.hardware.gpu import GpuFacts
from meshforge.agent.hardware.probe import (
    Budget,
    ProbeOutcome,
    ProbeStatus,
    failure_reason,
    guarded,
    sanitize_reason,
)
from meshforge.agent.hardware.summary import HardwareFacts
from meshforge.agent.hardware.view import facts_lines, facts_payload, format_bytes

pytestmark = pytest.mark.unit


# ------------------------------------------------------------------ NB-11 leak


def test_a_real_oserror_from_a_probe_never_leaks_its_path_into_reason(
    tmp_path: Path,
) -> None:
    """The defect: ``f"{type(exc).__name__}: {exc}"`` includes ``exc.filename``."""
    secret = tmp_path / "home" / "somebody" / "very-private-directory" / "node.json"

    def probe(_timeout_s: float) -> str:
        return secret.read_text(encoding="utf-8")  # real OSError, real path

    outcome = guarded("identity", Budget(1.0), 0.5, probe)

    assert outcome.status is ProbeStatus.ERROR
    assert outcome.reason.startswith("FileNotFoundError:")
    assert "No such file or directory" in outcome.reason
    assert "very-private-directory" not in outcome.reason
    assert str(secret) not in outcome.reason
    assert str(tmp_path) not in outcome.reason
    assert "node.json" not in outcome.reason


def test_a_real_permission_error_keeps_errno_and_drops_the_path(
    tmp_path: Path,
) -> None:
    if os.name == "nt" or os.geteuid() == 0:  # pragma: no cover - platform/CI
        pytest.skip("POSIX mode bits do not deny root or Windows the same way")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "x").write_text("secret", encoding="utf-8")
    locked.chmod(0o000)
    try:

        def probe(_timeout_s: float) -> str:
            return (locked / "x").read_text(encoding="utf-8")

        outcome = guarded("disks", Budget(1.0), 0.5, probe)
    finally:
        locked.chmod(0o700)

    assert outcome.status is ProbeStatus.ERROR
    assert "PermissionError" in outcome.reason
    assert "Errno 13" in outcome.reason
    assert str(tmp_path) not in outcome.reason


def test_a_real_subprocess_failure_does_not_leak_the_interpreters_path() -> None:
    """``CalledProcessError`` reprs the whole argv, interpreter path included.

    The path of this test run's interpreter contains a directory the operator did
    not ask to publish (and, here, a space - which is why quoted paths are removed
    whole before the unquoted pass).
    """

    def probe(_timeout_s: float) -> str:
        return subprocess.check_output(
            [sys.executable, "-c", "raise SystemExit(3)"], text=True
        )

    outcome = guarded("software", Budget(5.0), 4.0, probe)
    assert outcome.status is ProbeStatus.ERROR
    assert "CalledProcessError" in outcome.reason
    assert "non-zero exit status 3" in outcome.reason
    # Parts of one or two characters (the runner's "D:\\a\\...") are not
    # identifying and occur in any message.
    for part in [p for p in Path(sys.executable).parts[1:] if len(p) > 2]:
        assert part not in outcome.reason


def test_a_missing_executable_reports_errno_without_a_path() -> None:
    missing = "meshforge-does-not-exist-anywhere"

    def probe(_timeout_s: float) -> str:
        return subprocess.check_output([missing], text=True)

    outcome = guarded("gpu", Budget(1.0), 0.5, probe)
    assert outcome.status is ProbeStatus.ERROR
    assert "FileNotFoundError" in outcome.reason
    assert missing not in outcome.reason


@pytest.mark.parametrize(
    "text",
    [
        "cannot read '/home/alice/.config/meshforge/config.toml'",
        r"cannot read 'C:\Users\alice\AppData\Local\MeshForge\config.toml'",
        r"cannot read \\fileserver\share\alice\config.toml",
        "cannot read ~/.local/share/meshforge/identity/node.json",
    ],
)
def test_sanitize_reason_replaces_every_path_flavour(text: str) -> None:
    cleaned = sanitize_reason(text)
    assert "<path>" in cleaned
    assert "alice" not in cleaned
    assert "config.toml" not in cleaned
    assert cleaned.startswith("cannot read")


def test_sanitize_reason_keeps_ordinary_messages_intact() -> None:
    assert sanitize_reason("nvidia-smi exited 9 (50/100 devices)") == (
        "nvidia-smi exited 9 (50/100 devices)"
    )
    assert sanitize_reason("driver/library version mismatch") == (
        "driver/library version mismatch"
    )


def test_sanitize_reason_is_bounded() -> None:
    assert len(sanitize_reason("x" * 5_000)) == 200


def test_failure_reason_of_a_non_oserror_keeps_the_message() -> None:
    assert failure_reason(ValueError("bad CSV field 3")) == (
        "ValueError: bad CSV field 3"
    )


def test_timeout_outcomes_are_sanitised_too() -> None:
    def probe(_timeout_s: float) -> str:
        raise TimeoutError("nvidia-smi: timed out reading /home/alice/gpu/state")

    outcome = guarded("gpu", Budget(1.0), 0.5, probe)
    assert outcome.status is ProbeStatus.TIMEOUT
    assert "alice" not in outcome.reason
    assert "timed out" in outcome.reason


# --------------------------------------------------------------- the rendering


def _facts(*, gpus: bool = True, disks: bool = True) -> HardwareFacts:
    return HardwareFacts(
        cpu=CpuFacts(
            model="Fictional CPU 9000",
            physical_cores=8,
            logical_threads=16,
            ram_bytes=17_179_869_184,
            source="test",
        ),
        gpus=(
            (
                GpuFacts(
                    index=0,
                    model="Fictional GPU 1000",
                    vram_bytes=4_294_967_296,
                    driver_version="999.99",
                    compute_capability="8.6",
                    source="nvml",
                ),
            )
            if gpus
            else ()
        ),
        disks=(
            (
                DiskFacts(
                    mount="/data",
                    fstype="ext4",
                    total_bytes=1 << 40,
                    free_bytes=1 << 39,
                ),
            )
            if disks
            else ()
        ),
        capabilities=("docker", "torch"),
        probes=(
            ProbeOutcome("cpu", ProbeStatus.OK, value=1, duration_s=0.012),
            ProbeOutcome(
                "gpu", ProbeStatus.UNAVAILABLE, reason="not present", duration_s=0.1
            ),
        ),
        duration_s=1.5,
    )


def test_format_bytes() -> None:
    assert format_bytes(0) == "-"
    assert format_bytes(-5) == "-"
    assert format_bytes(512) == "512 B"
    assert format_bytes(17_179_869_184) == "16.0 GiB"


def test_facts_lines_show_every_probe_status_and_the_degraded_list() -> None:
    lines = facts_lines(_facts())
    text = "\n".join(lines)
    assert "Fictional CPU 9000" in text
    assert "8 physical / 16 logical" in text
    assert "16.0 GiB" in text
    assert "[0] Fictional GPU 1000" in text
    assert "/data" in text
    assert "docker, torch" in text
    assert "cpu" in text and "ok" in text
    assert "gpu" in text and "unavailable" in text and "not present" in text
    assert "degraded:" in text
    # No box-drawing characters: this must render on a Windows cp437 console.
    assert text.encode("cp437", errors="strict")


def test_facts_lines_on_a_machine_without_gpus_or_disks() -> None:
    text = "\n".join(facts_lines(_facts(gpus=False, disks=False)))
    assert "gpus: none detected" in text
    assert "disks: none detected" in text


def test_facts_payload_is_json_serialisable_and_complete() -> None:
    payload = facts_payload(_facts())
    encoded = json.loads(json.dumps(payload))
    assert set(encoded) == {
        "capabilities",
        "cpu",
        "degraded",
        "disks",
        "discovered_at",
        "duration_s",
        "gpus",
        "probes",
    }
    assert encoded["probes"][1] == {
        "name": "gpu",
        "status": "unavailable",
        "duration_s": 0.1,
        "reason": "not present",
    }
    assert encoded["degraded"] == ["gpu: unavailable: not present"]
    assert encoded["gpus"][0]["vram_bytes"] == 4_294_967_296


def test_an_oserror_without_a_filename_keeps_its_message() -> None:
    """Only the path-carrying form is rebuilt; ``OSError("device not ready")``
    has no filename and its message is the only information there is."""

    def probe(_timeout_s: float) -> str:
        raise OSError("device not ready")

    outcome = guarded("gpu", Budget(1.0), 0.5, probe)
    assert outcome.reason == "OSError: device not ready"
