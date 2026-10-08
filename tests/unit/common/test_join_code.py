"""``common.join_code`` (V0.7 G2, D135): the human join code.

Round trip for IPv4, IPv6 and host names; the reading forms a human produces
(case, spaces, look-alikes); every refusal typed (truncated, bad checksum, wrong
version, damaged fields, unusable address) and never echoing the code; the token
kept out of ``repr``; ``parse_ttl``. Mutation checks: the checksum and version
guards each have a test that fails without them.
"""

from __future__ import annotations

import base64
import hashlib
import re

import pytest

from meshforge.common import join_code
from meshforge.common.join_code import (
    JoinCode,
    JoinCodeError,
    JoinCodeReason,
    decode,
    encode,
    new_token,
    parse_ttl,
    token_hash,
)

PIN = "0123456789abcdef0123456789abcdef"


def _code(host: str = "192.168.1.10", port: int = 47101) -> JoinCode:
    return JoinCode(host=host, port=port, token=new_token(), ca_pin=PIN)


def _raw(text: str) -> bytes:
    letters = text.removeprefix("mf1-").replace("-", "").upper()
    return base64.b32decode(letters + "=" * (-len(letters) % 8))


def _text(payload: bytes) -> str:
    letters = base64.b32encode(payload).decode().rstrip("=").lower()
    return "mf1-" + "-".join(letters[i : i + 5] for i in range(0, len(letters), 5))


def _with_checksum(body: bytes) -> str:
    return _text(body + hashlib.sha256(body).digest()[:4])


@pytest.mark.parametrize(
    "host",
    ["192.168.1.10", "10.0.0.1", "::1", "fd00::1:2", "fe80::1", "controller.lan", "a"],
)
def test_round_trip(host: str) -> None:
    code = _code(host)
    text = encode(code)
    assert text.startswith("mf1-")
    assert re.fullmatch(r"mf1-([a-z2-7]{5}-)*[a-z2-7]{1,5}", text), text
    assert decode(text) == code


def test_ipv4_code_length_and_bracketed_ipv6_address() -> None:
    text = encode(_code())
    assert len(text.replace("-", "")) == 3 + 71  # 44 bytes -> 71 base32 chars
    assert _code("[fd00::1]").host == "[fd00::1]"
    assert decode(encode(_code("[fd00::1]"))).address == "[fd00::1]:47101"
    assert decode(encode(_code("Controller.LAN."))).host == "controller.lan"


def test_reading_forms_are_forgiven() -> None:
    code = _code()
    text = encode(code)
    assert decode(text.upper()) == code
    assert decode("  " + text.replace("-", " ") + "\n") == code
    assert decode(text.replace("-", "")) == code
    # Look-alikes: base32 has no 0/1/8, a human may type them for O/I/B.
    swapped = text.replace("o", "0").replace("i", "1").replace("b", "8")
    assert decode(swapped) == code


def test_token_hash_and_repr() -> None:
    code = _code()
    assert code.token_hash == hashlib.sha256(code.token).hexdigest()
    assert token_hash(code.token) == code.token_hash
    assert code.token.hex() not in repr(code)
    assert len(new_token()) == 16 and new_token() != new_token()


def _reason(text: str) -> JoinCodeReason:
    with pytest.raises(JoinCodeError) as info:
        decode(text)
    if len(text.strip()) >= 8:
        assert text.strip() not in info.value.message  # never echoes the code
    for group in text.split("-")[1:]:
        if len(group) == 5:
            assert group not in info.value.message
    return info.value.reason


def test_typo_is_bad_checksum_not_wrong_token() -> None:
    """Every single-character substitution, at every position (incl. the last
    character, whose spare bits must be zero - D152 T-3), is refused as a bad
    code; none decodes. Deterministic: no random positions."""
    text = encode(_code())
    alphabet = "abcdefghijklmnopqrstuvwxyz234567"
    tried = 0
    for at, ch in enumerate(text):
        if at < 4 or ch == "-":
            continue
        for other in alphabet:
            if other == ch:
                continue
            with pytest.raises(JoinCodeError) as info:
                decode(text[:at] + other + text[at + 1 :])
            assert info.value.reason is JoinCodeReason.BAD_CHECKSUM, (at, other)
            tried += 1
    assert tried == 71 * 31


