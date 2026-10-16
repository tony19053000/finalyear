"""V0.8 G7 fix round 1 (T-6, T-10): every form a page could hold a known
credential in is searched, and a finding never prints one."""

from __future__ import annotations

import base64
import json

import pytest
from mf_leaks import LeakWatch, needles

pytestmark = pytest.mark.unit

PEM = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n"
    'line with "quotes" and \\ back\r\n'
    "short\n"
    "-----END PRIVATE KEY-----\n"
)
UNICODE = "pässwörd-ünï-" + "x" * 8


def _found(value: str, text: str) -> bool:
    watch = LeakWatch(known={value})
    watch.places.append(("place", text))
    return bool(watch.findings())


@pytest.mark.parametrize(
    "form",
    [
        pytest.param(json.dumps(PEM), id="json-escaped"),
        pytest.param(json.dumps(json.dumps(PEM)), id="json-escaped-twice"),
        pytest.param(json.dumps(UNICODE), id="json-ascii-escaped-unicode"),
        pytest.param(json.dumps(UNICODE, ensure_ascii=False), id="json-raw-unicode"),
        pytest.param(base64.b64encode(PEM.encode()).decode(), id="base64"),
        pytest.param(
            base64.urlsafe_b64encode(PEM.encode()).decode().rstrip("="), id="b64url"
        ),
        pytest.param("x MIIEvQIBADANBgkqhkiG9w0BAQEFAASC y", id="one-line"),
        pytest.param(
            json.dumps('line with "quotes" and \\ back'), id="one-line-escaped"
        ),
        pytest.param("-----END PRIVATE KEY-----", id="last-line"),
    ],
)
def test_each_form_of_a_known_value_is_found(form: str) -> None:
    value = UNICODE if "nï" in form or "\\u00e4" in form else PEM.strip()
    assert _found(value, f"storage dump {{{form}}}")


def test_short_lines_of_a_multiline_value_are_not_searched() -> None:
    assert "short" not in needles(PEM.strip())
    assert not _found(PEM.strip(), "a perfectly short text")


def test_a_one_line_value_has_no_line_forms() -> None:
    forms = needles("single-line-value")
    assert "single-line-value" in forms
    assert all(len(f) >= 8 for f in forms)


def test_a_label_never_prints_the_value_even_when_its_url_holds_it() -> None:
    secret = "mf-label-secret-" + "Q" * 8
    watch = LeakWatch(known={secret})
    watch.places.append(
        (f"t: request URL https://h/x?c={secret}", f"https://h/x?c={secret}")
    )
    watch.places.append(("t: storage of https://h/p%3Fc%3D" + secret, secret))
    shape = "mfat_" + "A" * 20
    watch.places.append((f"t: navigated {shape}", shape))
    findings = watch.findings()
    assert findings
    text = "\n".join(findings)
    assert secret not in text and shape not in text
    assert "<redacted>" in text
