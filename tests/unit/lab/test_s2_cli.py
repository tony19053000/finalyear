"""Unit tests for ``lab/spikes/s2_crossos.py`` argument validation, the WSL
refusal and the ``report`` subcommand (roadmap 0.2.6). Every rejected
invocation exits before a socket is opened."""

# ruff: noqa: S104 - wildcard addresses are test data

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from spikes import s2_crossos as cli
from spikes._harness.envelope import make_envelope, write_envelope
from spikes._harness.reps import CaseResult

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
