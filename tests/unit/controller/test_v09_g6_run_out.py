"""V0.9 G6 read-only glue: ``GET /v1/bench/campaigns/{id}/runs`` shows each
run's ``tries`` and ``last_failure`` (already in ``bench_run``, migration 0024)
- the Benchmarks page's Runs tab prints them (brief §5h item 3)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="needs the controller extra")

from meshforge.controller.api.bench_routes import RunOut, _run_out
from meshforge.controller.bench.store import RunRow

pytestmark = pytest.mark.unit


def _row(**over: object) -> RunRow:
    base: dict[str, object] = {
        "id": "run_1",
        "campaign_id": "camp_1",
        "seq": 3,
        "group_id": "grp_a",
        "config_id": "cfg_a",
        "repetition": 1,
        "family": "cpu.primes",
        "params": {"limit": 10_000_000},
        "seed": 7,
        "conditions": ("none",),
        "strategy": "local",
        "node_id": "node_a",
        "origin_node_id": "node_a",
        "state": "planned",
    }
    base.update(over)
    return RunRow(**base)  # type: ignore[arg-type]


def test_tries_and_last_failure_are_in_the_run_out() -> None:
    out = _run_out(_row(tries=2, last_failure="network_error: stream lost"))
    assert out.tries == 2
    assert out.last_failure == "network_error: stream lost"
    dumped = out.model_dump(mode="json")
    assert dumped["tries"] == 2
    assert dumped["last_failure"] == "network_error: stream lost"


def test_a_fresh_run_has_zero_tries_and_no_failure() -> None:
    out = _run_out(_row())
    assert (out.tries, out.last_failure) == (0, None)


def test_the_new_fields_are_additive_with_defaults() -> None:
    """An older client's view: every pre-G6 field is unchanged; the new ones default."""
    fields = RunOut.model_fields
    assert fields["tries"].default == 0
    assert fields["last_failure"].default is None
