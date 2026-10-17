"""V0.8 G7 fix round 1 (T-2): the page guard's allowances are bounded.

``PageWatch.allow`` / ``allow_matching`` need a reason, refuse a text, suffix
or pattern that would excuse everything (or an HTTP 4xx/5xx line, for a
pattern), and an allowance that excused nothing is reported at the end unless
the test marked the line as timing-dependent (``may_not_occur``).
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from mf_dashboard import NOISE_CANARIES, PageWatch
from mf_e2e import OUTAGE_NOISE

pytestmark = pytest.mark.unit


class _Page:
    def is_closed(self) -> bool:
        return True  # final_problems then skips CSP/axe (no browser here)


class _Msg:
    def __init__(self, text: str, url: str = "https://h/assets/i.js") -> None:
        self.text = text
        self.type = "error"
        self.location = {"url": url}


def _watch() -> PageWatch:
    return PageWatch(page=_Page())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "suffix", "reason"),
    [
        ("status of 401", "/v1/session", ""),
        ("status of 401", "/v1/session", "   "),
        ("", "/v1/session", "why"),
        ("40", "/v1/session", "why"),
        ("status of 401", "", "why"),
        ("status of 401", "/", "why"),
    ],
)
def test_allow_refuses_a_missing_reason_or_a_match_everything_rule(
    text: str, suffix: str, reason: str
) -> None:
    with pytest.raises(ValueError):
        _watch().allow(text, suffix, reason)


@pytest.mark.parametrize(
    "pattern",
    [
        ".*",
        "",
        "x?",
        "error",
        "Failed to load resource",
        "status of",
        "ERR_",
        "WebSocket",
    ],
)
def test_allow_matching_refuses_a_pattern_that_hides_app_or_http_errors(
    pattern: str,
) -> None:
    with pytest.raises(ValueError):
        _watch().allow_matching(pattern, "a reason")


def test_allow_matching_needs_a_reason() -> None:
    with pytest.raises(ValueError):
        _watch().allow_matching("g7 probe", "")


def test_the_outage_noise_pattern_is_accepted_and_hides_no_http_status() -> None:
    w = _watch()
    w.allow_matching(OUTAGE_NOISE, "outage")
    assert not any(re.search(OUTAGE_NOISE, c) for c in NOISE_CANARIES)
    w._on_console(_Msg("Failed to load resource: net::ERR_CONNECTION_REFUSED"))  # type: ignore[arg-type]
    w._on_console(
        _Msg(  # type: ignore[arg-type]
            "Failed to load resource: the server responded with a status of 502 (Bad Gateway)",
            "https://h/v1/nodes",
        )
    )
    assert len(w.unexpected_console_errors()) == 1
    assert "502" in w.unexpected_console_errors()[0]


def test_an_unused_allowance_is_reported_and_a_used_one_is_not() -> None:
    w = _watch()
    w.allow("status of 404", "/v1/jobs/a", "read of a")
    w.allow("status of 404", "/v1/jobs/b", "read of b")
    w.allow_matching("g7 probe noise", "noise")
    w._on_console(_Msg("... status of 404 ()", "https://h/v1/jobs/a"))  # type: ignore[arg-type]
    problems = w.final_problems()
    unused = [p for p in problems if p.startswith("allowance never used")]
    assert len(unused) == 2, problems
    assert any("/v1/jobs/b" in p for p in unused)
    assert any("g7 probe noise" in p for p in unused)
    assert not any("/v1/jobs/a" in p for p in unused)


def test_may_not_occur_is_not_reported_when_unused_but_still_excuses() -> None:
    w = _watch()
    w.allow("status of 404", "/v1/jobs/x", "timing", may_not_occur=True)
    w.allow_matching("g7 probe noise", "timing", may_not_occur=True)
    assert w.final_problems() == []
    w._on_console(_Msg("... status of 404 ()", "https://h/v1/jobs/x"))  # type: ignore[arg-type]
    w._on_console(_Msg("g7 probe noise here"))  # type: ignore[arg-type]
    assert w.final_problems() == []


def test_a_disarmed_page_reports_no_unused_allowance() -> None:
    w = _watch()
    w.allow("status of 404", "/v1/jobs/x", "never comes")
    w.disarm("negative control")
    assert w.final_problems() == []


def _unused(w: Any) -> list[str]:
    return list(w.unused_allowances())


def test_unused_allowances_lists_the_reason() -> None:
    w = _watch()
    w.allow("status of 403", "/v1/x", "the refused probe")
    assert _unused(w) == ["allow('status of 403', '/v1/x') - the refused probe"]
