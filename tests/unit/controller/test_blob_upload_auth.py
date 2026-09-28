"""V0.4 G5 fix round (reviewer N6): who may ``PUT /v1/blobs`` (``_authorize_upload``).

The redundant ``grants.owns(token)`` pre-check is gone; ``UploadGrants.begin``
is the one authority. Pinned here: an unknown token and a missing header are
``401`` with no log line (unauthenticated noise must not flood the log); an
expired grant is ``401`` and logged; the fabric token bypasses grants; a live
grant yields a ticket.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="needs the controller extra")

import structlog
from starlette.requests import Request

from meshforge.common.hashing import sha256_bytes
from meshforge.controller.api.auth import ApiAuth
from meshforge.controller.api.blob_routes import _authorize_upload
from meshforge.controller.api.errors import ApiError
from meshforge.controller.upload_grants import UploadGrants

pytestmark = pytest.mark.unit

DIGEST = sha256_bytes(b"payload")


class _Verifier:
    def verify(self, token: str | None) -> bool:
        return token == "fabric-token"


def _request(token: str | None) -> Request:
    headers = [] if token is None else [(b"authorization", f"Bearer {token}".encode())]
    return Request({"type": "http", "method": "PUT", "headers": headers})


def _auth(now: list[float]) -> tuple[ApiAuth, UploadGrants]:
    grants = UploadGrants(clock=lambda: now[0])
    auth = ApiAuth(verifier=_Verifier(), upload_grants=grants)  # type: ignore[arg-type]
    return auth, grants


def _refused(
    auth: ApiAuth, token: str | None
) -> tuple[ApiError, list[dict[str, object]]]:
    with structlog.testing.capture_logs() as logged, pytest.raises(ApiError) as caught:
        _authorize_upload(_request(token), auth, DIGEST, 7)
    return caught.value, logged


@pytest.mark.parametrize("token", [None, "not-a-grant-" + "x" * 32])
def test_unknown_or_missing_token_is_401_without_a_log_line(token: str | None) -> None:
    auth, grants = _auth([100.0])
    grants.mint("att_1", ttl_s=60, max_blobs=3, max_bytes=100)
    error, logged = _refused(auth, token)
    assert error.http_status == 401
    assert logged == []


def test_expired_grant_is_401_and_logged() -> None:
    now = [100.0]
    auth, grants = _auth(now)
    token = grants.mint("att_1", ttl_s=10, max_blobs=3, max_bytes=100)
    now[0] = 200.0
    error, logged = _refused(auth, token.get_secret_value())
    assert error.http_status == 401
    assert [e["reason"] for e in logged] == ["expired"]
    assert token.get_secret_value() not in repr(logged)


def test_no_grants_configured_is_401() -> None:
    auth = ApiAuth(verifier=_Verifier())  # type: ignore[arg-type]
    error, _ = _refused(auth, "anything")
    assert error.http_status == 401


def test_fabric_token_bypasses_grants_and_a_live_grant_gets_a_ticket() -> None:
    auth, grants = _auth([100.0])
    assert _authorize_upload(_request("fabric-token"), auth, DIGEST, 7) is None
    token = grants.mint("att_1", ttl_s=60, max_blobs=3, max_bytes=100)
    ticket = _authorize_upload(_request(token.get_secret_value()), auth, DIGEST, 7)
    assert ticket is not None and ticket.attempt_id == "att_1"
