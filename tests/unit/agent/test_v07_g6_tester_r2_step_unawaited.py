"""V0.7 G6 tester r2 (T-13, minor, pre-G6): `Executor._step` raises
`_StoppedError` BEFORE awaiting the awaitable it was given, so a stop that
arrives just before staging leaves `fetch_bundle(...)` as a never-awaited
coroutine (`RuntimeWarning: coroutine 'fetch_bundle' was never awaited`; an
error under `filterwarnings = error`). Found by cancelling Docker jobs at
random moments."""

from __future__ import annotations

import asyncio
import gc
import warnings
from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.agent.execution.executor import Executor, _StoppedError

pytestmark = pytest.mark.unit


@pytest.mark.xfail(
    strict=True,
    reason="T-13 (minor, pre-G6): _step drops the awaitable un-awaited when a "
    "stop was already requested",
)
def test_a_stop_before_a_step_does_not_leave_an_unawaited_coroutine() -> None:
    async def work() -> int:
        return 1

    attempt: Any = SimpleNamespace(stop_reason="cancel", step=None)

    async def main() -> None:
        with pytest.raises(_StoppedError):
            await Executor._step(None, attempt, work())  # type: ignore[arg-type]

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        asyncio.run(main())
        gc.collect()
    assert [str(w.message) for w in caught if "never awaited" in str(w.message)] == []
