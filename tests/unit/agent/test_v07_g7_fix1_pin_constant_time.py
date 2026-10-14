"""V0.7 G7 fix round 1 (D167, weak row CG-S5): the join-code CA pin is decided
by ``hmac.compare_digest``, not by ``==``.

``tests/unit/common/test_pki.py::test_short_fingerprint_matches_forms`` shows
the pin matches its forms; this pins the constant-time claim of the row. A spy
replaces ``compare_digest`` in ``common.pki``: the agent's ``pin_ca`` must call
it with the cert's fingerprint prefix and the code's pin, and its answer must
decide - a spy that says "no" turns the genuine CA into ``pin_mismatch``, so a
``==`` (which would ignore the spy) fails this test.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from meshforge.agent.enrollment import JoinError, JoinReason, pin_ca
from meshforge.common import pki
from meshforge.common.ids import new_fabric_id

pytestmark = [pytest.mark.unit, pytest.mark.security]


@pytest.fixture(scope="module")
def chain() -> list[Any]:
    ca = pki.create_ca(new_fabric_id())
    server = pki.issue_server_cert(ca, hosts=["127.0.0.1"])
    return [server.cert, ca.cert]


def _spy(monkeypatch: pytest.MonkeyPatch, answer: bool) -> list[tuple[bytes, bytes]]:
    calls: list[tuple[bytes, bytes]] = []

    def compare_digest(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return answer

    monkeypatch.setattr(
        pki, "hmac", types.SimpleNamespace(compare_digest=compare_digest)
    )
    return calls


def test_the_pin_is_compared_with_compare_digest(
    chain: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    pin = pki.short_fingerprint(chain[1])
    calls = _spy(monkeypatch, answer=True)
    assert pin_ca(chain, pin) == chain[1]
    assert (pin.encode(), pin.encode()) in calls


def test_the_compare_digest_answer_decides(
    chain: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    pin = pki.short_fingerprint(chain[1])
    calls = _spy(monkeypatch, answer=False)
    with pytest.raises(JoinError) as info:
        pin_ca(chain, pin)  # the genuine CA, refused because the spy said no
    assert info.value.reason is JoinReason.PIN_MISMATCH
    assert calls
