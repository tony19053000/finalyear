"""V0.8 G1 fix round 1 (D182 T-4): instants that have no UTC form are a 400
``invalid_input``, never a database error."""

from __future__ import annotations

import base64
import datetime as dt

import pytest

from meshforge.controller.api.errors import ApiError
from meshforge.controller.api.paging import decode_cursor, encode_cursor, utc_instant

pytestmark = pytest.mark.unit

_JOB = "job_01J0000000000000000000000A"


def _b64(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()


def _accept(_: str) -> object:
    return None


@pytest.mark.parametrize(
    "text", ["0001-01-01T00:00:00+23:59", "9999-12-31T23:59:59.999999-23:59"]
)
def test_an_instant_outside_the_utc_range_is_invalid_input(text: str) -> None:
    with pytest.raises(ApiError) as caught:
        utc_instant(dt.datetime.fromisoformat(text), name="created_after")
    assert caught.value.http_status == 400
    assert caught.value.failure_type.value == "invalid_input"
    with pytest.raises(ApiError) as caught:
        decode_cursor(_b64(f"v1|{text}|{_JOB}"), validate_id=_accept)
    assert caught.value.http_status == 400


def test_a_naive_instant_is_invalid_input() -> None:
    with pytest.raises(ApiError) as caught:
        utc_instant(dt.datetime(2026, 1, 1), name="created_before")  # noqa: DTZ001
    assert caught.value.http_status == 400


def test_in_range_instants_come_back_as_the_same_instant_in_utc() -> None:
    zone = dt.timezone(dt.timedelta(hours=-7))
    value = dt.datetime(2026, 5, 5, 5, 5, 5, 123456, tzinfo=zone)
    assert utc_instant(value, name="x") == value
    assert utc_instant(value, name="x").utcoffset() == dt.timedelta(0)
    at, row = decode_cursor(encode_cursor(value, _JOB), validate_id=_accept)
    assert (at, row) == (value, _JOB)
    edge = dt.datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=dt.UTC)
    assert decode_cursor(encode_cursor(edge, _JOB), validate_id=_accept)[0] == edge
