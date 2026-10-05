"""V0.5 G5 tester round 2 (T3): which trees the dispatcher hands the scrubber
after an ``integrity_error`` - the project tree and every named-input tree, even
when listing the inputs fails; nothing for other outcomes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.common.errors import FailureType
from meshforge.controller.jobs.dispatcher import Dispatcher
from meshforge.controller.jobs.execution_store import StatusKind

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _dispatcher(inputs: Any, seen: list[str]) -> Dispatcher:
    d = object.__new__(Dispatcher)
    d._on_integrity_failure = seen.append  # type: ignore[attr-defined]
    d._job_inputs = inputs  # type: ignore[method-assign,assignment]
    return d


def _case(
    *,
    kind: StatusKind = StatusKind.APPLIED,
    ended: bool = True,
    failure: FailureType = FailureType.INTEGRITY_ERROR,
    tree: str | None = "P",
) -> tuple[Any, Any]:
    job = SimpleNamespace(id="job_1", tree_id=tree)
    outcome = SimpleNamespace(kind=kind, ended=ended, job=job)
    report = SimpleNamespace(failure_type=failure, attempt_id="att_1")
    return outcome, report


async def test_project_and_every_input_tree_once_in_order() -> None:
    seen: list[str] = []

    async def inputs(job: Any) -> list[Any]:
        return [SimpleNamespace(tree_id=t) for t in ("I1", "P", "I2", "I1")]

    d = _dispatcher(inputs, seen)
    o, r = _case()
    await d._suspect_trees(o, r)
    assert seen == ["P", "I1", "I2"]


@pytest.mark.parametrize("exc", [RuntimeError("db down"), OSError("x"), KeyError("k")])
async def test_input_lookup_failure_still_scrubs_the_project_tree(
    exc: Exception,
) -> None:
    seen: list[str] = []

    async def inputs(job: Any) -> list[Any]:
        raise exc

    d = _dispatcher(inputs, seen)
    o, r = _case()
    await d._suspect_trees(o, r)  # must not raise
    assert seen == ["P"]


@pytest.mark.parametrize(
    "case",
    [
        {"kind": StatusKind.DUPLICATE},
        {"kind": StatusKind.FENCED},
        {"ended": False},
        {"failure": FailureType.NODE_LOST},
        {"tree": None},
    ],
)
async def test_no_scrub_unless_a_tree_attempt_really_ended_integrity_error(
    case: dict[str, Any],
) -> None:
    seen: list[str] = []
    called: list[int] = []

    async def inputs(job: Any) -> list[Any]:
        called.append(1)
        return [SimpleNamespace(tree_id="I")]

    d = _dispatcher(inputs, seen)
    o, r = _case(**case)
    await d._suspect_trees(o, r)
    assert seen == [] and called == []


async def test_a_job_with_no_job_record_is_ignored() -> None:
    seen: list[str] = []
    d = _dispatcher(None, seen)
    o, r = _case()
    o.job = None
    await d._suspect_trees(o, r)
    assert seen == []
