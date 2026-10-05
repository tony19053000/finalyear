"""V0.6 G3 tester: adversarial exclusion / retry-policy cases (D110).

Derived from the D110 text, not the implementation: 1 node vs N nodes, every
node excluded, window expiry, pinned retries, boundaries of max_attempts and
backoff, loss vs on_lost:false.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from meshforge.common.errors import FailureType
from meshforge.controller.jobs.retry import (
    MAX_BACKOFF_S,
    RetryReason,
    backoff_s,
    retry_verdict,
)
from meshforge.controller.planner import (
    ClusterSnapshot,
    NodeSnapshot,
    PastFailure,
    PlanRequest,
    TreeNeed,
    exclusions,
    plan,
)

pytestmark = pytest.mark.unit

GIB = 1024**3
EXEC = frozenset({"exec", "exec-subprocess"})
NOW = dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.UTC)
TREE = "sha256:" + "a" * 64


def node(node_id: str, **kw: Any) -> NodeSnapshot:
    base: dict[str, Any] = {
        "state": "online",
        "connected": True,
        "capabilities": EXEC,
        "cpu_slots": 1,
    }
    base.update(kw)
    return NodeSnapshot(node_id=node_id, **base)


def request(**kw: Any) -> PlanRequest:
    base: dict[str, Any] = {
        "job_id": "job",
        "attempt_no": 2,
        "gpu": "none",
        "vram_bytes": None,
        "ram_bytes": None,
        "cpus": None,
        "base": "python-cpu",
        "env_key": "e",
        "trees": (TreeNeed(TREE, 1),),
    }
    base.update(kw)
    return PlanRequest(**base)


def failed(node_id: str, kind: str, ago_s: float = 1.0) -> PastFailure:
    return PastFailure(node_id, kind, NOW - dt.timedelta(seconds=ago_s), ())


def test_every_node_excluded_does_not_stall_the_job_for_the_window() -> None:
    """Attempt 1 lost on a, attempt 2 lost on b: attempt 3 must still be
    placeable (nothing else exists) rather than wait 600 s."""
    snap = ClusterSnapshot(nodes=(node("a"), node("b")))
    ex = exclusions(
        [failed("a", "node_lost"), failed("b", "timeout")],
        snap.nodes,
        now=NOW,
        exclude_s=600,
    )
    assert set(ex) == {"a", "b"}
    decision = plan(request(excluded=ex), snap, "cost_model")
    assert decision.chosen is not None


def test_three_nodes_two_excluded_picks_the_third_even_if_it_ranks_last() -> None:
    snap = ClusterSnapshot(nodes=(node("a"), node("b"), node("c", used_cpu_slots=0)))
    ex = {"a": "node_lost", "b": "node_lost"}
    for policy in ("cost_model", "least_loaded", "random"):
        decision = plan(request(excluded=ex), snap, policy)
        assert decision.chosen is not None and decision.chosen.node_id == "c"


def test_exclusion_window_expires_exactly_at_the_boundary() -> None:
    nodes = (node("a"), node("b"))
    inside = exclusions([failed("a", "timeout", 599.0)], nodes, now=NOW, exclude_s=600)
    at = exclusions([failed("a", "timeout", 600.0)], nodes, now=NOW, exclude_s=600)
    assert inside == {"a": "timeout"}
    assert at == {}


def test_a_failure_with_no_end_time_is_still_excluded() -> None:
    nodes = (node("a"),)
    got = exclusions(
        [PastFailure("a", "node_lost", None, ())], nodes, now=NOW, exclude_s=600
    )
    assert got == {"a": "node_lost"}


@pytest.mark.parametrize("kind", ["user_error", "oom", "env_error", "output_error"])
def test_non_listed_types_exclude_nothing(kind: str) -> None:
    assert (
        exclusions([failed("a", kind)], (node("a"), node("b")), now=NOW, exclude_s=600)
        == {}
    )


def test_a_pinned_retry_stays_on_its_node_even_with_a_better_one() -> None:
    snap = ClusterSnapshot(nodes=(node("a", used_cpu_slots=0), node("b")))
    decision = plan(
        request(pinned_node_id="b", excluded={"b": "node_lost"}), snap, "cost_model"
    )
    assert decision.chosen is not None and decision.chosen.node_id == "b"


@pytest.mark.parametrize("max_attempts", [1, 2, 3, 10])
def test_exhaustion_boundary_is_exactly_max_attempts(max_attempts: int) -> None:
    for used in range(1, max_attempts + 1):
        v = retry_verdict(
            failure_type=FailureType.TIMEOUT,
            retryable=None,
            lost=False,
            attempts_used=used,
            max_attempts=max_attempts,
            base_backoff_s=1.0,
            on_lost_retry=True,
            cancel_requested=False,
        )
        expected = (
            RetryReason.RETRY if used < max_attempts else RetryReason.RETRIES_EXHAUSTED
        )
        assert v.reason is expected, (used, max_attempts)


def test_on_lost_false_only_blocks_losses_not_other_retryable_failures() -> None:
    kw: dict[str, Any] = {
        "retryable": None,
        "attempts_used": 1,
        "max_attempts": 3,
        "base_backoff_s": 0.0,
        "on_lost_retry": False,
        "cancel_requested": False,
    }
    assert (
        retry_verdict(failure_type=FailureType.TIMEOUT, lost=False, **kw).reason
        is RetryReason.RETRY
    )
    assert (
        retry_verdict(failure_type=FailureType.NODE_LOST, lost=True, **kw).reason
        is RetryReason.ON_LOST_DISABLED
    )
    # an agent-reported node_lost (agent_shutdown) is a loss too
    assert (
        retry_verdict(failure_type=FailureType.NODE_LOST, lost=False, **kw).reason
        is RetryReason.ON_LOST_DISABLED
    )


def test_a_loss_on_the_last_attempt_is_exhausted_not_silently_failed() -> None:
    v = retry_verdict(
        failure_type=FailureType.NODE_LOST,
        retryable=None,
        lost=True,
        attempts_used=3,
        max_attempts=3,
        base_backoff_s=5.0,
        on_lost_retry=True,
        cancel_requested=False,
    )
    assert v.reason is RetryReason.RETRIES_EXHAUSTED


def test_backoff_sequence_and_cap_with_large_attempt_numbers() -> None:
    assert [backoff_s(n, 5.0) for n in (1, 2, 3, 4)] == [5.0, 10.0, 20.0, 40.0]
    assert backoff_s(10, 5.0) == MAX_BACKOFF_S
    assert backoff_s(10_000, 5.0) == MAX_BACKOFF_S
    assert backoff_s(1, 10_000.0) == MAX_BACKOFF_S
    assert backoff_s(0, 5.0) == 5.0
    assert backoff_s(3, 0.0) == 0.0
    assert backoff_s(3, -1.0) == 0.0
