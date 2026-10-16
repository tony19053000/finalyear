# ruff: noqa: E501
"""V0.8 G7 tester round 2 (unit): attacks on fix round 1.

* T-6: the forms of a known secret ``mf_leaks.needles`` / ``findings`` search.
* T-2: the allowance hatches of ``PageWatch`` (match-all tricks, unused report).
* T-8: ``JobStateSamples`` / ``check`` (late superseded, missed, revisited state).
* T-5: Node vs Python build digest for odd file names.
* T-3 / T-4: parity enumeration and e2e ids.

Tests that pin a remaining weakness are ``xfail(strict=True)`` with the finding
id: they flip to FAILED the day the weakness is fixed (then drop the marker).
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import quote

import pytest
from mf_dashboard import NOISE_CANARIES, PageWatch, sources_digest
from mf_e2e import OUTAGE_NOISE
from mf_leaks import LeakWatch, needles
from test_v08_g7_cli_ui_parity import PARITY
from test_v08_g7_cli_ui_parity import (
    _check as parity_check,
)
from test_v08_g7_cli_ui_parity import (
    _compute_commands as compute_commands,
)
from test_v08_g7_cli_ui_parity import (
    _e2e_test_exists as e2e_test_exists,
)
from test_v08_g7_cli_ui_parity import (
    test_every_compute_command_is_mapped as parity_compute_test,
)
from test_v08_g7_tester_r1_unit import _mini_frontend

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"


# ======================================================================
# T-6: forms of a known secret
# ======================================================================


def _found(secret: str, place_text: str, where: str = "t: localStorage") -> bool:
    watch = LeakWatch(known={secret})
    watch.places.append((where, place_text))
    return bool(watch.findings())


SECRETS = {
    "quote": 'pa"ss-wor"d-1234',
    "backslash": "pa\\ss\\wor\\d-1234",
    "tab": "pass\tword\tvalue-1234",
    "unicode": "pässwörd-日本語-1234",
    "emoji": "pw-\U0001f510-secret-1234",
    "control": "pw\x01\x02-secret-1234",
    "multiline": "-----BEGIN KEY-----\nMIIEvQIBADANBgkqhkiG9w0B\n-----END KEY-----\n",
    "crlf": "line-one-abcdefgh\r\nline-two-abcdefgh\r\n",
}


@pytest.mark.parametrize("name", sorted(SECRETS))
def test_t6_each_awkward_secret_is_found_in_json_and_double_json_dumps(
    name: str,
) -> None:
    secret = SECRETS[name]
    once = json.dumps(secret)
    twice = json.dumps(once)
    for ascii_only in (True, False):
        assert _found(secret, json.dumps({"k": secret}, ensure_ascii=ascii_only))
        assert _found(secret, json.dumps([["k", secret]], ensure_ascii=ascii_only))
    assert _found(secret, "x=" + once)
    assert _found(secret, twice)
    assert _found(secret, json.dumps(json.dumps({"k": secret})))  # JSON in JSON


@pytest.mark.parametrize("name", sorted(SECRETS))
def test_t6_each_awkward_secret_is_found_as_base64_every_variant(name: str) -> None:
    raw = SECRETS[name].encode("utf-8")
    for encoded in (base64.b64encode(raw), base64.urlsafe_b64encode(raw)):
        text = encoded.decode()
        for variant in (text, text.rstrip("=")):
            assert _found(SECRETS[name], f"token:{variant}")


def test_t6_base64_embedded_at_every_alignment_is_not_found_documented_limit() -> None:
    """base64 of 'prefix' + secret does not contain base64(secret) unless the
    alignment is a multiple of 3 bytes: only the aligned form is searched.
    This is the nature of the encoding (the page would have to encode the
    secret ALONE); recorded, not a defect."""
    secret = "plain-secret-value-1234"
    aligned = base64.b64encode(b"abc" + secret.encode()).decode()
    shifted = base64.b64encode(b"ab" + secret.encode()).decode()
    assert _found(secret, aligned)
    assert not _found(secret, shifted)


def test_t6_each_line_of_a_multiline_secret_is_found_alone() -> None:
    secret = SECRETS["multiline"]
    for line in ("MIIEvQIBADANBgkqhkiG9w0B", "-----BEGIN KEY-----"):
        assert _found(secret, f"console: {line}")
    assert _found(secret, json.dumps({"k": "MIIEvQIBADANBgkqhkiG9w0B"}))


def test_t6_a_short_line_of_a_multiline_secret_is_not_searched_by_design() -> None:
    assert not _found("abc\ndef\nghi\njkl\nmno", "def")  # lines < 8 chars


def test_t6_percent_encoded_once_is_found_in_any_place() -> None:
    secret = 'a"b c&d=e/f+g-1234'
    assert _found(secret, quote(secret, safe=""), "t: console.log")


@pytest.mark.xfail(
    strict=True,
    reason="T-6b (minor): percent-encoding TWICE (a redirect re-encoding a "
    "query value) is decoded once only",
)
def test_t6b_double_percent_encoding_is_found() -> None:
    secret = 'a"b c&d=e/f+g-1234'
    assert _found(secret, quote(quote(secret, safe=""), safe=""), "t: request URL")


@pytest.mark.xfail(
    strict=True,
    reason="T-6c (minor): a multi-line secret whose lines are all < 8 chars is "
    "searched only as typed; a page that normalises CRLF -> LF (a textarea "
    "does) is not found",
)
def test_t6c_crlf_normalised_to_lf_is_found() -> None:
    secret = "A1b2C3\r\nD4e5F6"  # 14 chars, lines of 6
    assert len(secret) >= 8
    assert _found(secret, secret.replace("\r\n", "\n"))


def test_t6_a_secret_with_surrogates_does_not_crash_needles() -> None:
    assert needles("lone-\ud800-surrogate-1234")  # surrogatepass: no exception


def test_t6_label_redaction_covers_the_escaped_form_in_the_url() -> None:
    secret = 'pa"ss-wor"d-1234'
    watch = LeakWatch(known={secret})
    watch.places.append((f"t: storage of https://h/{secret}/x", secret))
    out = "\n".join(watch.findings())
    assert out and secret not in out and "pa%22ss" not in out
    assert "wor" not in out.replace("wor\\", "")  # nothing of it survives


def test_t6_findings_never_print_value_for_any_form() -> None:
    secret = SECRETS["quote"]
    watch = LeakWatch(known={secret})
    for form in needles(secret):
        watch.places.append((f"t: place {form}", form))
    assert watch.findings()
    for line in watch.findings():
        assert "wor" not in line and "pa" not in line.replace("page", "")


# ======================================================================
# T-2: allowances
# ======================================================================


class _Page:
    def is_closed(self) -> bool:
        return True


class _Msg:
    def __init__(self, text: str, url: str = "https://h/assets/i.js") -> None:
        self.text = text
        self.type = "error"
        self.location = {"url": url}


def _watch() -> PageWatch:
    return PageWatch(page=_Page())  # type: ignore[arg-type]


MATCH_ALL = [
    ".",
    "(?:)",
    ".*?x?",
    "x*",
    "(?i).",
    "^",
    "$",
    r"\b|x",
    "(?s).*",
    r"[\s\S]",
    "(?=.)",
    r"\w+",
    "(?<!z)",
    "(?x) ",
    "(?:a|)",
    "(?i)ERROR",
    "(?i)failed",
    "(?i)uncaught",
    "Error",
    "@ https://",
    "status of [0-9]{3}",
    "Failed to load resource",
    "(?i)failed to load resource",
    "WebSocket connection",
    "net::ERR_[A-Z_]+",
    "[a-z]+",
    ".{0,3}",
]


@pytest.mark.parametrize("pattern", MATCH_ALL)
def test_t2_allow_matching_refuses_every_match_everything_trick(pattern: str) -> None:
    with pytest.raises(ValueError):
        _watch().allow_matching(pattern, "reason")


@pytest.mark.parametrize(
    "line",
    [
        "Failed to load resource: the server responded with a status of 403 (Forbidden) @ https://h/v1/x",
        "Failed to load resource: the server responded with a status of 400 (Bad Request) @ https://h/v1/x",
        "Failed to load resource: the server responded with a status of 502 (Bad Gateway) @ https://h/v1/x",
        "Failed to load resource: the server responded with a status of 429 (Too Many Requests) @ https://h/v1/x",
        "WebSocket connection to 'wss://h/v1/events/ws' failed: Error during WebSocket handshake: Unexpected response code: 403 @ https://h/a.js",
        "WebSocket connection to 'wss://h/v1/events/ws' failed: Error during WebSocket handshake: Unexpected response code: 401 @ https://h/a.js",
        "Failed to load resource: net::ERR_HTTP_RESPONSE_CODE_FAILURE @ https://h/v1/jobs",
        "Uncaught ReferenceError: x is not defined @ https://h/assets/i.js",
        "Error: render failed @ https://h/assets/i.js",
    ],
)
def test_t2_the_real_outage_noise_hides_no_http_status_or_app_error(line: str) -> None:
    assert not re.search(OUTAGE_NOISE, line), line
    watch = _watch()
    watch.allow_matching(OUTAGE_NOISE, "outage", may_not_occur=True)
    text, _, url = line.rpartition(" @ ")
    watch._on_console(_Msg(text, url))  # type: ignore[arg-type]
    assert watch.unexpected_console_errors() == [line]


@pytest.mark.parametrize(
    "line",
    [
        "Failed to load resource: net::ERR_CONNECTION_REFUSED @ https://h/v1/nodes",
        "Failed to load resource: net::ERR_CONNECTION_RESET @ https://h/v1/nodes",
        "WebSocket connection to 'wss://h/v1/events/ws' failed: @ https://h/a.js",
        "WebSocket connection to 'wss://h/v1/events/ws' failed: net::ERR_CONNECTION_REFUSED @ https://h/a.js",
    ],
)
def test_t2_the_real_outage_noise_does_cover_the_outage(line: str) -> None:
    watch = _watch()
    watch.allow_matching(OUTAGE_NOISE, "outage", may_not_occur=True)
    text, _, url = line.rpartition(" @ ")
    watch._on_console(_Msg(text, url))  # type: ignore[arg-type]
    assert watch.unexpected_console_errors() == []


@pytest.mark.parametrize(
    ("text", "suffix"),
    [
        ("", ""),
        ("", "/v1/x"),
        ("  ", "/v1/x"),
        ("ab", "/v1/x"),
        ("status of 401", ""),
        ("status of 401", "/"),
        ("status of 401", " / "),
        ("status of 401", "   "),
    ],
)
def test_t2_allow_refuses_match_everything(text: str, suffix: str) -> None:
    with pytest.raises(ValueError):
        _watch().allow(text, suffix, "reason")


@pytest.mark.parametrize("reason", ["", " ", "\n\t"])
def test_t2_a_blank_reason_is_refused_by_both_hatches(reason: str) -> None:
    with pytest.raises(ValueError):
        _watch().allow("status of 401", "/v1/x", reason)
    with pytest.raises(ValueError):
        _watch().allow_matching("g7 probe", reason)


def test_t2_a_refused_allowance_leaves_nothing_behind() -> None:
    watch = _watch()
    for bad in (".", "(?:)", "Error"):
        with pytest.raises(ValueError):
            watch.allow_matching(bad, "r")
    with pytest.raises(ValueError):
        watch.allow("", "", "r")
    assert watch.noise == [] and watch.allowed == []
    watch._on_console(_Msg("boom"))  # type: ignore[arg-type]
    assert watch.unexpected_console_errors() == ["boom @ https://h/assets/i.js"]


def test_t2_an_unused_allow_is_reported_at_the_end() -> None:
    watch = _watch()
    watch.allow("status of 401", "/v1/nodes", "the dead session")
    problems = watch.final_problems()
    assert len(problems) == 1 and problems[0].startswith("allowance never used")
    assert "/v1/nodes" in problems[0]


def test_t2_an_unused_allow_matching_is_reported_at_the_end() -> None:
    watch = _watch()
    watch.allow_matching("g7 probe noise", "the probe")
    problems = watch.final_problems()
    assert len(problems) == 1 and "g7 probe noise" in problems[0]


def test_t2_a_mistyped_suffix_never_applies_and_is_therefore_reported() -> None:
    """The G7 item-1 bug class: an allowance that excuses nothing."""
    watch = _watch()
    watch.allow("status of 401", "/v1/nodez", "typo")
    watch._on_console(_Msg("status of 401", "https://h/v1/nodes"))  # type: ignore[arg-type]
    problems = watch.final_problems()
    assert any(p.startswith("console error") for p in problems), problems
    assert any(p.startswith("allowance never used") for p in problems), problems


def test_t2_a_used_allow_is_not_reported() -> None:
    watch = _watch()
    watch.allow("status of 401", "/v1/nodes", "the dead session")
    watch._on_console(_Msg("status of 401", "https://h/v1/nodes"))  # type: ignore[arg-type]
    assert watch.final_problems() == []


def test_t2_a_used_allow_matching_is_not_reported() -> None:
    watch = _watch()
    watch.allow_matching("g7 probe noise", "the probe")
    watch._on_console(_Msg("g7 probe noise here"))  # type: ignore[arg-type]
    assert watch.final_problems() == []


def test_t2_one_use_does_not_excuse_a_sibling_allowance() -> None:
    watch = _watch()
    watch.allow("status of 401", "/v1/a", "a")
    watch.allow("status of 401", "/v1/b", "b")
    watch._on_console(_Msg("status of 401", "https://h/v1/a"))  # type: ignore[arg-type]
    problems = watch.final_problems()
    assert len(problems) == 1 and "/v1/b" in problems[0]


def test_t2_an_allowance_declared_after_the_line_is_reported_not_silently_ignored() -> (
    None
):
    """allow() is judged as lines arrive: declared late, it excuses nothing, so
    the line is a console error AND the allowance is reported unused."""
    watch = _watch()
    watch._on_console(_Msg("status of 401", "https://h/v1/a"))  # type: ignore[arg-type]
    watch.allow("status of 401", "/v1/a", "late")
    kinds = {p.split(":")[0] for p in watch.final_problems()}
    assert kinds == {"console error", "allowance never used"}


def test_t2_may_not_occur_skips_the_unused_report_but_still_judges_lines() -> None:
    watch = _watch()
    watch.allow("status of 401", "/v1/a", "timing", may_not_occur=True)
    watch.allow_matching("g7 probe noise", "timing", may_not_occur=True)
    assert watch.final_problems() == []
    watch._on_console(_Msg("status of 401", "https://h/v1/OTHER"))  # type: ignore[arg-type]
    assert len(watch.final_problems()) == 1


def test_t2_text_must_be_inside_the_line_not_the_url() -> None:
    watch = _watch()
    watch.allow("status of 401", "/v1/a", "r", may_not_occur=True)
    watch._on_console(_Msg("status of 500", "https://h/v1/a"))  # type: ignore[arg-type]
    assert watch.unexpected_console_errors() == ["status of 500 @ https://h/v1/a"]


def test_t2_a_non_error_message_is_never_judged_or_counted_as_use() -> None:
    watch = _watch()
    watch.allow("status of 401", "/v1/a", "r")
    msg = _Msg("status of 401", "https://h/v1/a")
    msg.type = "warning"
    watch._on_console(msg)  # type: ignore[arg-type]
    assert [p for p in watch.final_problems() if "never used" in p]


def test_t2_canaries_cover_the_documented_classes() -> None:
    joined = "\n".join(NOISE_CANARIES)
    for needle in ("401", "404", "500", "ERR_HTTP_RESPONSE_CODE_FAILURE", "handshake"):
        assert needle in joined


@pytest.mark.xfail(
    strict=True,
    reason="T-2b (minor): the canary check is a sample, not a proof - a "
    "negative-lookahead pattern dodges it and hides an application error "
    "class / other HTTP statuses",
)
@pytest.mark.parametrize(
    "pattern",
    [
        "Uncaught (?!TypeError)",
        "Failed to load resource: the server responded with a status of 4(?!01|04)",
    ],
)
def test_t2b_a_lookahead_pattern_cannot_hide_a_class_of_errors(pattern: str) -> None:
    with pytest.raises(ValueError):
        _watch().allow_matching(pattern, "reason")


@pytest.mark.xfail(
    strict=True,
    reason="T-2c (minor): allow() accepts a one-character URL suffix ('s', 'e', "
    "'1'), which is 'every resource' in practice; a suffix should be a path "
    "(start with '/') or at least 3 characters",
)
@pytest.mark.parametrize("suffix", ["s", "e"])
def test_t2c_a_tiny_url_suffix_is_refused(suffix: str) -> None:
    with pytest.raises(ValueError):
        _watch().allow("Uncaught", suffix, "reason")


# ======================================================================
# T-8: job.state latency samples
# ======================================================================


def _perf() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "ui_perf_tester_r2", ROOT / "scripts" / "ui_perf.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PERF = _perf()
T0 = 1_700_000_000_000.0  # ms


def _iso(ms: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ms / 1000.0, tz=UTC).isoformat()


def _tr(*steps: tuple[str, float]) -> list[dict[str, Any]]:
    return [{"to_state": s, "at": _iso(T0 + at)} for s, at in steps]


def _results(samples: Any) -> dict[str, Any]:
    fast = PERF.summary([10.0, 20.0])
    return {
        "list": {
            "list_first_render": fast,
            "list_in_app_nav": fast,
            "filter_change": fast,
            "detail_open": fast,
            "scroll": {"long_task_max": 0.0},
        },
        "events": {
            "heartbeat_latency": PERF.summary([100.0] * 20),
            "job_state_latency": PERF.summary(samples.lags),
            "job_state_missed": len(samples.missed),
            "job_state_missed_examples": samples.missed[:10],
        },
    }


def test_t8_a_shown_state_is_timed_to_its_own_display() -> None:
    s = PERF.JobStateSamples()
    s.add(
        "j",
        _tr(("queued", 0), ("running", 100)),
        {"queued": T0 + 30, "running": T0 + 160},
    )
    assert [round(x) for x in s.lags] == [30, 60]
    assert s.superseded == 0 and s.missed == []


def test_t8_a_hidden_state_is_timed_to_the_successor_the_page_showed() -> None:
    s = PERF.JobStateSamples()
    s.add(
        "j",
        _tr(("submitted", 0), ("planning", 10), ("queued", 20)),
        {"queued": T0 + 70},
    )
    assert s.superseded == 2 and s.missed == []
    assert [round(x) for x in s.lags] == [70, 60, 50]


def test_t8_a_superseded_state_arriving_late_fails_the_check() -> None:
    s = PERF.JobStateSamples()
    # planning is never shown; the page shows queued 5 s after planning happened
    s.add("j", _tr(("planning", 0), ("queued", 10)), {"queued": T0 + 5_000})
    for _ in range(40):
        s.add("k", _tr(("queued", 0)), {"queued": T0 + 20})
    problems = PERF.check(_results(s))
    assert any(p.startswith("job_state_latency_max") for p in problems), problems
    assert s.missed == []


def test_t8_a_superseded_state_late_is_caught_by_max_even_when_p95_is_fine() -> None:
    s = PERF.JobStateSamples()
    s.add("j", _tr(("planning", 0), ("queued", 10)), {"queued": T0 + 30_000})
    for _ in range(300):
        s.add("k", _tr(("queued", 0)), {"queued": T0 + 20})
    r = _results(s)
    assert r["events"]["job_state_latency"]["p95"] < 2000
    assert any(p.startswith("job_state_latency_max") for p in PERF.check(r))


def test_t8_a_state_with_no_later_state_shown_is_missed_and_fails() -> None:
    s = PERF.JobStateSamples()
    s.add("j", _tr(("queued", 0), ("running", 100)), {"queued": T0 + 10})
    assert s.missed == ["j:running"]
    for _ in range(40):
        s.add("k", _tr(("queued", 0)), {"queued": T0 + 20})
    assert any(p.startswith("job_state_missed: 1") for p in PERF.check(_results(s)))


def test_t8_a_job_the_page_never_listed_is_all_missed() -> None:
    s = PERF.JobStateSamples()
    s.add("j", _tr(("queued", 0), ("running", 100), ("succeeded", 300)), {})
    assert s.missed == ["j:queued", "j:running", "j:succeeded"]
    assert s.missed_terminal == 1


def test_t8_a_missed_terminal_state_is_missed_even_if_an_earlier_one_was_shown() -> (
    None
):
    s = PERF.JobStateSamples()
    s.add(
        "j",
        _tr(("queued", 0), ("running", 100), ("succeeded", 300)),
        {"running": T0 + 120},
    )
    assert s.missed == ["j:succeeded"] and s.missed_terminal == 1


def test_t8_a_successor_shown_before_the_state_happened_does_not_count() -> None:
    s = PERF.JobStateSamples()
    s.add("j", _tr(("planning", 1000), ("queued", 1010)), {"queued": T0 + 500})
    assert s.missed == ["j:planning"]


def test_t8_empty_samples_raise_not_pass() -> None:
    r = _results(PERF.JobStateSamples())
    with pytest.raises(KeyError):
        PERF.check(r)


@pytest.mark.xfail(
    strict=True,
    reason="T-8b (minor): a state REVISITED later (retry: running -> queued -> "
    "running) is timed to the FIRST time the page showed that state, so the "
    "second `running` gets a negative latency and a 30 s stall passes",
)
def test_t8b_a_revisited_state_shown_30_s_late_fails() -> None:
    s = PERF.JobStateSamples()
    s.add(
        "j",
        _tr(("queued", 0), ("running", 100), ("queued", 1_000), ("running", 2_000)),
        {"queued": T0 + 10, "running": T0 + 130},  # first-seen only
    )
    assert min(s.lags) >= 0, s.lags  # a negative latency is nonsense


# ======================================================================
# T-5: digest agreement for odd names (real node)
# ======================================================================


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="no node")


def _node_digest(fe: Path) -> str:
    (fe / "scripts").mkdir(exist_ok=True)
    shutil.copy(FRONTEND / "scripts" / "build-stamp.mjs", fe / "scripts")
    done = subprocess.run(
        ["node", "scripts/build-stamp.mjs", "--print"],
        cwd=fe,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return done.stdout.strip()


@needs_node
@pytest.mark.parametrize(
    "name",
    [
        "é.ts",  # NFD
        "é.ts",  # NFC
        "A.ts",
        "a.ts",  # case-only pair (Linux)
        "back\\slash.ts",
        "new\nline.ts",
        "tab\there.ts",
        "\U0001f600\U0001f601.ts",
        "￯.ts",
        ".ts",
        "a b  c.ts",
        "dot.in.name.test.ts",
        "\u202eRTL.ts",
    ],
)
def test_t5_node_and_python_digests_agree_for_odd_names(
    tmp_path: Path, name: str
) -> None:
    fe = _mini_frontend(tmp_path)
    (fe / "src" / name).write_text("x", "utf-8")
    (fe / "src" / "zz.ts").write_text("y", "utf-8")
    assert _node_digest(fe) == sources_digest(fe)


@needs_node
def test_t5_node_and_python_digests_agree_for_nested_odd_dirs(tmp_path: Path) -> None:
    fe = _mini_frontend(tmp_path)
    for d in ("\U0001f600", "￮", "é", "a b"):
        (fe / "src" / d).mkdir()
        (fe / "src" / d / "x.ts").write_text(d, "utf-8")
    assert _node_digest(fe) == sources_digest(fe)


@needs_node
def test_t5b_a_non_utf8_file_name_fails_loudly_in_node_never_a_silent_mismatch(
    tmp_path: Path,
) -> None:
    """Info (T-5b): a frontend file whose name is not valid UTF-8 (Linux only)
    makes the Node stamp CRASH (stat ENOENT on the U+FFFD name), i.e. `npm run
    build` fails loudly; it never yields a digest that differs from Python's.
    Windows/macOS cannot create such names; git for Windows would refuse them."""
    fe = _mini_frontend(tmp_path)
    bad = os.fsencode(fe / "src") + b"/bad\xff.ts"
    try:
        Path(os.fsdecode(bad)).write_text("x", "utf-8")
    except OSError:
        pytest.skip("filesystem refuses non-UTF-8 names")
    with pytest.raises(subprocess.CalledProcessError):
        _node_digest(fe)


# ======================================================================
# T-3 / T-4: parity
# ======================================================================


def test_t3_a_hidden_nested_new_command_needs_a_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import typer

    from meshforge.cli.app import app

    sub = typer.Typer()

    @sub.command("zz-deep", hidden=True)
    def zz_deep() -> None:  # pragma: no cover
        """fake"""

    monkeypatch.setattr(app, "registered_groups", list(app.registered_groups))
    app.add_typer(sub, name="zz-group", hidden=True)
    assert "zz-group zz-deep" in compute_commands()
    with pytest.raises(AssertionError, match="zz-group zz-deep"):
        parity_compute_test()


@pytest.mark.parametrize(
    "test_id",
    [
        "test_v08_g7_guard.py::test_nonexistent",
        "../e2e/test_v08_g7_guard.py::test_x",
        "./test_v08_g7_guard.py::test_x",
        "sub/test_v08_g7_guard.py::test_x",
        "tests/e2e/test_v08_g7_guard.py::test_x",
        "/etc/passwd::test_x",
        "conftest.py::test_x",
        "parity.py::test_x",
        "test_v08_g7_guard.py",
        "test_v08_g7_guard.py::test_x::extra",
        "test_v08_g7_guard.py::",
        " test_v08_g7_guard.py::test_x",
        "test_v08_g7_guard.py::test_x\n",
        "test_v08_g7_guard.py\n::test_x",
        "..\\unit\\test_v08_g7_cli_ui_parity.py::test_cli_only_is_the_exception",
        "test_v08_g7_guard.py::TestX::test_y",
        "test_v08_g7_guard.pyc::test_x",
        "TEST_V08_G7_GUARD.PY::test_x",
        "test_é.py::test_x",
    ],
)
def test_t4_bad_e2e_ids_are_refused(test_id: str) -> None:
    assert not e2e_test_exists(test_id)


def test_t4_a_real_id_is_accepted_and_every_table_id_is_unique_per_entry() -> None:
    assert e2e_test_exists(
        "test_v08_g7_mode1_flows.py::test_mode1_job_life_cycle_in_the_browser_matches_the_api_and_the_cli"
    )
    for table in (PARITY.COMPUTE, PARITY.CONTROLLER_USER):
        for entry in table.values():
            ids = getattr(entry, "e2e", ())
            assert len(ids) == len(set(ids))


def test_t4_a_symlinked_test_file_pointing_outside_is_refused(
    tmp_path: Path,
) -> None:
    link = ROOT / "tests" / "e2e" / "test_zz_tester_r2_symlink.py"
    try:
        link.symlink_to(ROOT / "tests" / "unit" / "test_v08_g7_cli_ui_parity.py")
    except OSError:
        pytest.skip("no symlinks")
    try:
        assert not e2e_test_exists(
            "test_zz_tester_r2_symlink.py::test_cli_only_is_the_exception"
        )
    finally:
        link.unlink()


def test_t4_the_check_helper_is_the_one_the_gate_uses() -> None:
    parity_check(PARITY.COMPUTE, compute_commands(), "compute")
