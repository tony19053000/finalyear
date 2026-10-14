"""V0.8 G1 deliverable 2 (D171/D174): every ``/v1`` route declares its
response and the one error envelope in OpenAPI (the dashboard's generated
client depends on it), and the keyset cursor codec is strict."""

from __future__ import annotations

import base64
import datetime as dt
from typing import Any

import pytest

from meshforge.common.ids import new_job_id, validate_job_id
from meshforge.controller.api.errors import ApiError
from meshforge.controller.api.paging import decode_cursor, encode_cursor
from meshforge.controller.http import openapi_document

pytestmark = pytest.mark.unit

BINARY = {
    ("get", "/v1/blobs/{sha256}"),
    ("get", "/v1/jobs/{job_id}/logs"),
}
"""Routes whose success body is bytes (declared as application/octet-stream)."""
NO_BODY = {("head", "/v1/blobs/{sha256}")}


def _spec() -> dict[str, Any]:
    return openapi_document()


def test_every_route_declares_its_success_body_and_the_error_envelope() -> None:
    spec = _spec()
    checked = 0
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            responses = op["responses"]
            ok = [c for c in responses if c.startswith("2")]
            assert ok, (method, path)
            for code in ok:
                content = responses[code].get("content", {})
                if (method, path) in NO_BODY:
                    continue
                if (method, path) in BINARY:
                    assert "application/octet-stream" in content, (method, path)
                    continue
                schema = content.get("application/json", {}).get("schema", {})
                assert "$ref" in schema, (method, path, code, schema)
            for code in ("400", "401", "403", "404", "503"):
                error = responses[code]["content"]["application/json"]["schema"]
                assert error["$ref"].endswith("/ErrorEnvelope"), (method, path, code)
            checked += 1
    assert checked >= 40


def test_cursor_round_trip_and_strictness() -> None:
    job = str(new_job_id())
    at = dt.datetime(2026, 10, 15, 12, 0, 0, 123456, tzinfo=dt.UTC)
    cursor = encode_cursor(at, job)
    assert decode_cursor(cursor, validate_id=validate_job_id) == (at, job)
    bad = [
        "",
        "x" * 161,
        "!!!",
        encode_cursor(at, "not-a-job"),
        encode_cursor(at, job)[:-4],
        "djJ8MjAyNi0xMC0xNVQxMjowMDowMCswMDowMHxqb2Jf",  # v2|...
    ]
    raw = f"v1|{at.replace(tzinfo=None).isoformat()}|{job}".encode()
    naive = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    bad.append(naive)  # a time without a zone
    for text in bad:
        with pytest.raises(ApiError):
            decode_cursor(text, validate_id=validate_job_id)
