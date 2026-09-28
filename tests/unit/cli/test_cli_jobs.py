"""V0.4 G5 D76: ``compute`` job plumbing without a controller."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from meshforge.cli.client import ControllerClient, ControllerError
from meshforge.cli.follow import ws_url
from meshforge.cli.job_commands import _key_ok, _new_key
from meshforge.cli.outcome import MAX_WORKLOAD_EXIT, job_exit_code
from meshforge.common.exit_codes import ExitCode

pytestmark = pytest.mark.unit

TOKEN = "t" * 43


@pytest.mark.parametrize(
    ("state", "failure", "code", "expected"),
    [
        ("succeeded", None, 0, 0),
        ("failed", "user_error", 1, 1),
        ("failed", "user_error", 3, 3),
        ("failed", "user_error", MAX_WORKLOAD_EXIT, MAX_WORKLOAD_EXIT),
        ("failed", "user_error", MAX_WORKLOAD_EXIT + 1, MAX_WORKLOAD_EXIT),
        ("failed", "user_error", 255, MAX_WORKLOAD_EXIT),
        ("failed", "user_error", None, MAX_WORKLOAD_EXIT),  # killed by a signal
        ("failed", "user_error", -9, MAX_WORKLOAD_EXIT),
        ("cancelled", "cancelled", None, ExitCode.JOB_CANCELLED),
        ("failed", "node_lost", None, ExitCode.JOB_FAILED),
        ("failed", "deadline_exceeded", None, ExitCode.JOB_FAILED),
        ("failed", "output_error", 0, ExitCode.JOB_FAILED),
    ],
)
def test_job_exit_code(
    state: str, failure: str | None, code: int | None, expected: int
) -> None:
    assert job_exit_code(state, failure, code) == expected


def test_fabric_codes_never_collide_with_a_clamped_workload_code() -> None:
    fabric = {ExitCode.JOB_FAILED, ExitCode.JOB_CANCELLED, ExitCode.INTERRUPTED}
    assert all(code > MAX_WORKLOAD_EXIT for code in fabric)
    assert len(fabric) == 3


def test_idempotency_key_bounds() -> None:
    assert _key_ok(_new_key()) and _key_ok("k" * 128)
    assert not _key_ok("k" * 129) and not _key_ok("")
    assert not _key_ok("has space") and not _key_ok("tab\t") and not _key_ok("é")
    assert _new_key() != _new_key()


def test_ws_url_keeps_the_token_out_of_it() -> None:
    assert ws_url("http://h:1", "job_x", stream="both") == (
        "ws://h:1/v1/jobs/job_x/logs/ws?stream=both"
    )
    assert ws_url("https://h:1/", "job_x", stream="stdout").startswith("wss://h:1/")


def test_client_sends_the_key_and_token_as_headers_only(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "HEAD":
            return httpx.Response(404)
        return httpx.Response(201, json={"job_id": "job_1", "state": "queued"})

    client = ControllerClient(
        "http://ctl:1",
        timeout_s=1,
        transport=httpx.MockTransport(handler),
        token=SecretStr(TOKEN),
    )
    assert client.has_blob("a" * 64) is False
    blob = tmp_path / "b"
    blob.write_bytes(b"abc")
    client.put_blob("a" * 64, blob)
    created = client.submit_job({"x": 1}, idempotency_key="k-1")
    assert created["job_id"] == "job_1"
    head, put, post = seen
    assert head.method == "HEAD"
    assert put.headers["content-length"] == "3" and put.read() == b"abc"
    assert post.headers["idempotency-key"] == "k-1"
    assert json.loads(post.content) == {"x": 1}
    for request in seen:
        assert TOKEN not in str(request.url)
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
    client.close()


def test_client_maps_a_refused_upload_to_a_typed_error(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            413, json={"error": {"code": "invalid_input", "message": "too big"}}
        )

    blob = tmp_path / "b"
    blob.write_bytes(b"x")
    with (
        ControllerClient(
            "http://ctl:1", timeout_s=1, transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(ControllerError) as info,
    ):
        client.put_blob("a" * 64, blob)
    assert "too big" in info.value.message
