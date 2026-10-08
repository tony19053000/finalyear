"""V0.7 G2 tester round 2: D152 canonical join-code spelling."""

# ruff: noqa: S311

from __future__ import annotations

import base64
import random

import pytest

from meshforge.common.join_code import JoinCode, JoinCodeError, decode, encode

PIN = "0123456789abcdef0123456789abcdef"
ALPHA = "abcdefghijklmnopqrstuvwxyz234567"


def _code(host: str, port: int = 47101, seed: int = 0) -> str:
    token = random.Random(seed).randbytes(16)
    return encode(JoinCode(host=host, port=port, token=token, ca_pin=PIN))


def test_every_spare_bit_variant_of_the_last_character_is_refused() -> None:
    for seed, host in enumerate(
        ["10.1.2.3", "2001:db8::7", "a.example", "x" * 63 + ".example.org"]
    ):
        code = _code(host, seed=seed)
        last = ALPHA.index(code[-1])
        decode(code)
        for v in range(32):
            if v != last:
                with pytest.raises(JoinCodeError):
                    decode(code[:-1] + ALPHA[v])


def test_canonical_codes_round_trip_for_every_hostname_length() -> None:
    for n in range(1, 60):
        host = ("a" * n) if n < 60 else "a"
        code = _code(host, seed=n)
        assert decode(code).host == host
    for n in range(1, 254, 7):
        host = ".".join(["b" * min(63, n)] * 1)
        assert decode(_code(host, seed=n)).host == host


def test_random_codes_round_trip_and_readable_variants_still_decode() -> None:
    rng = random.Random(3)
    for i in range(300):
        host = rng.choice(
            ["10.0.0.1", "192.168.5.9", "2001:db8::1", "fe80::1", "node-1.lan", "h"]
        )
        code = _code(host, port=rng.randint(1, 65535), seed=i)
        want = decode(code)
        assert decode(code.upper()) == want
        assert decode(" " + code.replace("-", " ") + "\n") == want
        assert decode(code.replace("-", "")) == want
        assert (
            decode(code.replace("o", "0").replace("i", "1").replace("b", "8")) == want
        )


def test_non_canonical_but_valid_looking_forms_are_refused() -> None:
    code = _code("10.1.2.3")
    letters = code[4:].replace("-", "")
    payload = base64.b32decode(letters.upper() + "=" * (-len(letters) % 8))
    # padding characters, appended zero bytes, a leading extra group
    for text in (
        "mf1-" + letters + "=",
        "mf1-" + letters + "aa",
        "mf1-aaaaa-" + letters,
    ):
        with pytest.raises(JoinCodeError):
            decode(text)
    # a payload with a *valid checksum* but appended trailing zero byte is not canonical either
    assert payload
