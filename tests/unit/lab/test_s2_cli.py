"""Unit tests for ``lab/spikes/s2_crossos.py`` argument validation, the WSL
refusal and the ``report`` subcommand (roadmap 0.2.6). Every rejected
invocation exits before a socket is opened."""

# ruff: noqa: S104 - wildcard addresses are test data

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from spikes import s2_crossos as cli
from spikes._harness import MIN_VALID_REPS
from spikes._harness.envelope import make_envelope, write_envelope
from spikes._harness.reps import CaseResult
from spikes._s2.run import ClientRun
from spikes._s2.verdict import INVALID

_NATIVE = {
    "platform": "linux",
    "is_wsl": False,
    "wsl_version": None,
    "distro": None,
    "native": True,
    "evidence": {},
}
_WSL = {
    **_NATIVE,
    "is_wsl": True,
    "wsl_version": 2,
    "distro": "Ubuntu",
    "native": False,
}


@pytest.fixture(autouse=True)
def _native_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests must behave the same whether or not the developer is in WSL."""
    monkeypatch.setattr(cli, "wsl_status_here", lambda *_a, **_k: dict(_NATIVE))
    monkeypatch.setattr(cli, "firewall_profile", lambda: {"supported": False})


def _usage_error(argv: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == cli.EXIT_USAGE
    return capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "needle"),
    [
        (["serve", "--bind", "0.0.0.0"], "wildcard"),
        (["serve", "--bind", "::"], "wildcard"),
        (["serve", "--bind", "8.8.8.8"], "non-private"),
        (["serve", "--bind", "my-laptop"], "needs an IP"),
        (["serve"], "--bind"),
        (["serve", "--bind", "127.0.0.1", "--http-port", "70000"], "between"),
        (["serve", "--bind", "127.0.0.1", "--grpc-port", "-1"], "between"),
        (["serve", "--bind", "127.0.0.1", "--idle", "0"], "between"),
        (["serve", "--bind", "127.0.0.1", "--name", "bad name!"], "characters"),
        (["client", "--server", "8.8.8.8", "--link", "wifi"], "not a private"),
        (["client", "--server", "0.0.0.0", "--link", "wifi"], "unicast"),
        (["client", "--server", "224.0.0.1", "--link", "wifi"], "unicast"),
        (["client", "--server", "laptop2", "--link", "wifi"], "needs an IP"),
        (["client", "--server", "10.0.0.2", "--link", "lan"], "invalid choice"),
        (["client", "--server", "10.0.0.2"], "--link"),
        (["client", "--link", "wifi"], "--server"),
        (
            ["client", "--server", "10.0.0.2", "--link", "wifi", "--reps", "0"],
            "between",
        ),
        (
            ["client", "--server", "10.0.0.2", "--link", "wifi", "--blob-bytes", "0"],
            "between",
        ),
        (
            ["client", "--server", "10.0.0.2", "--link", "wifi", "--idle-gaps", "x"],
            "seconds expected",
        ),
        (
            ["client", "--server", "10.0.0.2", "--link", "wifi", "--idle-gaps", ""],
            "idle gap",
        ),
        (
            ["client", "--server", "10.0.0.2", "--link", "wifi", "--keepalive", "yes"],
            "on, off or both",
        ),
        (
            ["client", "--server", "10.0.0.2", "--link", "wifi", "--hold", "0"],
            "between",
        ),
        (["report", "--out", "x.md"], "--results"),
        (["report", "--results", "nope"], "--out"),
    ],
)
def test_bad_invocations_exit_two(
    argv: list[str], needle: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert needle in _usage_error(argv, capsys)


def test_no_subcommand_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert "command" in _usage_error([], capsys)


def test_report_rejects_a_missing_results_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [
        "report",
        "--results",
        str(tmp_path / "missing"),
        "--out",
        str(tmp_path / "r.md"),
    ]
    assert "not found" in _usage_error(argv, capsys)


@pytest.mark.parametrize("command", ["serve", "client"])
def test_wsl_is_refused_before_anything_is_bound(
    command: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "wsl_status_here", lambda *_a, **_k: dict(_WSL))
    argv = (
        ["serve", "--bind", "127.0.0.1"]
        if command == "serve"
        else ["client", "--server", "127.0.0.1", "--link", "wifi"]
    )
    assert cli.main(argv) == cli.EXIT_WSL
    err = capsys.readouterr().err
    assert "ADR-0009" in err


def test_allow_wsl_warns_but_continues(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "wsl_status_here", lambda *_a, **_k: dict(_WSL))
    status, refusal = cli._wsl_gate(allow_wsl=True)
    assert refusal is None
    assert status["is_wsl"] is True
    assert "does NOT validate ADR-0009" in capsys.readouterr().err


def test_firewall_warning_names_the_public_adapter(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli,
        "firewall_profile",
        lambda: {
            "supported": True,
            "profiles": [{"interface": "Wi-Fi", "category": "Public"}],
        },
    )
    cli._warn_firewall()
    err = capsys.readouterr().err
    assert "Wi-Fi" in err
    assert "Private" in err


def test_firewall_read_failure_is_reported_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli,
        "firewall_profile",
        lambda: {"supported": True, "error": "powershell: gone"},
    )
    cli._warn_firewall()
    assert "powershell: gone" in capsys.readouterr().err


# ------------------------------------------------------------------- report
def _write_result(directory: Path, client: str, server: str, *, survived: int) -> None:
    case = CaseResult(
        case_id="grpc-bidi-idle30s-kaoff",
        label="bidi",
        metrics={"survived": "0/1"},
        requested_reps=5,
        samples=[
            {
                "survived": survived,
                "failure": None
                if survived
                else {
                    "message": "no reply to session ping 6 within 20 s",
                    "fix": "re-run with --keepalive on",
                },
            }
            for _ in range(5)
        ],
    )
    ping = CaseResult(
        case_id="http-ping",
        label="ping",
        metrics={"ttfb_ms": "ms"},
        requested_reps=5,
        samples=[{"ttfb_ms": 1.0} for _ in range(5)],
    )
    doc = make_envelope(
        spike="S2",
        topic="S2-crossos",
        link="ethernet",
        params={"server_name": server, "server": "192.168.1.21"},
        cases=[ping, case],
        extra={
            "local": {"system": "Linux", "release": "6.8", "wsl": {"is_wsl": False}},
            "peer_identity": {
                "system": "Windows",
                "release": "11",
                "wsl": {"is_wsl": False},
            },
            "outbound_only": {"no_inbound_rule_declared": True},
        },
        host={"hostname": client, "os": "Linux", "os_release": "6.8"},
        host_label=client,
        git={"commit": "abc1234", "dirty": False},
        now=datetime(2026, 9, 19, tzinfo=UTC),
    )
    write_envelope(doc, directory / f"{client}__to-{server}.json")


def test_report_renders_verdicts_and_marks_untested_directions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "2026-09-19-S2-crossos"
    results.mkdir()
    _write_result(results, "L1", "L2", survived=1)
    out = tmp_path / "report.md"
    code = cli.main(
        [
            "report",
            "--results",
            str(results),
            "--out",
            str(out),
            "--hosts",
            "L1=L1,L2=L2,L3=L3",
        ]
    )
    assert code == cli.EXIT_OK
    text = out.read_text(encoding="utf-8")
    assert "S2 cross-OS reachability — 2026-09-19" in text
    assert "L1 -> L2" in text
    assert "L2 -> L1" in text  # untested direction
    assert "TBD" in text
    assert "Roadmap task: 0.2.6" in text
    assert "abc1234" in text
    assert "written" in capsys.readouterr().err


def test_report_without_results_still_produces_a_tbd_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "empty"
    results.mkdir()
    out = tmp_path / "report.md"
    assert cli.main(["report", "--results", str(results), "--out", str(out)]) == 0
    assert "no S2 result files" in capsys.readouterr().err
    assert "(no results)" in out.read_text(encoding="utf-8")


def test_report_skips_invalid_files_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "2026-09-19-S2-crossos"
    results.mkdir()
    _write_result(results, "L1", "L2", survived=0)
    (results / "broken.json").write_text("{not json", encoding="utf-8")
    out = tmp_path / "report.md"
    assert cli.main(["report", "--results", str(results), "--out", str(out)]) == 0
    err = capsys.readouterr().err
    assert "skipped invalid result file" in err
    text = out.read_text(encoding="utf-8")
    assert "broken.json" in text
    assert "--keepalive on" in text  # the idle-drop fix hint reached the report


def test_report_date_comes_from_the_folder_name(tmp_path: Path) -> None:
    results = tmp_path / "2026-01-02-S2-crossos"
    results.mkdir()
    assert cli._report_day([results], None) == date(2026, 1, 2)
    assert cli._report_day([tmp_path / "no-date"], None) == datetime.now(UTC).date()
    assert cli._report_day([results], date(2030, 5, 5)) == date(2030, 5, 5)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("both", ("on", "off")), ("on", ("on",)), ("off", ("off",))],
)
def test_keepalive_argument(text: str, expected: tuple[str, ...]) -> None:
    assert cli._keepalive(text) == expected


def test_idle_gaps_are_sorted_and_deduplicated() -> None:
    assert cli._gaps("120,30,30") == (30.0, 120.0)


def test_labels_parse_into_host_to_label(tmp_path: Path) -> None:
    assert cli._labels("L1=hostA,L2=hostB") == {"hostA": "L1", "hostB": "L2"}


def test_exit_codes_are_distinct() -> None:
    codes = [
        cli.EXIT_OK,
        cli.EXIT_IO,
        cli.EXIT_USAGE,
        cli.EXIT_INCOMPLETE,
        cli.EXIT_WSL,
        cli.EXIT_INTERRUPTED,
    ]
    assert len(set(codes)) == len(codes)


def test_client_config_estimate_is_sane() -> None:
    from spikes._s2.run import ClientConfig

    cfg = ClientConfig(
        server="10.0.0.2",
        http_port=1,
        grpc_port=2,
        link="wifi",
        reps=5,
        warmup=1,
        idle_gaps_s=(30.0, 120.0),
        keepalives=("on", "off"),
    )
    minutes = cfg.estimated_seconds() / 60
    assert 15 < minutes < 45


# ------------------------------------------- report: pairs, links, re-runs
def _result(
    directory: Path,
    client: str,
    server: str,
    *,
    link: str = "ethernet",
    survived: int = 1,
    reps: int = 5,
    when: datetime = datetime(2026, 9, 19, tzinfo=UTC),
    file_name: str | None = None,
) -> Path:
    """One S2 result file, written the way ``client`` writes it."""
    cases = [
        CaseResult(
            case_id="http-ping",
            label="ping",
            metrics={"ttfb_ms": "ms"},
            requested_reps=reps,
            samples=[{"ttfb_ms": 1.0} for _ in range(reps)],
        ),
        CaseResult(
            case_id="grpc-unary",
            label="unary",
            metrics={"rtt_ms": "ms"},
            requested_reps=reps,
            samples=[{"rtt_ms": 2.0} for _ in range(reps)],
        ),
        CaseResult(
            case_id="grpc-bidi-idle30s-kaon",
            label="bidi",
            metrics={"survived": "0/1"},
            requested_reps=reps,
            samples=[
                {
                    "survived": survived,
                    "failure": None
                    if survived
                    else {"message": "idle drop", "fix": "keepalive on"},
                }
                for _ in range(reps)
            ],
        ),
    ]
    doc = make_envelope(
        spike="S2",
        topic="S2-crossos",
        link=link,
        params={"server_name": server, "server": "192.168.1.21"},
        cases=cases,
        extra={
            "local": {"system": "Linux", "release": "6.8", "wsl": {"is_wsl": False}},
            "peer_identity": {
                "system": "Windows",
                "release": "11",
                "wsl": {"is_wsl": False},
            },
            "outbound_only": {"no_inbound_rule_declared": True},
        },
        host={"hostname": client, "os": "Linux", "os_release": "6.8"},
        host_label=client,
        git={"commit": "abc1234", "dirty": False},
        now=when,
    )
    path = directory / (file_name or f"{client}__to-{server}_{link}.json")
    write_envelope(doc, path)
    return path


def _verdict_rows(text: str) -> list[str]:
    block = text.split("#### Reachability verdicts")[1].split("#### Latency")[0]
    return [ln for ln in block.splitlines() if ln.startswith("|")][2:]


def _build(tmp_path: Path, results: Path, *extra: str) -> str:
    out = tmp_path / "report.md"
    assert (
        cli.main(["report", "--results", str(results), "--out", str(out), *extra]) == 0
    )
    return out.read_text(encoding="utf-8")


def test_report_holds_both_directions_of_a_pair(tmp_path: Path) -> None:
    results = tmp_path / "2026-09-19-S2-crossos"
    results.mkdir()
    _result(results, "L1", "L2")
    _result(results, "L2", "L1")
    rows = _verdict_rows(_build(tmp_path, results, "--hosts", "L1=L1,L2=L2"))
    assert [r.split("|")[1].strip() for r in rows] == ["L1 -> L2", "L2 -> L1"]
    assert not [r for r in rows if r.count("TBD") == r.count("|") - 2], (
        "a direction that was measured must not be rendered as untested"
    )


def test_report_keeps_the_two_links_apart(tmp_path: Path) -> None:
    results = tmp_path / "2026-09-19-S2-crossos"
    results.mkdir()
    _result(results, "L1", "L2", link="ethernet")
    _result(results, "L1", "L2", link="wifi", survived=0)
    rows = _verdict_rows(_build(tmp_path, results, "--hosts", "L1=L1,L2=L2"))
    by_link = {r.split("|")[2].strip(): r for r in rows if "L1 -> L2" in r}
    assert set(by_link) == {"Ethernet", "Wi-Fi"}
    assert "idle drop" in by_link["Wi-Fi"]
    assert "idle drop" not in by_link["Ethernet"]


def test_report_prefers_the_newest_of_two_runs_in_one_folder(tmp_path: Path) -> None:
    results = tmp_path / "2026-09-19-S2-crossos"
    results.mkdir()
    _result(
        results,
        "L1",
        "L2",
        survived=0,
        when=datetime(2026, 9, 19, tzinfo=UTC),
        file_name="a-first.json",
    )
    _result(
        results,
        "L1",
        "L2",
        survived=1,
        when=datetime(2026, 9, 20, tzinfo=UTC),
        file_name="b-second.json",
    )
    rows = _verdict_rows(_build(tmp_path, results))
    measured = [r for r in rows if r.split("|")[1].strip() == "L1 -> L2"]
    assert len(measured) == 1, "one row per direction and link, never one per file"
    assert "idle drop" not in measured[0], "the older, failed run must not win"


def test_report_marks_an_under_repeated_direction_invalid(tmp_path: Path) -> None:
    results = tmp_path / "2026-09-19-S2-crossos"
    results.mkdir()
    _result(results, "L1", "L2", reps=MIN_VALID_REPS - 1)
    text = _build(tmp_path, results)
    row = _verdict_rows(text)[0]
    assert row.count(INVALID) >= 3
    assert "| yes " not in row


def test_report_without_hosts_labels_uses_the_measured_ones(tmp_path: Path) -> None:
    results = tmp_path / "2026-09-19-S2-crossos"
    results.mkdir()
    _result(results, "L1", "L2")
    rows = _verdict_rows(_build(tmp_path, results))
    assert len(rows) == 2, "the reverse direction is still reported TBD"
    assert rows[1].split("|")[1].strip() == "L2 -> L1"


def test_report_reads_two_result_folders(tmp_path: Path) -> None:
    one, two = tmp_path / "wifi-run", tmp_path / "eth-run"
    one.mkdir()
    two.mkdir()
    _result(one, "L1", "L2", link="wifi")
    _result(two, "L1", "L2", link="ethernet")
    out = tmp_path / "report.md"
    assert (
        cli.main(
            [
                "report",
                "--results",
                str(one),
                "--results",
                str(two),
                "--out",
                str(out),
                "--date",
                "2026-09-21",
            ]
        )
        == cli.EXIT_OK
    )
    text = out.read_text(encoding="utf-8")
    assert "2026-09-21" in text
    assert len(_verdict_rows(text)) == 3  # both links + the untested direction


def test_report_ignores_another_spikes_results(tmp_path: Path) -> None:
    results = tmp_path / "2026-09-19-mixed"
    results.mkdir()
    _result(results, "L1", "L2")
    other = make_envelope(
        spike="S3",
        topic="S3-transport",
        link="ethernet",
        params={},
        cases=[],
        host={"hostname": "L9", "os": "Linux"},
        host_label="L9",
        git={"commit": "abc1234", "dirty": False},
        now=datetime(2026, 9, 19, tzinfo=UTC),
    )
    write_envelope(other, results / "s3.json")
    text = _build(tmp_path, results)
    assert "L9" not in text


def test_report_reports_an_unwritable_output_as_exit_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "2026-09-19-S2-crossos"
    results.mkdir()
    _result(results, "L1", "L2")
    blocked = tmp_path / "file.md" / "report.md"
    blocked.parent.write_text("not a folder", encoding="utf-8")
    code = cli.main(["report", "--results", str(results), "--out", str(blocked)])
    assert code == cli.EXIT_IO
    assert "cannot write" in capsys.readouterr().err


def test_report_fails_loudly_on_a_broken_template(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "2026-09-19-S2-crossos"
    results.mkdir()
    _result(results, "L1", "L2")
    template = tmp_path / "broken.md"
    template.write_text("# nothing to fill\n", encoding="utf-8")
    code = cli.main(
        [
            "report",
            "--results",
            str(results),
            "--out",
            str(tmp_path / "r.md"),
            "--template",
            str(template),
        ]
    )
    assert code == cli.EXIT_IO
    assert "cannot build the report" in capsys.readouterr().err
    assert not (tmp_path / "r.md").exists()


# ------------------------------------------------------------ client summary
def test_print_summary_names_every_case_and_the_fixes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _result(tmp_path, "L1", "L2", survived=0)
    doc = json.loads(path.read_text(encoding="utf-8"))
    run = ClientRun(envelope=doc, client_name="L1", server_name="L2", complete=False)
    cli.print_summary(run)
    err = capsys.readouterr().err
    assert "S2 verdict" in err
    assert "http-ping" in err
    assert "outbound-only" in err
    assert "what to fix" in err
    assert "idle drop" in err
