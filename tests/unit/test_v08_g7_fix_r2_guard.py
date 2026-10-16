"""V0.8 G7 fix round 2 (T-2c, T-2d): an ``allow`` URL suffix is a resource,
and a WebSocket console line's resource is the socket its text names.
"""

from __future__ import annotations

import re

import pytest
from mf_dashboard import NOISE_CANARIES, PageWatch, line_resources

pytestmark = pytest.mark.unit

WS_LINE = (
    "WebSocket connection to 'wss://h:8443/v1/jobs/job_01J0000000000000000000000A/"
    "logs/ws?stream=stdout&offset=12' failed: Error in connection establishment: "
    "net::ERR_CONNECTION_RESET"
)
#: The pattern ``test_v08_g4_tester_r1`` declares for the log socket it drops.
G4_LOG_SOCKET = (
    r"^WebSocket connection to 'wss://[^']+/v1/jobs/job_[0-9A-Z]{26}/logs/ws"
    r"(?:\?[^']*)?' failed:(?!\s*Error during WebSocket handshake)"
)


class _Page:
    def is_closed(self) -> bool:
        return True


class _Msg:
    def __init__(self, text: str, url: str = "") -> None:
        self.text = text
        self.type = "error"
        self.location = {"url": url}


def _watch() -> PageWatch:
    return PageWatch(page=_Page())  # type: ignore[arg-type]


@pytest.mark.parametrize("suffix", ["s", "e", "1", "ws", " js "])
def test_a_suffix_without_a_slash_needs_three_characters(suffix: str) -> None:
    with pytest.raises(ValueError, match="no resource"):
        _watch().allow("status of 404", suffix, "reason")


@pytest.mark.parametrize("suffix", ["/s", "/v1/x", "logs/ws", "abc"])
def test_a_path_or_three_characters_is_accepted(suffix: str) -> None:
    watch = _watch()
    watch.allow("status of 404", suffix, "reason")
    assert len(watch.allowed) == 1


def test_a_websocket_line_names_its_socket_as_a_resource() -> None:
    assert line_resources(WS_LINE, "") == (
        "",
        "wss://h:8443/v1/jobs/job_01J0000000000000000000000A/logs/ws",
    )
    assert line_resources("Uncaught x", "https://h/a.js") == ("https://h/a.js",)
    # Only a line that STARTS as Chromium's socket line names a socket.
    assert line_resources("x WebSocket connection to 'wss://h/logs/ws'", "") == ("",)


def test_an_allow_on_the_socket_applies_to_the_websocket_line() -> None:
    watch = _watch()
    watch.allow("ERR_CONNECTION_RESET", "/logs/ws", "the dropped log socket")
    watch._on_console(_Msg(WS_LINE))  # type: ignore[arg-type]
    assert watch.allowed[0].used == 1 and watch.console_errors == []
    assert watch.final_problems() == []


def test_an_allow_on_another_socket_does_not_apply() -> None:
    watch = _watch()
    watch.allow("ERR_CONNECTION_RESET", "/events/ws", "a different socket")
    watch._on_console(_Msg(WS_LINE))  # type: ignore[arg-type]
    assert watch.allowed[0].used == 0 and len(watch.console_errors) == 1


def test_the_g4_log_socket_pattern_excuses_the_drop_but_no_http_answer() -> None:
    watch = _watch()
    watch.allow_matching(G4_LOG_SOCKET, "the dropped log socket")  # canaries pass
    watch._on_console(_Msg(WS_LINE))  # type: ignore[arg-type]
    handshake = WS_LINE.split(" failed:")[0] + (
        " failed: Error during WebSocket handshake: Unexpected response code: 500"
    )
    watch._on_console(_Msg(handshake))  # type: ignore[arg-type]
    other = WS_LINE.replace("/logs/ws", "/events/ws")
    watch._on_console(_Msg(other))  # type: ignore[arg-type]
    assert [
        line.split(" failed:")[1][:12] for line in watch.unexpected_console_errors()
    ] == [
        " Error durin",
        " Error in co",
    ]
    assert not any(re.search(G4_LOG_SOCKET, c) for c in NOISE_CANARIES)
