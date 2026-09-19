"""Unit tests for ``lab/spikes/s1_network.py`` argument validation and the
``matrix`` subcommand. Every rejected invocation exits with 2 *before* any
socket is opened (bind safety, docs/LAB.md §6)."""

# ruff: noqa: S104 - wildcard addresses are test data

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from spikes import s1_network as cli
from spikes._harness.envelope import make_envelope, write_envelope
from spikes._harness.reps import CaseResult


def _usage_error(argv: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    return capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "needle"),
    [
        (["serve", "--bind", "0.0.0.0"], "wildcard"),
        (["serve", "--bind", "::"], "wildcard"),
        (["serve", "--bind", "8.8.8.8"], "non-private"),
        (["serve", "--bind", "my-laptop"], "needs an IP"),
        (["serve"], "--bind"),
        (["serve", "--bind", "127.0.0.1", "--port", "70000"], "between"),
        (["serve", "--bind", "127.0.0.1", "--idle", "0"], "between"),
        (["serve", "--bind", "127.0.0.1", "--name", "bad name!"], "characters"),
        (["client", "--server", "8.8.8.8", "--link", "wifi"], "not a private"),
        (["client", "--server", "0.0.0.0", "--link", "wifi"], "unicast"),
        (["client", "--server", "laptop2", "--link", "wifi"], "needs an IP"),
        (["client", "--server", "10.0.0.2", "--link", "lan"], "invalid choice"),
        (["client", "--server", "10.0.0.2"], "--link"),
        (
            ["client", "--server", "10.0.0.2", "--link", "wifi", "--streams", "0"],
            "1..16",
        ),
        (
            ["client", "--server", "10.0.0.2", "--link", "wifi", "--streams", "1,x"],
            "integers",
        ),
        (
            ["client", "--server", "10.0.0.2", "--link", "wifi", "--seconds", "0"],
            "between",
        ),
        (
            ["client", "--server", "10.0.0.2", "--link", "wifi", "--reps", "0"],
            "between",
        ),
        (
            ["client", "--server", "10.0.0.2", "--link", "wifi", "--ping-size", "10"],
            "between",
        ),
        (["matrix", "--results", "does-not-exist-xyz", "--out", "x.md"], "not found"),
        (["matrix", "--results", ".", "--out", "x.md", "--labels", "L1"], "LABEL=HOST"),
        (["bogus"], "invalid choice"),
    ],
)
def test_usage_errors_exit_2(
    argv: list[str], needle: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert needle in _usage_error(argv, capsys)


def test_streams_and_labels_parsing() -> None:
    assert cli._streams("4,1,4") == (1, 4)
    assert cli._labels("L1=hostA, L2=hostB") == {"hostA": "L1", "hostB": "L2"}
    with pytest.raises(argparse.ArgumentTypeError):
        cli._labels("L1=")


def test_check_server() -> None:
    assert cli._check_server("192.168.1.20", allow_any=False) == "192.168.1.20"
    assert cli._check_server("8.8.8.8", allow_any=True) == "8.8.8.8"


def test_report_day_from_folder_name() -> None:
    assert cli._report_day([Path("x/2026-09-20-S1-network")], None) == date(2026, 9, 20)
    assert cli._report_day([Path("x")], date(2026, 1, 2)) == date(2026, 1, 2)
    assert cli._report_day([Path("2026-13-40-bad")], None) == datetime.now(UTC).date()


def _write_run(folder: Path, client: str, server: str) -> None:
    def case(direction: str) -> CaseResult:
        sender, receiver = (
            (client, server) if direction == "upload" else (server, client)
        )
        return CaseResult(
            case_id=f"tcp-{direction}-x1",
            label=f"TCP {sender} → {receiver}",
            metrics={"receiver_mbps": "Mbit/s"},
            attrs={"kind": "tcp", "streams": 1, "sender": sender, "receiver": receiver},
            samples=[{"receiver_mbps": 500.0}] * 5,
        )

    env = make_envelope(
        spike="S1",
        topic="S1-network",
        link="ethernet",
        params={"client_name": client, "server_name": server},
        cases=[case("upload"), case("download")],
        git={"commit": "abc", "dirty": False},
        host={"hostname": client},
        now=datetime(2026, 9, 20, tzinfo=UTC),
    )
    write_envelope(env, folder / f"{client}__to-{server}_ethernet.json")


def test_matrix_writes_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "2026-09-20-S1-network"
    _write_run(results, "hostA", "hostB")
    (results / "junk.json").write_text("{", encoding="utf-8")
    out = tmp_path / "reports" / "2026-09-20-S1-network.md"
    code = cli.main(
        [
            "matrix",
            "--results",
            str(results),
            "--out",
            str(out),
            "--labels",
            "L1=hostA,L2=hostB",
        ]
    )
    assert code == 0
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# S1 network matrix — 2026-09-20")
    assert "| L1 | — | 500.0 ± 0.0 (n=5) |" in text
    assert "Skipped result files:" in text and "junk.json" in text
    assert "skipped invalid result file" in capsys.readouterr().err


def test_matrix_with_no_results_is_all_tbd(tmp_path: Path) -> None:
    out = tmp_path / "r.md"
    assert cli.main(["matrix", "--results", str(tmp_path), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "#### Wi-Fi" in text and "#### Ethernet" in text
    assert "(no results)" in text


def test_matrix_unwritable_output_exits_1(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    code = cli.main(
        ["matrix", "--results", str(tmp_path), "--out", str(blocker / "r.md")]
    )
    assert code == 1