def test_truncation() -> None:
    text = encode(_code())
    assert _reason(text[:-6]) in (JoinCodeReason.TRUNCATED, JoinCodeReason.BAD_CHECKSUM)
    assert _reason(text[:-1]) in (JoinCodeReason.TRUNCATED, JoinCodeReason.BAD_CHECKSUM)
    assert _reason("mf1-abcde-fghij") is JoinCodeReason.TRUNCATED
    assert _reason(text[:12]) is JoinCodeReason.TRUNCATED


def test_wrong_version_prefix_and_byte() -> None:
    text = encode(_code())
    assert _reason("mf2-" + text[4:]) is JoinCodeReason.UNSUPPORTED_VERSION
    body = bytearray(_raw(text)[:-4])
    body[0] = 2  # a well-formed, checksummed code of an unknown format
    assert _reason(_with_checksum(bytes(body))) is JoinCodeReason.UNSUPPORTED_VERSION


def test_damaged_fields_with_a_valid_checksum() -> None:
    body = bytearray(_raw(encode(_code()))[:-4])
    unknown_kind = bytes(body[:1]) + b"\x09" + bytes(body[2:])
    assert _reason(_with_checksum(unknown_kind)) is JoinCodeReason.MALFORMED
    longer = bytes(body) + b"\x00"
    assert _reason(_with_checksum(longer)) is JoinCodeReason.MALFORMED
    port_zero = bytes(body[:6]) + b"\x00\x00" + bytes(body[8:])
    assert _reason(_with_checksum(port_zero)) is JoinCodeReason.BAD_ADDRESS
    wildcard = bytes(body[:2]) + b"\x00\x00\x00\x00" + bytes(body[6:])
    assert _reason(_with_checksum(wildcard)) is JoinCodeReason.BAD_ADDRESS
    name = b"\x01\x64\x03a_b" + bytes(body[6:])  # '_' is not a host name
    assert _reason(_with_checksum(name)) is JoinCodeReason.BAD_ADDRESS


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "hello",
        "mf",
        "mf1",
        "mf1-",
        "mf1-!!!!!",
        "xf1-aaaaaaaa",
        "mf1-" + "a" * 2000,
    ],
)
def test_garbage_is_refused(text: str) -> None:
    assert _reason(text) in set(JoinCodeReason)


@pytest.mark.parametrize(
    ("host", "port"),
    [
        ("0.0.0.0", 47101),  # noqa: S104 - must be refused
        ("::", 47101),
        ("fe80::1%eth0", 47101),
        ("not a host", 47101),
        ("x" * 254, 47101),
        ("10.0.0.1", 0),
        ("10.0.0.1", 65536),
    ],
)
def test_encode_refuses_unusable_addresses(host: str, port: int) -> None:
    with pytest.raises(JoinCodeError) as info:
        encode(_code(host, port))
    assert info.value.reason is JoinCodeReason.BAD_ADDRESS


def test_encode_refuses_bad_token_or_pin() -> None:
    for code in (
        JoinCode(host="10.0.0.1", port=1, token=b"short", ca_pin=PIN),
        JoinCode(host="10.0.0.1", port=1, token=new_token(), ca_pin="ab" * 15),
        JoinCode(host="10.0.0.1", port=1, token=new_token(), ca_pin="AB" * 16),
    ):
        with pytest.raises(JoinCodeError) as info:
            encode(code)
        assert info.value.reason is JoinCodeReason.MALFORMED


def test_host_name_max_and_max_plus_one() -> None:
    label = "a" * 63
    longest = ".".join([label, label, label, "a" * 61])  # 253 characters
    assert len(longest) == join_code.MAX_HOST_NAME_BYTES
    assert decode(encode(_code(longest))).host == longest
    with pytest.raises(JoinCodeError):
        encode(_code(longest + "a"))


def test_mutation_checksum_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the checksum compare always passed, a typo would decode to a wrong
    token (the test above would fail): prove that by breaking it."""
    text = encode(_code())
    typo = text[:10] + ("z" if text[10] != "z" else "y") + text[11:]
    monkeypatch.setattr(join_code.hmac, "compare_digest", lambda a, b: True)
    decoded = decode(typo)  # no error any more...
    assert decoded.token != decode(text).token or decoded.host != "192.168.1.10"


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("90s", 90), ("15m", 900), ("2H", 7200), ("900", 900), (" 1h ", 3600)],
)
def test_parse_ttl(text: str, seconds: int) -> None:
    assert parse_ttl(text) == seconds


@pytest.mark.parametrize("text", ["", "15x", "-5m", "1.5h", "m", "9999999"])
def test_parse_ttl_refuses(text: str) -> None:
    with pytest.raises(ValueError, match="duration"):
        parse_ttl(text)
