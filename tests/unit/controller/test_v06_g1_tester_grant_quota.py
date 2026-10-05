"""V0.6 G1 tester round (D114): quota invariants of :class:`UploadGrants` under
re-mint / duplicate / out-of-order use. Criterion: an attempt never has more
than ``max_bytes`` / ``max_blobs`` stored under its grants, however the grant
was re-minted."""

from __future__ import annotations

import pytest

from meshforge.common.hashing import sha256_bytes
from meshforge.controller.upload_grants import GrantRefusal, UploadGrants

pytestmark = pytest.mark.unit

A, B, C = (sha256_bytes(x) for x in (b"a", b"b", b"c"))


def _grants() -> UploadGrants:
    return UploadGrants(clock=lambda: 1.0)


def _total(grants: UploadGrants, attempt: str) -> int:
    key = grants._by_attempt[attempt]
    return sum(grants._grants[key].uploaded.values())


def test_credit_twice_and_release_after_credit_are_idempotent() -> None:
    grants = _grants()
    tok = grants.mint("att", ttl_s=10, max_blobs=2, max_bytes=100).get_secret_value()
    ticket = grants.begin_parts(tok, A, 60)
    grants.credit(ticket, 60)
    grants.credit(ticket, 60)  # duplicate
    grants.release(ticket)  # late release of a credited ticket
    assert grants.uploaded("att", A)
    assert _total(grants, "att") == 60
    key = grants._by_attempt["att"]
    assert grants._grants[key].reserved_bytes == 0
    with pytest.raises(GrantRefusal):  # 60 stored + 41 > 100
        grants.begin_parts(tok, B, 41)


def test_release_twice_does_not_give_back_more_than_was_reserved() -> None:
    grants = _grants()
    tok = grants.mint("att", ttl_s=10, max_blobs=3, max_bytes=100).get_secret_value()
    t_a = grants.begin_parts(tok, A, 50)
    t_b = grants.begin_parts(tok, B, 50)
    grants.release(t_a)
    grants.release(t_a)  # duplicate
    with pytest.raises(GrantRefusal):  # B still reserved: 50 + 51 > 100
        grants.begin_parts(tok, C, 51)
    grants.release(t_b)
    assert grants.begin_parts(tok, C, 100).reserved == 100


def test_a_successor_credit_never_pushes_the_attempt_over_its_byte_quota() -> None:
    grants = _grants()
    old = grants.mint("att", ttl_s=10, max_blobs=4, max_bytes=100).get_secret_value()
    in_flight = grants.begin_parts(old, A, 100)  # whole quota reserved
    new = grants.mint("att", ttl_s=10, max_blobs=4, max_bytes=100).get_secret_value()
    grants.begin_parts(new, B, 100)  # new grant knows nothing of A's 100 bytes
    grants.credit(in_flight, 100)  # A finishes under the replaced grant
    key = grants._by_attempt["att"]
    grant = grants._grants[key]
    assert sum(grant.uploaded.values()) + grant.reserved_bytes <= 100


def test_a_revoked_or_closed_attempt_is_credited_nothing() -> None:
    grants = _grants()
    tok = grants.mint("att", ttl_s=10, max_blobs=2, max_bytes=100).get_secret_value()
    ticket = grants.begin_parts(tok, A, 10)
    grants.revoke("att")
    grants.credit(ticket, 10)
    assert not grants.uploaded("att", A)
    tok2 = grants.mint("att2", ttl_s=10, max_blobs=2, max_bytes=100).get_secret_value()
    t2 = grants.begin_parts(tok2, A, 10)
    grants.close("att2", grace_s=5)
    with pytest.raises(GrantRefusal) as refused:
        grants.begin_parts(tok2, B, 10)
    assert refused.value.reason == "closed"
    grants.credit(t2, 10)  # an in-flight complete finishing after close: no crash


def test_remint_many_times_keeps_stored_blobs_and_never_inflates_usage() -> None:
    grants = _grants()
    tok = grants.mint("att", ttl_s=10, max_blobs=2, max_bytes=100).get_secret_value()
    grants.credit(grants.begin_parts(tok, A, 40), 40)
    for _ in range(50):  # a flapping controller / unacked refreshes
        tok = grants.mint(
            "att", ttl_s=10, max_blobs=2, max_bytes=100
        ).get_secret_value()
        assert grants.begin_parts(tok, A, 40).free
    key = grants._by_attempt["att"]
    assert grants._grants[key].used_bytes == 40
    assert len(grants) == 1  # old grants are dropped, not accumulated
