"""V0.7 G7 tester r1: what does the constant-time AST scan miss?

An independent scan of ``src/`` (names pin|fingerprint|code|digest|key|bearer|
credential|hash|sig|tag|nonce|serial ... plus ``in`` membership) found NO real
credential compared with ``==`` that the G7 scan misses - every hit is public
data (CA/cert fingerprints, serials, session ids, SQL column filters). These
tests pin the *scanner's* former blind spots (TQ-6): they were strict xfails
and pass since fix round 1 (D167) widened the scan.
"""

from __future__ import annotations

import pytest
from test_v07_g7_constant_time_scan import flagged

pytestmark = [pytest.mark.unit, pytest.mark.security]


def test_control_the_obvious_form_is_flagged() -> None:
    assert flagged("presented_token == expected_token")


@pytest.mark.parametrize(
    "source",
    [
        "presented == expected",
        "bearer == stored",
        "fingerprint != pinned_fingerprint",
        "pin == other_pin",
        "code == expected_code",
        "h.digest() == stored",
        "credential == other",
        "hexdigest != wanted_hex",
        "sig == expected_sig",
        "cookie == stored_cookie",
    ],
)
def test_other_names_for_a_credential_are_flagged(source: str) -> None:
    assert flagged(source)


@pytest.mark.parametrize(
    "source",
    ["presented_token in valid_tokens", "token not in allowed", "secret in {a, b}"],
)
def test_membership_of_a_credential_is_flagged(source: str) -> None:
    assert flagged(source)


def test_a_credential_compared_with_a_literal_is_flagged() -> None:
    assert flagged('password == "hunter2"')
