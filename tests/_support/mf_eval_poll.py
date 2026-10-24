"""V0.15 G6b (D378): ``POST /v1/planner/eval-reports`` is start-then-poll.

Test helpers for the API's two steps: :func:`bg` adds ``background: true``
to a request body (a body without it is refused, 400
``eval_requires_background``); :func:`wait_for_report` polls ``GET
/v1/planner/eval-reports/{id}`` until the report is no longer ``running``
and returns that answer.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

__all__ = ["EVAL_PATH", "bg", "wait_for_report"]

EVAL_PATH = "/v1/planner/eval-reports"


def bg(body: Mapping[str, Any]) -> dict[str, Any]:
    """``body`` with ``background: true``."""
    return {**body, "background": True}


async def wait_for_report(
    get: Callable[[str], Awaitable[httpx.Response]],
    report_id: int,
    *,
    timeout_s: float = 240.0,
    poll_s: float = 0.05,
) -> httpx.Response:
    """The first ``GET .../{report_id}`` answer whose ``status`` is not
    ``running`` (``AssertionError`` after ``timeout_s``)."""
    deadline = time.monotonic() + timeout_s
    while True:
        got = await get(f"{EVAL_PATH}/{report_id}")
        assert got.status_code == 200, got.text
        if got.json()["status"] != "running":
            return got
        assert time.monotonic() < deadline, f"report {report_id} still running"
        await asyncio.sleep(poll_s)
