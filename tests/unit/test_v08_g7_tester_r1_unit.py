"""V0.8 G7 tester r1 (unit tier): attack the G7 test infrastructure itself.

Derived from brief 5g / roadmap criteria, not from what the code does:

* item 0: exact revoked-reason match, stale-dist detection edge cases;
* item 2: the CLI/UI parity table must fail for a fake command, a bogus
  route, a missing/skipped e2e test;
* item 1: the page guard's escape hatches must not be able to mask everything;
* item 5: LeakWatch must find a credential however a page happens to hold it;
* item 4: the perf thresholds really cover criterion 2.

Tests marked ``xfail(strict)`` are DEFECTS (T-n in the progress file): they
state the property that should hold and flip to a failure when it is fixed.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import grpc
import pytest
from mf_dashboard import (
    BUILD_STAMP,
    FRONTEND,
    PageWatch,
    dist_problem,
    require_fresh_dist,
    sources_digest,
)
from mf_leaks import LeakWatch
from mf_ui_perf import load_ui_perf
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
from test_v08_g7_cli_ui_parity import (
    test_every_entry_is_a_real_route_with_real_e2e_tests_or_a_reason as parity_entry_test,
)

from meshforge.agent.session.policy import StreamEndAction, stream_end_action

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
E2E = ROOT / "tests" / "e2e"
PD = grpc.StatusCode.PERMISSION_DENIED


# ======================================================================
# item 0 (D204 d): the revoked reason is matched exactly
# ======================================================================


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        ("node_revoked", StreamEndAction.STOP_REVOKED),
        ("node_revoked: this node was revoked", StreamEndAction.STOP_REVOKED),
        ("  node_revoked : padded  ", StreamEndAction.STOP_REVOKED),
        ("node_revoked:", StreamEndAction.STOP_REVOKED),
        ("node_revoked: a: b: c", StreamEndAction.STOP_REVOKED),
        ("node_revoked_pending: x", StreamEndAction.FATAL_CONFIG),
        ("node_revokedx", StreamEndAction.FATAL_CONFIG),
        ("node_revoked-ish: x", StreamEndAction.FATAL_CONFIG),
        ("not_node_revoked: x", StreamEndAction.FATAL_CONFIG),
        ("x: node_revoked", StreamEndAction.FATAL_CONFIG),
        ("NODE_REVOKED: x", StreamEndAction.FATAL_CONFIG),
        (
            "node_revoked node n1",
            StreamEndAction.FATAL_CONFIG,
        ),  # no colon: not the wire form
        ("", StreamEndAction.FATAL_CONFIG),
        (None, StreamEndAction.FATAL_CONFIG),
        ("cert_superseded: x", StreamEndAction.FATAL_CONFIG),
    ],
)
def test_revoked_reason_word_is_matched_exactly(
    details: str | None, expected: StreamEndAction
) -> None:
    assert stream_end_action(PD, details) is expected


def test_the_real_controller_revoked_detail_still_stops_the_agent() -> None:
    """The exact string the controller sends (admission.REVOKED_DETAIL) must
    keep mapping to STOP_REVOKED, or a revoked laptop loops forever."""
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.controller.session.admission import REVOKED_DETAIL

    assert stream_end_action(PD, REVOKED_DETAIL) is StreamEndAction.STOP_REVOKED


@pytest.mark.parametrize(
    "code",
    [
        grpc.StatusCode.UNAUTHENTICATED,
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.INTERNAL,
        grpc.StatusCode.OK,
        None,
    ],
)
def test_node_revoked_text_under_another_status_code_is_not_a_revocation(
    code: grpc.StatusCode | None,
) -> None:
    assert (
        stream_end_action(code, "node_revoked: x") is not StreamEndAction.STOP_REVOKED
    )


# ======================================================================
# item 0 (D206): stale-dist detection
# ======================================================================


def _mini_frontend(root: Path) -> Path:
    fe = root / "frontend"
    (fe / "src" / "test").mkdir(parents=True)
    (fe / "public").mkdir()
    (fe / "index.html").write_text("<html></html>", "utf-8")
    (fe / "package.json").write_text("{}", "utf-8")
    (fe / "src" / "main.tsx").write_text("export {}\n", "utf-8")
    (fe / "public" / "favicon.svg").write_text("<svg/>", "utf-8")
    (fe / "src" / "test" / "x.ts").write_text("t", "utf-8")
    return fe


def _mini_dist(root: Path, fe: Path) -> Path:
    dist = root / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>", "utf-8")
    (dist / BUILD_STAMP).write_text(
        json.dumps({"schema": 1, "sources_sha256": sources_digest(fe)}), "utf-8"
    )
    return dist


def test_fresh_then_every_kind_of_source_change_is_stale(tmp_path: Path) -> None:
    fe = _mini_frontend(tmp_path)
    dist = _mini_dist(tmp_path, fe)
    assert dist_problem(dist, fe) is None
    changes: list[Any] = [
        lambda: (fe / "src" / "main.tsx").write_text("export const a = 1\n", "utf-8"),
        lambda: (fe / "src" / "new.ts").write_text("x", "utf-8"),
        lambda: (fe / "public" / "favicon.svg").write_text("<svg x/>", "utf-8"),
        lambda: (fe / "package.json").write_text('{"a":1}', "utf-8"),
        lambda: (fe / "vite.config.ts").write_text("//", "utf-8"),
        lambda: (fe / "src" / "main.tsx").unlink(),
        lambda: (fe / "src" / "main.tsx").rename(fe / "src" / "renamed.tsx"),
    ]
    for change in changes:
        # reset to the freshly built state
        shutil.rmtree(fe)
        _mini_frontend(tmp_path)
        assert dist_problem(dist, fe) is None, "reset failed"
        change()
        problem = dist_problem(dist, fe)
        assert problem is not None and "STALE" in problem, problem


def test_a_test_file_edit_does_not_make_the_build_stale(tmp_path: Path) -> None:
    fe = _mini_frontend(tmp_path)
    dist = _mini_dist(tmp_path, fe)
    (fe / "src" / "test" / "x.ts").write_text("changed", "utf-8")
    (fe / "src" / "a.test.tsx").write_text("it()", "utf-8")
    assert dist_problem(dist, fe) is None


@pytest.mark.parametrize(
    "stamp",
    [
        b"",
        b"not json",
        b"[]",
        b"null",
        b'{"sources_sha256": null}',
        b'{"schema": 1}',
        b"\xff\xfe\x00bad utf8",
        b'{"sources_sha256": 12345}',
    ],
)
def test_a_broken_stamp_is_a_skip_reason_never_an_exception(
    tmp_path: Path, stamp: bytes
) -> None:
    fe = _mini_frontend(tmp_path)
    dist = _mini_dist(tmp_path, fe)
    (dist / BUILD_STAMP).write_bytes(stamp)
    problem = dist_problem(dist, fe)
    assert problem is not None and "npm run build" in problem, problem


def test_stamp_is_a_directory_or_dist_index_missing(tmp_path: Path) -> None:
    fe = _mini_frontend(tmp_path)
    dist = _mini_dist(tmp_path, fe)
    (dist / BUILD_STAMP).unlink()
    (dist / BUILD_STAMP).mkdir()
    assert "npm run build" in (dist_problem(dist, fe) or "")
    (dist / "index.html").unlink()
    assert "not built" in (dist_problem(dist, fe) or "")
    assert "not built" in (dist_problem(tmp_path / "nope", fe) or "")


def test_require_fresh_dist_skips_with_the_rebuild_command(tmp_path: Path) -> None:
    with pytest.raises(pytest.skip.Exception, match=r"npm ci && npm run build"):
        require_fresh_dist(tmp_path / "no-dist")


def test_the_real_dist_stamp_matches_the_real_sources_or_says_stale() -> None:
    """Whatever the state of this checkout's build, dist_problem answers with
    None or a rebuild instruction (never raises)."""
    problem = dist_problem()
    assert problem is None or "npm run build" in problem


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize(
    "astral",
    [False, True],  # T-5 fixed: both sort by UTF-8 bytes (= code points)
    ids=["bmp-unicode", "astral"],
)
def test_node_and_python_digests_agree_on_unusual_file_names(
    tmp_path: Path, astral: bool
) -> None:
    """A source file with spaces, unicode and an astral character in its name
    hashes the same in the Node build stamp and in the Python re-computation.
    (JS sorts by UTF-16 code units, Python by code points: they differ for
    astral vs U+E000-U+FFFF names. A frontend file name like that makes every
    dist-reading test SKIP forever as 'stale'.)"""
    fe = _mini_frontend(tmp_path)
    (fe / "scripts").mkdir()
    shutil.copy(FRONTEND / "scripts" / "build-stamp.mjs", fe / "scripts")
    (fe / "src" / "sp ace.ts").write_text("a", "utf-8")
    (fe / "src" / "ünï.ts").write_text("b", "utf-8")
    if astral:
        (fe / "src" / ("a" + chr(0x10000) + ".ts")).write_text("c", "utf-8")
        (fe / "src" / ("a" + chr(0xFFFF) + ".ts")).write_text("d", "utf-8")
    done = subprocess.run(
        ["node", "scripts/build-stamp.mjs", "--print"],
        cwd=fe,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert done.stdout.strip() == sources_digest(fe)


# ======================================================================
# item 2: the parity table is a real gate
# ======================================================================


@pytest.fixture
def fake_cli_command(monkeypatch: pytest.MonkeyPatch) -> str:
    """Register a brand-new `compute zz-brand-new` command on the real app."""
    from meshforge.cli.app import app

    def zz_brand_new() -> None:  # pragma: no cover - never run
        """fake"""

    monkeypatch.setattr(app, "registered_commands", list(app.registered_commands))
    app.command("zz-brand-new")(zz_brand_new)
    return "zz-brand-new"


def test_a_new_cli_command_without_a_mapping_fails_the_parity_test(
    fake_cli_command: str,
) -> None:
    assert fake_cli_command in compute_commands()
    with pytest.raises(AssertionError, match=r"zz-brand-new"):
        parity_compute_test()


def test_a_new_cli_subgroup_command_without_a_mapping_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import typer

    from meshforge.cli.app import app

    sub = typer.Typer()

    @sub.command("deep")
    def deep() -> None:  # pragma: no cover
        """fake"""

    monkeypatch.setattr(app, "registered_groups", list(app.registered_groups))
    app.add_typer(sub, name="zz-group")
    assert "zz-group deep" in compute_commands()
    with pytest.raises(AssertionError, match=r"zz-group deep"):
        parity_compute_test()


def test_removing_a_command_from_the_cli_fails_the_parity_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meshforge.cli.app import app

    kept = [
        c
        for c in app.registered_commands
        if (c.name or (c.callback.__name__ if c.callback else "")) != "logs"
    ]
    assert len(kept) < len(app.registered_commands)
    monkeypatch.setattr(app, "registered_commands", kept)
    with pytest.raises(AssertionError, match=r"no longer exist"):
        parity_compute_test()


def test_a_hidden_new_command_still_needs_a_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meshforge.cli.app import app

    def zz_hidden() -> None:  # pragma: no cover
        """fake"""

    monkeypatch.setattr(app, "registered_commands", list(app.registered_commands))
    app.command("zz-hidden", hidden=True)(zz_hidden)
    with pytest.raises(AssertionError):
        parity_compute_test()


def _mutated(**changes: Any) -> dict[str, Any]:
    table = dict(PARITY.COMPUTE)
    table.update(changes)
    return table


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        (
            PARITY.InUi("/does-not-exist", "x", ("test_v08_g7_guard.py::x",)),
            r"/does-not-exist",
        ),
        (PARITY.InUi("/jobs", "x", ()), r"jobs"),
        (PARITY.InUi("/jobs", "x", ("test_v08_g4_jobs.py::test_nope",)), r"test_nope"),
        (PARITY.InUi("/jobs", "x", ("no_such_file.py::test_x",)), r"no_such_file"),
        (PARITY.InUi("/jobs", "x", ("test_v08_g4_jobs.py",)), r"jobs"),
        (PARITY.InUi("/jobs", "x", ("test_v08_g4_jobs.py::helper",)), r"helper"),
        (PARITY.InUi("/jobs", "  ", (PARITY.LIFE_CYCLE,)), r"jobs"),
        (PARITY.CliOnly("too short"), r"jobs"),
        (PARITY.CliOnly(""), r"jobs"),
        ("not an entry", r"jobs"),
    ],
)
def test_a_wrong_table_entry_fails_the_entry_check(entry: Any, match: str) -> None:
    with pytest.raises(AssertionError):
        parity_entry_test(_mutated(jobs=entry))


def test_the_check_runs_the_real_table_green() -> None:
    parity_entry_test(PARITY.COMPUTE)
    parity_entry_test(PARITY.CONTROLLER_USER)
    parity_check(PARITY.COMPUTE, compute_commands(), "compute")


def _decorated_skips(path: Path, function: str) -> list[str]:
    tree = ast.parse(path.read_text("utf-8"))
    skips: list[str] = []
    module_marks = ast.unparse(tree)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function:
            for dec in node.decorator_list:
                text = ast.unparse(dec)
                if re.search(r"\.(skip|xfail)\b", text) and "skipif" not in text:
                    skips.append(text)
    if re.search(r"pytestmark\s*=.*\.mark\.(skip|xfail)\b", module_marks):
        skips.append("module-level skip/xfail")
    return skips


def test_no_parity_cited_e2e_test_is_unconditionally_skipped_or_xfailed() -> None:
    """A cited test that never runs proves nothing about the command."""
    cited = {
        (test_id.partition("::")[0], test_id.partition("::")[2])
        for table in (PARITY.COMPUTE, PARITY.CONTROLLER_USER)
        for entry in table.values()
        if isinstance(entry, PARITY.InUi)
        for test_id in entry.e2e
    }
    assert len(cited) >= 10
    bad = {
        f"{f}::{fn}": skips
        for f, fn in sorted(cited)
        if (skips := _decorated_skips(E2E / f, fn))
    }
    assert not bad, bad


def test_every_cited_e2e_test_takes_a_new_page_or_a_fabric() -> None:
    """A cited 'e2e' proof that never opens a browser page is not a UI proof."""
    cited = {
        (test_id.partition("::")[0], test_id.partition("::")[2])
        for table in (PARITY.COMPUTE, PARITY.CONTROLLER_USER)
        for entry in table.values()
        if isinstance(entry, PARITY.InUi)
        for test_id in entry.e2e
    }
    offenders: list[str] = []
    for file_name, function in sorted(cited):
        tree = ast.parse((E2E / file_name).read_text("utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == function:
                args = {a.arg for a in node.args.args}
                if "new_page" not in args:
                    offenders.append(f"{file_name}::{function}")
    assert not offenders, offenders


def test_parity_helper_rejects_a_method_or_nested_function_as_a_test_id() -> None:
    assert not e2e_test_exists("test_v08_g7_guard.py::inner")
    assert not e2e_test_exists("test_v08_g7_guard.py::")
    assert not e2e_test_exists("")


def test_parity_ids_cannot_escape_tests_e2e() -> None:
    assert not e2e_test_exists(
        "../unit/test_v08_g7_cli_ui_parity.py::test_cli_only_is_the_exception"
    )


# ======================================================================
# item 1: the escape hatches of the page guard
# ======================================================================


class _Msg:
    def __init__(
        self, text: str, kind: str = "error", url: str = "https://x/a.js"
    ) -> None:
        self.text = text
        self.type = kind
        self.location = {"url": url}


def _watch() -> PageWatch:
    return PageWatch(page=None)  # type: ignore[arg-type]  # logic only; no page


def test_a_console_error_is_recorded_and_judged() -> None:
    w = _watch()
    w._on_console(_Msg("boom"))  # type: ignore[arg-type]
    assert w.unexpected_console_errors() == ["boom @ https://x/a.js"]


@pytest.mark.parametrize("kind", ["warning", "log", "info", "debug"])
def test_non_error_console_types_are_not_errors(kind: str) -> None:
    w = _watch()
    w._on_console(_Msg("hmm", kind))  # type: ignore[arg-type]
    assert w.unexpected_console_errors() == []


def test_console_assert_counts_as_an_error() -> None:
    w = _watch()
    w._on_console(_Msg("Assertion failed", "assert"))  # type: ignore[arg-type]
    assert w.unexpected_console_errors()


def test_allow_is_narrow_by_text_and_resource() -> None:
    w = _watch()
    w.allow("status of 401", "/v1/session", "the refused login probe")
    w._on_console(_Msg("Failed ... status of 401 ()", url="https://h/v1/session"))  # type: ignore[arg-type]
    w._on_console(_Msg("Failed ... status of 401 ()", url="https://h/v1/nodes"))  # type: ignore[arg-type]
    w._on_console(_Msg("Failed ... status of 500 ()", url="https://h/v1/session"))  # type: ignore[arg-type]
    assert len(w.unexpected_console_errors()) == 2


def test_an_empty_allow_must_not_mask_everything() -> None:
    w = _watch()
    try:
        w.allow("", "")
    except (AssertionError, ValueError):
        return  # rejected up front: good
    w._on_console(_Msg("real application error", url="https://h/app.js"))  # type: ignore[arg-type]
    assert w.unexpected_console_errors(), "allow('', '') masked a real error"


def test_a_match_everything_pattern_must_not_mask_everything() -> None:
    w = _watch()
    try:
        w.allow_matching(".*")
    except (AssertionError, ValueError):
        return
    w._on_console(_Msg("real application error"))  # type: ignore[arg-type]
    assert w.unexpected_console_errors(), "allow_matching('.*') masked a real error"


def test_disarm_and_skip_axe_require_a_reason() -> None:
    w = _watch()
    with pytest.raises(AssertionError):
        w.disarm("")
    with pytest.raises(AssertionError):
        w.skip_final_axe("")


def test_disarm_hides_page_errors_too_by_design() -> None:
    """Documented behaviour (not a defect): disarm() skips the whole page."""
    w = _watch()
    w.page_errors.append("uncaught")
    w._on_console(_Msg("boom"))  # type: ignore[arg-type]
    w.disarm("negative control")
    assert w.final_problems() == []


# ======================================================================
# item 5: LeakWatch judging
# ======================================================================

LONG = "mf-secret-" + "x7Kq9" * 4


@pytest.mark.parametrize(
    ("where", "text"),
    [
        ("localStorage", json.dumps({"localStorage": [["k", LONG]]})),
        ("sessionStorage", json.dumps({"sessionStorage": [["k", LONG]]})),
        ("indexedDB", json.dumps({"indexedDB": [["db", "s", [{"a": {"b": [LONG]}}]]]})),
        ("cookie", json.dumps({"cookies": [f"c={LONG}"]})),
        ("window.name", json.dumps({"windowName": LONG})),
        ("url query", f"https://h/p?x={LONG}"),
        ("url fragment", f"https://h/p#{LONG}"),
        ("url path", f"https://h/{LONG}/z"),
        ("ws url", f"wss://h/v1/events/ws?token={LONG}"),
        ("console", f"debug: {LONG}"),
        ("bundle", f"var a='{LONG}';"),
    ],
)
def test_a_known_secret_is_found_in_every_kind_of_place(where: str, text: str) -> None:
    watch = LeakWatch(known={LONG})
    watch.places.append((where, text))
    assert watch.findings() == [f"{where}: holds a known credential"]
    assert LONG not in "".join(watch.findings())  # never prints the value


def test_url_encoded_secret_is_found() -> None:
    from urllib.parse import quote

    secret = "pa ss/w+rd&é=" + "q" * 8
    watch = LeakWatch(known={secret})
    watch.places.append(("url", f"https://h/?p={quote(secret, safe='')}"))
    assert watch.findings()


def test_a_secret_with_special_characters_is_found_in_a_storage_dump() -> None:
    secret = 'line1 "quoted"\nline2 \\back' + "z" * 8
    watch = LeakWatch(known={secret})
    # exactly what DUMP_JS does: JSON.stringify of Object.entries(localStorage)
    dump = json.dumps({"localStorage": [["k", secret]]})
    watch.places.append(("storage", dump))
    assert watch.findings(), "multi-line/quoted secret in localStorage went unnoticed"


def test_one_line_of_a_multiline_secret_is_found() -> None:
    body = "-----BEGIN KEY-----\nAAAAAAAAAAAAAAAAAAAA\nBBBBBBBBBBBBBBBBBBBB\n-----END KEY-----"
    watch = LeakWatch()
    watch.add_known(body)
    watch.places.append(("console", "leaked line: AAAAAAAAAAAAAAAAAAAA"))
    assert watch.findings()


def test_base64_of_a_secret_is_found() -> None:
    """Was a documented limit; fix round 1 (T-6, Lead ruling D207) searches the
    base64 form too, so this now asserts the opposite."""
    import base64

    watch = LeakWatch(known={LONG})
    watch.places.append(("storage", base64.b64encode(LONG.encode()).decode()))
    assert watch.findings() == ["storage: holds a known credential"]


@pytest.mark.parametrize(
    "shape",
    [
        "mfat_" + "A" * 16,
        "mfat_" + "a-_Z0" * 8,
        "mfbs_" + "B" * 43,
        "mf1-ABCDE-1-2-3",
        "mf1-ABCDE-AAAAA-BBBBB-CCCCC-DDDDD",
    ],
)
def test_credential_shapes_are_found_without_being_registered(shape: str) -> None:
    watch = LeakWatch()
    watch.places.append(("p", f"prefix {shape} suffix"))
    assert len(watch.findings()) == 1


@pytest.mark.parametrize(
    "not_a_credential", ["mfat_short", "mf1-AB", "mfbs_", "mf1-ABCDE-1-2"]
)
def test_near_miss_shapes_are_not_findings(not_a_credential: str) -> None:
    watch = LeakWatch()
    watch.places.append(("p", not_a_credential))
    assert watch.findings() == []


def test_short_known_values_are_ignored_by_design() -> None:
    watch = LeakWatch()
    watch.add_known("abcd")
    watch.add_known("1234567")
    watch.add_known(None)
    watch.add_known("")
    assert watch.known == set()
    watch.add_known("12345678")
    assert watch.known == {"12345678"}


def test_a_value_learnt_after_the_leak_was_recorded_is_still_found() -> None:
    watch = LeakWatch()
    watch.places.append(("early console", f"x {LONG} y"))
    assert watch.findings() == []
    watch.add_known(LONG)
    assert watch.findings()


def test_learn_ignores_failed_responses_and_records_unreadable_bodies() -> None:
    class Req:
        method = "POST"
        post_data = "{not json"
        post_data_buffer = b""

    class Resp:
        status = 200
        url = "https://h/v1/session"
        request = Req()

        def json(self) -> Any:
            raise ValueError("no")

    watch = LeakWatch()
    watch._learn(Resp())  # type: ignore[arg-type]
    assert any(w == "leak watch" for w, _ in watch.places), (
        "an unreadable body is a gap"
    )
    Resp.status = 401
    watch2 = LeakWatch()
    watch2._learn(Resp())  # type: ignore[arg-type]
    assert watch2.places == []  # a refused login teaches nothing


# ======================================================================
# item 4: do the perf thresholds cover criterion 2?
# ======================================================================


def _ui_perf() -> ModuleType:
    return load_ui_perf("ui_perf_tr1")


def _results(**override: float) -> dict[str, Any]:
    # Fix round 1 (T-8): live latency is checked at p50, p95 and max, and a
    # job.state change never shown is counted - the fixture carries all three
    # statistics (each = the override) and the missed count.
    base: dict[str, Any] = {
        "list": {
            "list_first_render": {"p95": 100.0},
            "list_in_app_nav": {"p95": 100.0},
            "filter_change": {"p95": 100.0},
            "detail_open": {"p95": 100.0},
            "scroll": {"long_task_max": 10.0},
        },
        "events": {
            "heartbeat_latency": {"p50": 100.0, "p95": 100.0, "max": 100.0},
            "job_state_latency": {"p50": 100.0, "p95": 100.0, "max": 100.0},
            "job_state_missed": 0,
        },
    }
    for key, value in override.items():
        for section in ("list", "events"):
            if key in base[section]:
                for stat in base[section][key]:
                    base[section][key][stat] = value
        if key == "long_task_max":
            base["list"]["scroll"]["long_task_max"] = value
    return base


def test_criterion_2_live_latency_is_asserted_at_two_seconds_p95() -> None:
    perf = _ui_perf()
    assert perf.THRESHOLDS_MS["heartbeat_latency_p95"] <= 2000.0
    assert perf.THRESHOLDS_MS["job_state_latency_p95"] <= 2000.0
    assert perf.check(_results()) == []
    assert perf.check(_results(heartbeat_latency=2000.0)) == []
    assert any("heartbeat" in p for p in perf.check(_results(heartbeat_latency=2000.1)))
    assert any("job_state" in p for p in perf.check(_results(job_state_latency=2500.0)))


def test_every_threshold_is_checked_and_tripped() -> None:
    perf = _ui_perf()
    got = {
        t.split(":")[0]
        for t in perf.check(
            _results(
                list_first_render=1e9,
                list_in_app_nav=1e9,
                filter_change=1e9,
                detail_open=1e9,
                heartbeat_latency=1e9,
                job_state_latency=1e9,
                long_task_max=1e9,
            )
        )
    }
    assert got == set(perf.THRESHOLDS_MS)


def test_missing_latency_samples_fail_loudly_not_silently() -> None:
    perf = _ui_perf()
    broken = _results()
    broken["events"]["job_state_latency"] = perf.summary([])  # {"n": 0}
    with pytest.raises(KeyError):
        perf.check(broken)


def test_without_the_events_phase_latency_is_not_checked() -> None:
    """check() on a result with no 'events' key says nothing about criterion 2;
    the pytest -m perf test always passes with_events=True."""
    perf = _ui_perf()
    r = _results()
    del r["events"]
    assert perf.check(r) == []


def test_a_rare_multi_second_stall_must_fail_the_latency_check() -> None:
    perf = _ui_perf()
    values = [50.0] * 95 + [30_000.0] * 5  # 5 % of events arrive 30 s late
    r = _results()
    r["events"]["job_state_latency"] = perf.summary(values)
    assert perf.check(r), "5 events 30 s late passed the latency threshold"


def test_summary_percentiles() -> None:
    perf = _ui_perf()
    assert perf.summary([]) == {"n": 0}
    one = perf.summary([5.0])
    assert one["p50"] == one["p95"] == one["max"] == 5.0
    many = perf.summary(list(map(float, range(1, 101))))
    assert many["p50"] == 50.5 and 95.0 <= many["p95"] <= 96.0 and many["max"] == 100.0
