"""V1.0 G0 tester round 1: the ERR_NETWORK_CHANGED allow-list and the UI-perf
suspect-retry must not swallow anything else."""

from __future__ import annotations

import re
import ssl
from typing import Any

import httpx
import pytest
from mf_dashboard import NETWORK_CHANGED_NOISE, PageWatch
from mf_ui_perf import load_ui_perf

pytestmark = pytest.mark.unit

UI = load_ui_perf("ui_perf_v10_g0_tester")
SCEN = UI.scenarios


class _Page:
    def is_closed(self) -> bool:
        return True


class _Msg:
    def __init__(
        self, text: str, url: str = "https://h/v1/jobs", kind: str = "error"
    ) -> None:
        self.text, self.type, self.location = text, kind, {"url": url}


def _watch() -> PageWatch:
    w = PageWatch(page=_Page())  # type: ignore[arg-type]
    w.allow_matching(NETWORK_CHANGED_NOISE, "test", may_not_occur=True)
    return w


def test_excused_line_carries_the_resource_url_and_is_the_only_one() -> None:
    w = _watch()
    w._on_console(_Msg("Failed to load resource: net::ERR_NETWORK_CHANGED"))  # type: ignore[arg-type]
    assert w.unexpected_console_errors() == []


@pytest.mark.parametrize(
    "text",
    [
        # a real error that merely follows / embeds the noise text
        "Failed to load resource: net::ERR_NETWORK_CHANGED\nUncaught TypeError: boom",
        "Failed to load resource: net::ERR_NETWORK_CHANGEDX",
        "Failed to load resource: net::ERR_NETWORK_CHANGED_x",
        "Failed to load resource: net::err_network_changed",
        "Failed to load resource:  net::ERR_NETWORK_CHANGED",
        "Failed to load resource: net::ERR_INTERNET_DISCONNECTED",
        "Failed to load resource: net::ERR_CONNECTION_RESET",
        "Failed to load resource: net::ERR_ABORTED",
        "Failed to load resource: net::ERR_CERT_AUTHORITY_INVALID",
        "Refused to connect: net::ERR_NETWORK_CHANGED",
    ],
)
def test_lookalikes_still_fail(text: str) -> None:
    w = _watch()
    w._on_console(_Msg(text))  # type: ignore[arg-type]
    assert len(w.unexpected_console_errors()) == 1, text


def test_the_pattern_is_anchored_and_not_multiline() -> None:
    assert NETWORK_CHANGED_NOISE.startswith("^")
    pat = re.compile(NETWORK_CHANGED_NOISE)
    assert not pat.search(
        "ok\nFailed to load resource: net::ERR_NETWORK_CHANGED @ https://h/"
    )


def test_page_errors_are_never_excused_by_the_noise_entry() -> None:
    w = _watch()
    w.page_errors.append("net::ERR_NETWORK_CHANGED")
    assert w.page_errors  # the guard checks page_errors separately from noise


def test_warnings_and_http_statuses_still_fail() -> None:
    w = _watch()
    w._on_console(
        _Msg("Failed to load resource: the server responded with a status of 502 ()")
    )  # type: ignore[arg-type]
    w._on_console(_Msg("Uncaught Error: x"))  # type: ignore[arg-type]
    assert len(w.unexpected_console_errors()) == 2


# ----------------------------------------------------------- suspect retry

SUSPECT = {
    "error": {"code": "no_feasible_plan", "message": "the node is suspect, not online"}
}


def _api(handler: Any) -> UI.Api:
    api = UI.Api("https://h", "tok", ssl.create_default_context())
    api.http.close()
    api.http = httpx.Client(
        base_url="https://h", transport=httpx.MockTransport(handler)
    )
    return api


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SCEN, "SUSPECT_RETRY_PAUSE_S", 0.0)


@pytest.mark.parametrize(
    "resp",
    [
        httpx.Response(
            409,
            json={
                "error": {
                    "code": "no_feasible_plan",
                    "message": "the node is suspect, not online (extra)",
                }
            },
        ),
        httpx.Response(
            409,
            json={
                "error": {
                    "code": "no_feasible_plan",
                    "message": "The node is suspect, not online",
                }
            },
        ),
        httpx.Response(409, json={"error": {"code": "no_feasible_plan"}}),
        httpx.Response(409, json={"error": "the node is suspect, not online"}),
        httpx.Response(409, json=[1]),
        httpx.Response(409, content=b"not json"),
        httpx.Response(409, json={"error": None}),
        httpx.Response(429, json=SUSPECT),
        httpx.Response(503, json=SUSPECT),
        httpx.Response(200, json=SUSPECT),
    ],
)
def test_only_the_exact_refusal_is_transient(resp: httpx.Response) -> None:
    try:
        got = SCEN.is_transient_suspect(resp)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"classifier crashed: {exc!r}")
    assert got is False


def test_exact_refusal_is_transient() -> None:
    assert SCEN.is_transient_suspect(httpx.Response(409, json=SUSPECT)) is True


def test_non_json_409_is_raised_once_not_retried() -> None:
    calls: list[int] = []

    def h(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(409, content=b"<html>")

    with pytest.raises(RuntimeError, match="409"):
        _api(h).submit("t", "n", "node_a")
    assert len(calls) == 1


def test_a_network_exception_is_not_swallowed() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(httpx.ConnectError):
        _api(h).submit("t", "n", "node_a")


def test_retry_is_bounded_in_wall_time_and_in_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000.0]
    monkeypatch.setattr(SCEN.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(SCEN.time, "sleep", lambda s: now.__setitem__(0, now[0] + 1.0))
    calls: list[int] = []

    def h(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(409, json=SUSPECT)

    with pytest.raises(RuntimeError, match="suspect"):
        _api(h).submit("t", "n", "node_a")
    assert SCEN.SUSPECT_RETRY_S <= 10.0
    assert len(calls) <= int(SCEN.SUSPECT_RETRY_S) + 2


def test_retry_then_a_different_error_is_raised_at_once() -> None:
    answers = [
        httpx.Response(409, json=SUSPECT),
        httpx.Response(500, text="boom"),
        httpx.Response(201, json={"job_id": "j"}),
    ]
    calls: list[int] = []

    def h(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return answers[len(calls) - 1]

    with pytest.raises(RuntimeError, match="500"):
        _api(h).submit("t", "n", "node_a")
    assert len(calls) == 2
