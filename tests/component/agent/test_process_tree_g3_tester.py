"""V0.4 G3 tester round 1: adversarial additions to `meshforge.agent.process`
not covered by the implementer's test_process_tree.py -
double-kill / kill-after-natural-exit idempotency (roadmap 0.4.17 item 6 asks
for `kill_tree` of an already-exited process to be safe), and terminate_group
on an already-exited process.
"""

from __future__ import annotations

import asyncio
import os

import pytest

psutil = pytest.importorskip("psutil", reason="needs the agent extra (pid checks)")

from meshforge.agent.process import spawn  # noqa: E402 - after the importorskip guard

pytestmark = pytest.mark.timeout(60)


def _env() -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "")}
    return {k: v for k, v in env.items() if v}


def test_kill_now_after_natural_exit_does_not_raise_and_is_idempotent() -> None:
    async def scenario() -> None:
        handle = await spawn(
            [__import__("sys").executable, "-c", "print('done')"], env=_env()
        )
        await handle.reap()
        assert handle.returncode == 0
        # the process already exited and was reaped/released; killing it now
        # must be a silent no-op, not an exception, and safe to call twice.
        handle.kill_now()
        handle.kill_now()
        handle.terminate_group()

    asyncio.run(scenario())


def test_kill_now_called_twice_on_a_running_process_is_safe() -> None:
    async def scenario() -> int | None:
        handle = await spawn(
            [__import__("sys").executable, "-c", "import time; time.sleep(30)"],
            env=_env(),
        )
        handle.kill_now()
        handle.kill_now()  # second call while the first kill is still landing
        await handle.reap()
        return handle.returncode

    code = asyncio.run(scenario())
    assert code is not None  # it did exit (forced)


def test_terminate_group_then_kill_now_on_an_already_terminated_process() -> None:
    async def scenario() -> None:
        handle = await spawn(
            [__import__("sys").executable, "-c", "import time; time.sleep(30)"],
            env=_env(),
        )
        handle.terminate_group()
        await asyncio.sleep(0.3)
        handle.kill_now()
        await handle.reap()
        assert handle.killed is True

    asyncio.run(scenario())
