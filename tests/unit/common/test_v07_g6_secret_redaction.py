"""V0.7 G6 deliverable 5 (D144): exact-value secret redaction.

* every form - raw, base64 std/url padded/unpadded, hex lower/upper,
  URL-encoded (``%XX`` upper/lower, ``+``/``%20``) - becomes ``[secret:NAME]``;
* split across chunk boundaries at **every** split point, and under seeded
  random chunking, the streamed output equals the one-shot output;
* length-preserving mode (the controller) keeps every length;
* our own structlog lines are scrubbed while a value is active, and no longer
  touched once it is removed (mutation check: the scrubber's removal shows the
  value).
"""

from __future__ import annotations

import base64
import io
import json
import logging
import random
from urllib.parse import quote, quote_plus

import pytest
import structlog

from meshforge.common.logging import configure_logging, get_logger
from meshforge.common.secret_redaction import (
    ACTIVE,
    StreamRedactor,
    encoded_forms,
    redact_bytes,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

VALUE = b"s3cret value/with+symbols?&=\xc3\xa9\xfb!"
LOGV = "log-secret välue+/=".encode()
SECRETS = {"API_KEY": VALUE, "OTHER": b"another-one-42"}


def _forms() -> list[bytes]:
    return [
        VALUE,
        base64.b64encode(VALUE),
        base64.b64encode(VALUE).rstrip(b"="),
        base64.urlsafe_b64encode(VALUE),
        base64.urlsafe_b64encode(VALUE).rstrip(b"="),
        VALUE.hex().encode(),
        VALUE.hex().upper().encode(),
        quote(VALUE, safe="").encode(),
        quote(VALUE, safe="").lower().encode(),
        quote_plus(VALUE, safe="").encode(),
    ]


def test_every_form_is_known() -> None:
    forms = encoded_forms(VALUE)
    for form in _forms():
        assert form in forms


@pytest.mark.parametrize("form", _forms())
def test_each_form_is_replaced(form: bytes) -> None:
    text = b"before " + form + b" after\n"
    out = redact_bytes(text, SECRETS)
    assert out == b"before [secret:API_KEY] after\n"
    same = redact_bytes(text, SECRETS, length_preserving=True)
    assert len(same) == len(text) and form not in same
    assert b"[secret:API_KEY]" in same or b"*" in same


def test_split_at_every_point_equals_one_shot() -> None:
    text = b"x" * 7 + b"".join(f + b" | " for f in _forms()) + b"tail"
    expected = redact_bytes(text, SECRETS)
    for cut in range(len(text) + 1):
        for preserve in (False, True):
            r = StreamRedactor(SECRETS, length_preserving=preserve)
            out = r.feed(text[:cut]) + r.feed(text[cut:]) + r.flush()
            if preserve:
                assert len(out) == len(text)
                assert all(f not in out for f in _forms())
            else:
                assert out == expected, cut


def test_random_chunking_never_leaks() -> None:
    rng = random.Random(6)  # noqa: S311 - seeded fuzz, reproducible
    for _ in range(200):
        parts = [
            rng.choice([*_forms(), b"noise ", b"\n", b"secret:"]) for _ in range(30)
        ]
        text = b"".join(parts)
        r = StreamRedactor(SECRETS)
        out = b""
        i = 0
        while i < len(text):
            step = rng.randint(1, 9)
            out += r.feed(text[i : i + step])
            i += step
        out += r.flush()
        assert out == redact_bytes(text, SECRETS)
        assert VALUE not in out and base64.b64encode(VALUE) not in out


def test_no_secrets_is_a_pass_through() -> None:
    r = StreamRedactor({})
    assert r.feed(b"abc") == b"abc" and r.flush() == b""


def test_our_own_logs_are_scrubbed_while_active() -> None:
    stream = io.StringIO()
    configure_logging(level="INFO", fmt="json", stream=stream)
    log = get_logger("g6.test")
    try:
        ACTIVE.add("att_t", {"API_KEY": LOGV, "OTHER": b"another-one-42"})
        log.info(
            "g6.value " + LOGV.decode(),  # noqa: G003 - the value in the message is the point
            field=base64.b64encode(LOGV).decode(),
            nested={"k": LOGV.hex()},
        )
        try:
            raise RuntimeError("boom " + LOGV.decode())
        except RuntimeError:
            log.exception("g6.failed")
        ACTIVE.remove("att_t")
        log.info("g6.after another-one-42")
    finally:
        ACTIVE.remove("att_t")
        structlog.reset_defaults()
        logging.getLogger().handlers.clear()
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    text = stream.getvalue()
    assert LOGV.decode() not in text and LOGV.hex() not in text
    assert base64.b64encode(LOGV).decode() not in text
    assert "[secret:API_KEY]" in lines[0]["event"]
    assert lines[0]["field"] == "[secret:API_KEY]"
    assert "another-one-42" in lines[-1]["event"]  # removed: no longer touched
