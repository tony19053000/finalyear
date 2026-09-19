"""Unit tests for ``lab/spikes/_s3/meters.py``: the server transfer log (parts,
waiting, eviction, thread-safety), the client meter and peak RSS."""

from __future__ import annotations

import threading
import time

from spikes._s3 import meters
from spikes._s3.meters import ClientMeter, Transfer, TransferLog


def test_transfer_log_single_part_records_bytes_crc_and_window() -> None:
    log = TransferLog()
    log.begin("t1", "tcp")
    time.sleep(0.01)
    log.end("t1", num_bytes=1000, crc_ok=True)
    item = log.get("t1")
    assert item is not None
    assert item.bytes == 1000 and item.parts == 1 and item.open_parts == 0
    assert item.crc_ok is True and item.error is None
    assert item.seconds >= 0.005 and item.cpu_s >= 0.0
    assert log.total_bytes == 1000 and log.total_transfers == 1
    doc = item.to_json()
    assert doc["transfer_id"] == "t1" and doc["transport"] == "tcp"
    assert doc["bytes"] == 1000 and doc["parts"] == 1
    assert "rss_peak_mib" in doc


def test_transfer_log_multi_part_merges_and_waits_for_all_parts() -> None:
    log = TransferLog()
    log.begin("h", "http")
    log.begin("h", "http")
    assert log.wait_for("h", parts=2, timeout_s=0.05) is not None  # returns partial
    item = log.get("h")
    assert item is not None and item.open_parts == 2 and item.parts == 0

    def finish() -> None:
        time.sleep(0.05)
        log.end("h", num_bytes=10, crc_ok=True)
        log.end("h", num_bytes=20, crc_ok=False, error="boom")

    threading.Thread(target=finish).start()
    done = log.wait_for("h", parts=2, timeout_s=5)
    assert done is not None
    assert done.parts == 2 and done.open_parts == 0 and done.bytes == 30
    assert done.crc_ok is False  # any failed part fails the transfer
    assert done.error == "boom"
    assert log.total_transfers == 1


def test_transfer_log_errors_are_joined_and_unknown_end_is_ignored() -> None:
    log = TransferLog()
    log.begin("e", "grpc")
    log.end("e", num_bytes=1, error="a")
    log.begin("e", "grpc")
    log.end("e", num_bytes=1, error="b")
    item = log.get("e")
    assert item is not None and item.error == "a; b"
    log.end("nope", num_bytes=5)  # no crash, nothing recorded
    assert log.get("nope") is None
    assert log.wait_for("nope", parts=1, timeout_s=0.01) is None


def test_transfer_log_is_bounded() -> None:
    log = TransferLog(max_transfers=3)
    for i in range(5):
        log.begin(f"t{i}", "tcp")
        log.end(f"t{i}", num_bytes=1)
    assert log.get("t0") is None and log.get("t1") is None
    assert all(log.get(f"t{i}") is not None for i in range(2, 5))
    assert log.total_transfers == 5


def test_transfer_seconds_never_negative_or_missing() -> None:
    t = Transfer("x", "tcp")
    assert t.seconds == 0.0 and t.cpu_s == 0.0
    t.t_first, t.t_last = 5.0, 4.0
    assert t.seconds == 0.0


def test_client_meter_windows() -> None:
    meter = ClientMeter()
    time.sleep(0.01)
    meter.first_byte()
    first = meter.t_first_byte
    meter.first_byte()  # idempotent
    assert meter.t_first_byte == first
    time.sleep(0.01)
    meter.stop()
    assert meter.wall_s >= 0.02
    assert meter.transfer_s <= meter.wall_s
    assert meter.ttfb_s is not None and 0 < meter.ttfb_s < meter.wall_s
    assert meter.cpu_s >= 0.0


def test_client_meter_without_first_byte() -> None:
    meter = ClientMeter()
    meter.stop()
    assert meter.ttfb_s is None
    assert meter.transfer_s == meter.wall_s
    assert meter.wall_s > 0


def test_peak_rss_is_positive_or_none() -> None:
    peak = meters.peak_rss_bytes()
    assert peak is None or peak > 1024 * 1024
    mib = meters.peak_rss_mib()
    assert mib is None or mib > 1.0
    assert meters.cpu_seconds() >= 0.0
