"""V0.3 G2 review round 2 - NB3: a controller that stops because one of its own
tasks died (the lease timer, the HTTP server) exits ``FAILURE``, not 0.

No database and no sockets: ``ControllerRuntime`` is constructed but never
``_start``-ed; only its task supervision and exit-code plumbing are exercised
(``_serve``/``run_controller`` with ``_start``/``stop`` replaced by stubs).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import structlog

from meshforge.common.errors import ExitCode
from meshforge.controller.server import ControllerRuntime, run_controller
from meshforge.controller.settings import ControllerSection, ControllerSettings

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import os

    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


@pytest.fixture
def settings() -> ControllerSettings:
    return ControllerSettings(
        controller=ControllerSection(
            grpc_bind="127.0.0.1:0", http_bind="127.0.0.1:0", shutdown_drain_s=0.1
        )
    )


async def _boom() -> None:
    raise RuntimeError("lease timer died")


@pytest.mark.asyncio
async def test_owned_task_failure_requests_stop_and_marks_failure(
    settings: ControllerSettings,
) -> None:
    runtime = ControllerRuntime(settings)
    assert runtime.exit_code is ExitCode.OK
    with structlog.testing.capture_logs() as logs:
        runtime._spawn(_boom(), "lease-timer")
        await asyncio.wait_for(runtime.wait_for_stop(), 0.5)
    assert runtime.failed_task == "lease-timer"
    assert runtime.exit_code is ExitCode.FAILURE
    (failed,) = [e for e in logs if e["event"] == "controller.task_failed"]
    assert failed["task"] == "lease-timer" and failed["cause"] == "RuntimeError"
    await runtime.stop()  # nothing was started; must still be safe and idempotent
    await runtime.stop()


@pytest.mark.asyncio
async def test_cancelled_task_is_not_a_failure(settings: ControllerSettings) -> None:
    runtime = ControllerRuntime(settings)

    async def forever() -> None:
        await asyncio.sleep(3600)

    task = runtime._spawn(forever(), "http-server")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert runtime.failed_task is None and runtime.exit_code is ExitCode.OK
    assert not runtime._stop_requested.is_set()


def test_run_controller_exits_failure_after_a_task_crash(
    settings: ControllerSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_start(self: ControllerRuntime) -> None:
        self._spawn(_boom(), "lease-timer")

    async def fake_stop(self: ControllerRuntime) -> None:
        self._stopped = True

    monkeypatch.setattr(ControllerRuntime, "_start", fake_start)
    monkeypatch.setattr(ControllerRuntime, "stop", fake_stop)
    monkeypatch.setattr(ControllerRuntime, "install_signal_handlers", lambda self: None)
    with structlog.testing.capture_logs() as logs:
        code = run_controller(settings)
    assert code == int(ExitCode.FAILURE) != 0
    exited = [e for e in logs if e["event"] == "controller.exited_after_task_failure"]
    assert exited and exited[0]["task"] == "lease-timer"


def test_run_controller_exits_ok_after_a_requested_stop(
    settings: ControllerSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_start(self: ControllerRuntime) -> None:
        asyncio.get_running_loop().call_soon(self.request_stop)  # like SIGTERM

    async def fake_stop(self: ControllerRuntime) -> None:
        self._stopped = True

    monkeypatch.setattr(ControllerRuntime, "_start", fake_start)
    monkeypatch.setattr(ControllerRuntime, "stop", fake_stop)
    monkeypatch.setattr(ControllerRuntime, "install_signal_handlers", lambda self: None)
    assert run_controller(settings) == int(ExitCode.OK) == 0


def test_stop_passes_the_drain_budget_to_the_session_manager(
    settings: ControllerSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write chains get the same ``shutdown_drain_s`` the servers do."""
    seen: dict[str, Any] = {}

    class FakeManager:
        async def shutdown(self, *, drain_s: float) -> None:
            seen["drain_s"] = drain_s

    async def go() -> None:
        runtime = ControllerRuntime(settings)
        runtime.manager = FakeManager()  # type: ignore[assignment]
        await runtime.stop()

    asyncio.run(go())
    assert seen == {"drain_s": settings.controller.shutdown_drain_s}
