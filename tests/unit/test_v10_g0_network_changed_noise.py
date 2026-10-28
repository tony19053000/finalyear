"""V1.0 G0: the e2e page guard's one shared noise entry, ``NETWORK_CHANGED_NOISE``.

Docker job containers change the host's network interfaces; Chromium then aborts
in-flight fetches with ``net::ERR_NETWORK_CHANGED``. Only that exact error on a
failed resource load is excused - every other console error still fails a test.
"""

from __future__ import annotations

import re

import pytest
from mf_dashboard import NETWORK_CHANGED_NOISE, NOISE_CANARIES, PageWatch

pytestmark = pytest.mark.unit


class _Page:
    def is_closed(self) -> bool:
        return True


class _Msg:
    def __init__(self, text: str, url: str = "") -> None:
        self.text = text
        self.type = "error"
        self.location = {"url": url}


def _watch() -> PageWatch:
    watch = PageWatch(page=_Page())  # type: ignore[arg-type]
    watch.allow_matching(NETWORK_CHANGED_NOISE, "test", may_not_occur=True)
    return watch


def test_the_seen_line_is_excused() -> None:
    watch = _watch()
    watch._on_console(  # type: ignore[arg-type]
        _Msg(
            "Failed to load resource: net::ERR_NETWORK_CHANGED",
            "https://127.0.0.1:43491/v1/jobs?limit=50",
        )
    )
    assert watch.unexpected_console_errors() == []
    assert watch.unused_allowances() == []


@pytest.mark.parametrize(
    "text",
    [
        "Failed to load resource: net::ERR_CONNECTION_REFUSED",
        "Failed to load resource: net::ERR_NETWORK_CHANGED_SOMETHING",
        "Failed to load resource: net::ERR_NETWORK_ACCESS_DENIED",
        "Failed to load resource: the server responded with a status of 500 ()",
        "Uncaught Error: net::ERR_NETWORK_CHANGED",
        "x Failed to load resource: net::ERR_NETWORK_CHANGED",
    ],
)
def test_any_other_line_still_fails(text: str) -> None:
    watch = _watch()
    watch._on_console(_Msg(text, "https://h/v1/jobs"))  # type: ignore[arg-type]
    assert len(watch.unexpected_console_errors()) == 1


def test_it_hides_no_canary() -> None:
    pattern = re.compile(NETWORK_CHANGED_NOISE)
    assert not [c for c in NOISE_CANARIES if pattern.search(c)]
