"""V0.9 G3 lane B: the bench store's literal copies agree with meshforge.bench."""

from __future__ import annotations

import pytest

from meshforge.bench.campaign import Strategy
from meshforge.controller.store.models.bench import RUN_STATES, RUN_STRATEGIES
from meshforge.controller.telemetry.schema import STRATEGIES

pytestmark = pytest.mark.unit


def test_store_strategies_match_the_bench_enum_and_the_record() -> None:
    assert tuple(s.value for s in Strategy) == RUN_STRATEGIES == STRATEGIES


def test_run_states() -> None:
    assert RUN_STATES == ("planned", "submitted", "done", "failed", "skipped")


def test_queue_reads_bench_prefix_copy_matches_the_submitter() -> None:
    from meshforge.controller.bench.submit import IDEMPOTENCY_PREFIX
    from meshforge.controller.jobs.queue_reads import BENCH_IDEMPOTENCY_PREFIX

    assert BENCH_IDEMPOTENCY_PREFIX == IDEMPOTENCY_PREFIX
