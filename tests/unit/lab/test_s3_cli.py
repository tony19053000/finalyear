"""Unit tests for ``lab/spikes/s3_transport.py`` argument validation, the case
plan built from a :class:`ClientConfig`, the hello-port resolution and the
``report`` subcommand on synthetic result files (no sockets are opened)."""

# ruff: noqa: S104 - wildcard addresses are test data

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from spikes import s3_transport as cli
from spikes._harness.envelope import load_envelope, make_envelope, write_envelope
from spikes._harness.reps import CaseResult
from spikes._s3.protocol import (
    CHUNK_SWEEP,
    DEFAULT_CHUNK_BYTES,
    DEFAULT_PORT,
    DEFAULT_SIZES,
    GRPC_PORT_OFFSET,
    HTTP_PORT_OFFSET,
    KIB,
    MIB,
)
from spikes._s3.run import METRICS, ClientConfig, build_cases


def _usage_error(argv: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    return capsys.readouterr().err


CLIENT = ["client", "--server", "10.0.0.2", "--link", "wifi"]


@pytest.mark.parametrize(
    ("argv", "needle"),
    [
        (["serve", "--bind", "0.0.0.0"], "wildcard"),
        (["serve", "--bind", "::"], "wildcard"),
        (["serve", "--bind", "8.8.8.8"], "non-private"),
        (["serve", "--bind", "my-laptop"], "needs an IP"),
        (["serve"], "--bind"),
        (["serve", "--bind", "127.0.0.1", "--port", "65535"], "between"),
        (["serve", "--bind", "127.0.0.1", "--port", "-1"], "between"),
        (["serve", "--bind", "127.0.0.1", "--idle", "0"], "between"),
        (["serve", "--bind", "127.0.0.1", "--header-timeout", "0"], "between"),
        (["serve", "--bind", "127.0.0.1", "--name", "bad name!"], "characters"),
        (["client", "--server", "8.8.8.8", "--link", "wifi"], "not a private"),
        (["client", "--server", "0.0.0.0", "--link", "wifi"], "unicast"),
        (["client", "--server", "224.0.0.1", "--link", "wifi"], "unicast"),
        (["client", "--server", "laptop2", "--link", "wifi"], "needs an IP"),
        (["client", "--server", "10.0.0.2", "--link", "lan"], "invalid choice"),
        (["client", "--server", "10.0.0.2"], "--link"),
        ([*CLIENT, "--sizes", "1KiB"], "outside"),
        ([*CLIENT, "--sizes", "x"], "not a byte size"),
        ([*CLIENT, "--sizes", ","], "no sizes"),
        ([*CLIENT, "--max-size", "8GiB"], "outside"),
        ([*CLIENT, "--chunk", "8MiB"], "chunk"),
        ([*CLIENT, "--chunk-sweep", "1KiB"], "outside"),
        ([*CLIENT, "--chunk-sweep", " , "], "no chunk sizes"),
        ([*CLIENT, "--range-parts", "0"], "between"),
        ([*CLIENT, "--range-parts", "17"], "between"),
        ([*CLIENT, "--reps", "0"], "between"),
        ([*CLIENT, "--reps", "1.5"], "not a number"),
        ([*CLIENT, "--pause", "-1"], "between"),
        ([*CLIENT, "--transfer-timeout", "0.5"], "between"),
        ([*CLIENT, "--ceiling-size", "1KiB"], "outside"),
        ([*CLIENT, "--ceiling-reps", "0"], "between"),
        ([*CLIENT, "--name", "a/b"], "characters"),
        (["report", "--results", "does-not-exist-xyz", "--out", "x.md"], "not found"),
        (["report", "--out", "x.md"], "--results"),
        (["bogus"], "invalid choice"),
        ([], "required"),
    ],
)
def test_usage_errors_exit_2(
    argv: list[str], needle: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert needle in _usage_error(argv, capsys)


def test_argument_type_helpers() -> None:
    assert cli._sizes("8MiB,1MiB") == (MIB, 8 * MIB)
    assert cli._chunks("1MiB,64KiB,1MiB") == (64 * KIB, MIB)
    assert cli._ceiling_size("0") == 0
    assert cli._ceiling_size("off") == 0
    assert cli._ceiling_size("8MiB") == 8 * MIB
    assert cli._port("0") == 0
    assert cli._port(str(65535 - HTTP_PORT_OFFSET)) == 65535 - HTTP_PORT_OFFSET
    with pytest.raises(argparse.ArgumentTypeError):
        cli._size("nope")
    with pytest.raises(argparse.ArgumentTypeError):
        cli._chunk("16MiB")
    assert cli._check_server(" 192.168.1.20 ", allow_any=False) == "192.168.1.20"
    assert cli._check_server("8.8.8.8", allow_any=True) == "8.8.8.8"
    with pytest.raises(ValueError, match="allow-any"):
        cli._check_server("8.8.8.8", allow_any=False)


def test_client_defaults_in_parser() -> None:
    args = cli._parser().parse_args(CLIENT)
    assert args.port == DEFAULT_PORT and args.reps == 5 and args.warmup == 1
    assert tuple(args.sizes) == DEFAULT_SIZES and args.max_size == max(DEFAULT_SIZES)
    assert args.chunk == DEFAULT_CHUNK_BYTES and tuple(args.chunk_sweep) == CHUNK_SWEEP
    assert args.chunk_sweep_size == 64 * MIB and args.range_parts == 4
    assert args.ceiling_size == cli.DEFAULT_CEILING_SIZE and args.ceiling_reps == 3
    assert args.no_uploads is False and args.transfer_timeout == 600.0


# ------------------------------------------------------------ case plan
def _cfg(**kw: Any) -> ClientConfig:
    base: dict[str, Any] = {"server": "10.0.0.2", "port": 47301, "link": "wifi"}
    base.update(kw)
    return ClientConfig(**base)


def test_build_cases_default_plan_ascends_and_sweeps_once() -> None:
    cfg = _cfg(sizes=(MIB, 8 * MIB, 64 * MIB), chunk_sweep_size=64 * MIB)
    ids = [c.case_id for c in build_cases(cfg)]
    assert ids[:6] == [
        "tcp-down-1MiB",
        "grpc-down-1MiB",
        "http-get-1MiB",
        "http-range4-1MiB",
        "tcp-up-1MiB",
        "grpc-up-1MiB",
    ]
    assert ids[6].endswith("8MiB") and ids[12].endswith("64MiB")
    # the sweep skips the default chunk (already measured as grpc-down-64MiB)
    assert ids[18:] == [
        "grpc-down-64MiB-chunk64KiB",
        "grpc-down-64MiB-chunk256KiB",
        "grpc-down-64MiB-chunk4MiB",
    ]
    assert len(ids) == len(set(ids))
    sweep = [c for c in build_cases(cfg) if c.attrs.get("sweep")]
    assert all(c.attrs["size_bytes"] == 64 * MIB for c in sweep)
    assert {c.attrs["chunk_bytes"] for c in sweep} == {64 * KIB, 256 * KIB, 4 * MIB}


def test_build_cases_respects_max_size_no_uploads_and_range_parts() -> None:
    cfg = _cfg(
        sizes=(MIB, 8 * MIB, 64 * MIB),
        max_size=8 * MIB,
        uploads=False,
        range_parts=2,
        chunk_sweep_size=64 * MIB,
    )
    cases = build_cases(cfg)
    ids = [c.case_id for c in cases]
    assert not any("-up-" in i for i in ids)
    assert "http-range2-1MiB" in ids and not any("64MiB" in i for i in ids)
    # the sweep moves down to the largest selected size
    assert "grpc-down-8MiB-chunk4MiB" in ids
    for c in cases:
        assert c.attrs["size_bytes"] <= 8 * MIB
        assert set(c.attrs) >= {"transport", "direction", "size_bytes", "chunk_bytes"}


def test_build_cases_sweep_at_unselected_size_includes_default_chunk() -> None:
    cfg = _cfg(sizes=(MIB, 64 * MIB), chunk_sweep_size=32 * MIB)
    sweep = [c for c in build_cases(cfg) if c.attrs.get("sweep")]
    assert {c.attrs["chunk_bytes"] for c in sweep} == set(CHUNK_SWEEP)
    assert all(c.attrs["size_bytes"] == 32 * MIB for c in sweep)


def test_tiny_max_size_keeps_smallest_size() -> None:
    cfg = _cfg(sizes=(MIB, 8 * MIB), max_size=4 * KIB)
    assert cfg.selected_sizes == (MIB,)
    assert build_cases(cfg)[0].case_id == "tcp-down-1MiB"


# --------------------------------------------------------- hello ports
def test_endpoint_follows_convention_then_hello() -> None:
    cfg = _cfg(port=47301)
    ep = cfg.endpoint
    assert (ep.tcp_port, ep.grpc_port, ep.http_port) == (
        47301,
        47301 + GRPC_PORT_OFFSET,
        47301 + HTTP_PORT_OFFSET,
    )
    real = cfg.endpoint_from_hello(
        {"ports": {"tcp": 47301, "grpc": 5000, "http": 6000}}
    )
    assert (real.tcp_port, real.grpc_port, real.http_port) == (47301, 5000, 6000)
    assert real.host == "10.0.0.2" and real.io_timeout_s == ep.io_timeout_s
    # a server without gRPC says port 0; garbage keeps the convention
    assert cfg.endpoint_from_hello({"ports": {"grpc": 0}}).grpc_port == 0
    assert cfg.endpoint_from_hello({"ports": {"grpc": True}}).grpc_port == 47302
    assert cfg.endpoint_from_hello({"ports": {"http": 70000}}).http_port == 47303
    assert cfg.endpoint_from_hello({"ports": "nope"}).http_port == 47303
    assert cfg.endpoint_from_hello({}).grpc_port == 47302


# -------------------------------------------------------------- envelope
def _synthetic_envelope(name: str = "cliA", **extra: Any) -> dict[str, Any]:
    result = CaseResult(
        case_id="tcp-down-1MiB",
        label="raw TCP download 1MiB",
        metrics=dict(METRICS),
        attrs={"transport": "tcp", "direction": "download", "size_bytes": MIB},
        requested_reps=5,
        requested_warmup=1,
        samples=[dict.fromkeys(METRICS, 1.0 + i) for i in range(5)],
    )
    return make_envelope(
        spike="S3",
        topic="S3-transport",
        link="wifi",
        params={"server_name": "srvB"},
        cases=[result],
        extra=extra,
        git={"commit": "abc1234", "dirty": False},
        host={"hostname": name, "os": "Linux"},
        host_label=name,
        now=datetime(2026, 9, 20, tzinfo=UTC),
    )


def test_synthetic_envelope_validates_and_sanitises(tmp_path: Path) -> None:
    doc = _synthetic_envelope(home=str(Path.home() / "secret"))
    assert doc["extra"]["home"] == "~/secret"
    path = tmp_path / "cliA__to-srvB_wifi.json"
    write_envelope(doc, path)
    loaded = load_envelope(path)
    assert loaded["spike"] == "S3" and "tcp-down-1MiB" in loaded["cases"]
    assert str(Path.home()) not in path.read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.tmp"))


def test_report_from_synthetic_results_is_deterministic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "2026-09-20-S3-transport"
    write_envelope(_synthetic_envelope(), results / "clia__to-srvb_wifi.json")
    (results / "junk.json").write_text("{not json", encoding="utf-8")
    out = tmp_path / "report.md"
    assert cli.main(["report", "--results", str(results), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# S3 transport throughput — 2026-09-20")
    assert "Roadmap task: 0.2.7" in text and "Spike: S3" in text
    assert "### Throughput by transport and payload size" in text
    assert "**cliA -> srvB · Wi-Fi**" in text
    assert "### Q2 answer" in text and "TBD" in text
    assert "- junk.json: invalid JSON" in text
    assert str(Path.home()) not in text and "aayush" not in text.lower()
    assert "skipped invalid result file" in capsys.readouterr().err
    assert cli.main(["report", "--results", str(results), "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == text
    assert not list(tmp_path.glob("*.tmp"))


def test_report_on_empty_folder_warns_and_uses_explicit_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "r.md"
    assert (
        cli.main(
            [
                "report",
                "--results",
                str(tmp_path),
                "--out",
                str(out),
                "--date",
                "2026-01-02",
            ]
        )
        == 0
    )
    assert "no S3 result files" in capsys.readouterr().err
    assert "— 2026-01-02" in out.read_text(encoding="utf-8")


def test_report_ignores_other_spikes(tmp_path: Path) -> None:
    doc = _synthetic_envelope()
    doc["spike"] = "S1"
    write_envelope(doc, tmp_path / "s1.json")
    out = tmp_path / "r.md"
    assert cli.main(["report", "--results", str(tmp_path), "--out", str(out)]) == 0
    assert "TBD (no results)" in out.read_text(encoding="utf-8")


def test_report_day_from_folder_name(tmp_path: Path) -> None:
    named = tmp_path / "2026-03-04-S3-transport"
    named.mkdir()
    assert cli._report_day([tmp_path, named], None).isoformat() == "2026-03-04"
    assert cli._report_day([named], None).isoformat() == "2026-03-04"
    explicit = datetime(2026, 5, 6, tzinfo=UTC).date()
    assert cli._report_day([named], explicit).isoformat() == "2026-05-06"
    bad = tmp_path / "2026-99-99-x"
    bad.mkdir()
    assert (
        cli._report_day([bad], None).isoformat() == datetime.now(UTC).date().isoformat()
    )
