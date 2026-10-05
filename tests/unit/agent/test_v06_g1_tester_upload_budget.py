"""V0.6 G1 tester round (D114): edges of the agent's upload retry budget."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_executor import (
    Reports,
    WritingRuntime,
    _executor,
    _settle,
    _start,
    _with_outputs,
)
from test_v06_g1_upload_retry import _down, _final

from meshforge.common.proto_codec import (
    attempt_state_from_proto,
    failure_type_from_proto,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class LogsDownOutputFlaky:
    """Log uploads always fail (each failure costs ``cost`` seconds of the fake
    clock); the output upload fails once transiently, then works."""

    def __init__(self, now: list[float], cost: float) -> None:
        self.now, self.cost, self.output_tries = now, cost, 0

    async def __call__(self, _b: str, _d: Any, _t: str, path: Path) -> int:
        if path.suffix == ".log":
            self.now[0] += self.cost
            raise _down()
        self.output_tries += 1
        if self.output_tries == 1:
            raise _down()
        return 1


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT (minor): final logs are best-effort but share the attempt's one "
    "upload_retry_s budget; a controller outage that exhausts it on the logs "
    "leaves the OUTPUT upload zero retries (a single transient error -> "
    "output_error) and delays every final report by up to upload_retry_s",
)
async def test_best_effort_log_retries_do_not_consume_the_output_budget(
    tmp_path: Path,
) -> None:
    now = [0.0]
    uploads, reports = LogsDownOutputFlaky(now, cost=6.0), Reports()
    executor, digest = _executor(
        tmp_path,
        WritingRuntime(),
        reports,
        upload=uploads,
        execution={"upload_retry_s": 5},
        clock=lambda: now[0],
    )
    msg = _start(digest)
    _with_outputs(msg)
    executor.handle(msg)
    await _settle(executor)
    assert attempt_state_from_proto(_final(reports).state).value == "done"
    await executor.close()


async def test_exhausted_budget_ends_in_a_retryable_output_error_not_a_hang(
    tmp_path: Path,
) -> None:
    now = [0.0]

    class AlwaysDown:
        tries = 0

        async def __call__(self, *_a: Any) -> int:
            self.tries += 1
            now[0] += 3.0
            raise _down()

    uploads, reports = AlwaysDown(), Reports()
    executor, digest = _executor(
        tmp_path,
        WritingRuntime(),
        reports,
        upload=uploads,
        execution={"upload_retry_s": 5},
        clock=lambda: now[0],
    )
    msg = _start(digest)
    _with_outputs(msg)
    executor.handle(msg)
    await _settle(executor)
    final = _final(reports)
    assert attempt_state_from_proto(final.state).value == "failed"
    assert failure_type_from_proto(final.failure.failure_type).value == "output_error"
    assert final.failure.retryable
    assert uploads.tries < 20  # bounded
    await executor.close()
