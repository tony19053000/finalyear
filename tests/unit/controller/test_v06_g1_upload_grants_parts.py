"""V0.6 G1 / D114: :class:`UploadGrants` bookkeeping for resumable part uploads.

``has`` (a usable grant in this process), ``check``, ``begin_parts`` (the whole
blob reserved once per digest; max accepted, max+1 refused; another size for a
reserved digest refused), ``credit`` / ``release``; a re-mint carries what the
attempt already stored but drops in-flight reservations.
"""

from __future__ import annotations

import pytest

from meshforge.common.hashing import sha256_bytes
from meshforge.controller.upload_grants import GrantRefusal, UploadGrants

pytestmark = pytest.mark.unit

A = sha256_bytes(b"a")
B = sha256_bytes(b"b")


def _grants(now: list[float]) -> UploadGrants:
    return UploadGrants(clock=lambda: now[0])


def test_has_is_a_live_unclosed_grant() -> None:
    now = [100.0]
    grants = _grants(now)
    assert not grants.has("att")
    grants.mint("att", ttl_s=10, max_blobs=2, max_bytes=100)
    assert grants.has("att")
    now[0] = 110.0
    assert not grants.has("att")  # expired
    grants.mint("att", ttl_s=10, max_blobs=2, max_bytes=100)
    grants.close("att", grace_s=5)
    assert not grants.has("att")  # closed: takes no new upload


def test_check_names_the_attempt_or_refuses() -> None:
    grants = _grants([1.0])
    token = grants.mint("att", ttl_s=10, max_blobs=2, max_bytes=100)
    assert grants.check(token.get_secret_value()) == "att"
    with pytest.raises(GrantRefusal) as refused:
        grants.check("nope")
    assert refused.value.reason == "unknown"
    grants.close("att", grace_s=5)  # (grace 0 would expire it at once)
    with pytest.raises(GrantRefusal) as closed:
        grants.check(token.get_secret_value())
    assert closed.value.reason == "closed"


def test_begin_parts_reserves_the_blob_once_and_bounds_it() -> None:
    grants = _grants([1.0])
    token = grants.mint("att", ttl_s=10, max_blobs=2, max_bytes=100).get_secret_value()
    first = grants.begin_parts(token, A, 60)
    again = grants.begin_parts(token, A, 60)  # a later part of the same blob
    assert first.reserved == again.reserved == 60 and not again.free
    with pytest.raises(GrantRefusal) as over:
        grants.begin_parts(token, B, 41)  # 60 + 41 > 100
    assert over.value.reason == "byte_quota"
    ok = grants.begin_parts(token, B, 40)  # exactly 100
    assert ok.reserved == 40
    with pytest.raises(GrantRefusal) as mismatch:
        grants.begin_parts(token, A, 61)
    assert mismatch.value.reason == "size_mismatch"


def test_begin_parts_bounds_the_blob_count() -> None:
    grants = _grants([1.0])
    token = grants.mint("att", ttl_s=10, max_blobs=1, max_bytes=100).get_secret_value()
    grants.begin_parts(token, A, 10)
    with pytest.raises(GrantRefusal) as refused:
        grants.begin_parts(token, B, 10)
    assert refused.value.reason == "blob_quota"


def test_credit_makes_it_uploaded_and_free_release_gives_room_back() -> None:
    grants = _grants([1.0])
    token = grants.mint("att", ttl_s=10, max_blobs=2, max_bytes=100).get_secret_value()
    ticket = grants.begin_parts(token, A, 60)
    grants.credit(ticket, 60)
    assert grants.uploaded("att", A)
    assert grants.begin_parts(token, A, 60).free  # a repeat costs nothing
    other = grants.begin_parts(token, B, 40)
    grants.release(other)
    assert not grants.uploaded("att", B)
    assert grants.begin_parts(token, B, 40).reserved == 40  # room came back
    # a plain PUT of a released digest is not refused as a second reservation
    grants.release(grants.begin_parts(token, B, 40))
    put = grants.begin(token, B, 40)
    grants.finish(put, stored=True, size=40)
    assert grants.uploaded("att", B)


def test_a_re_mint_keeps_what_was_stored_not_what_was_in_flight() -> None:
    grants = _grants([1.0])
    old = grants.mint("att", ttl_s=10, max_blobs=3, max_bytes=100).get_secret_value()
    grants.credit(grants.begin_parts(old, A, 30), 30)
    grants.begin_parts(old, B, 70)  # in flight when the refresh comes
    new = grants.mint("att", ttl_s=10, max_blobs=3, max_bytes=100).get_secret_value()
    assert grants.uploaded("att", A)
    assert grants.begin_parts(new, B, 70).reserved == 70  # 30 + 70 = 100 fits
    with pytest.raises(GrantRefusal):
        grants.check(old)


@pytest.mark.parametrize("via", ["put", "parts"])
def test_an_upload_in_flight_across_a_re_mint_counts_for_the_new_grant(
    via: str,
) -> None:
    """A refresh replaced the grant while a PUT / complete under the old one
    was still running: the verified blob counts for the attempt (its DONE must
    not fail output_error); after a revoke it counts for nobody."""
    grants = _grants([1.0])
    old = grants.mint("att", ttl_s=10, max_blobs=3, max_bytes=100).get_secret_value()
    ticket = (
        grants.begin(old, A, 30) if via == "put" else grants.begin_parts(old, A, 30)
    )
    grants.mint("att", ttl_s=10, max_blobs=3, max_bytes=100)  # the refresh
    if via == "put":
        grants.finish(ticket, stored=True, size=30)
    else:
        grants.credit(ticket, 30)
    assert grants.uploaded("att", A)
    revoked = grants.begin(
        grants.mint("att2", ttl_s=10, max_blobs=3, max_bytes=100).get_secret_value(),
        B,
        5,
    )
    grants.revoke("att2")
    grants.finish(revoked, stored=True, size=5)
    assert not grants.uploaded("att2", B)
