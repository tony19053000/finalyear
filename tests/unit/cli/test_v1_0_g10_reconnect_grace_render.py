"""V1.0 G10 (D445 r1 M1): ``compute jobs <id>`` shows an attempt waiting for
its node to reconnect, with the seconds left."""

from __future__ import annotations

import datetime as dt

import pytest

from meshforge.cli.render import job_detail_table, reconnect_grace_text

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 11, 11, 10, 0, tzinfo=dt.UTC)


def test_the_wait_and_its_seconds_left() -> None:
    attempt = {"reconnect_grace_until": "2026-11-11T10:00:42Z"}
    assert reconnect_grace_text(attempt, NOW) == "waiting for node to reconnect (42s)"
    late = {"reconnect_grace_until": "2026-11-11T09:59:00+00:00"}
    assert reconnect_grace_text(late, NOW) == "waiting for node to reconnect (0s)"


@pytest.mark.parametrize("value", [None, "", "garbage", 17])
def test_nothing_when_not_waiting(value: object) -> None:
    assert reconnect_grace_text({"reconnect_grace_until": value}, NOW) == ""
    assert reconnect_grace_text({}, NOW) == ""


def test_the_job_detail_has_a_waiting_row() -> None:
    until = (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=50)).isoformat()
    table = job_detail_table(
        {
            "job_id": "job_x",
            "state": "running",
            "attempts": [
                {
                    "attempt_id": "att_x",
                    "node_id": "node_x",
                    "state": "running",
                    "reconnect_grace_until": until,
                }
            ],
        }
    )
    fields = [str(c) for c in table.columns[0]._cells]
    values = [str(c) for c in table.columns[1]._cells]
    assert "waiting" in fields  # the cell is stripped of its indent
    assert values[fields.index("waiting")].startswith("waiting for node to reconnect (")
