"""Cluster scenario f: G4's R1 regression on real processes (0.3.14), plus LATE-1.

R1 (G4) was a dedupe key that accepted *replayed* reports, so after a lossy
reconnect a sample could be counted twice in the snapshot buckets V0.15 trains
on. The component harness could not reproduce the real shape - "controller stays
up, the agent's TCP drops, the agent reconnects and replays" - which is why R1
survived a round. Here the controller and the agent are real processes and the
drop is a real RST from the fault proxy.

How a replay of an *already counted* sample is forced: the link gets latency, so
every report's Ack comes back well after the report was counted. With one report
every 100 ms there are always counted-but-unacked reports; a ``reset_peer``
leaves them in the agent's replay ring and the next stream replays them. The test
proves this happened (non-vacuous) from both logs: the agent's
``agent.session.reconciled`` shows ``replayed > 0`` and the controller (at DEBUG)
logs ``controller.resources.duplicate_update`` for the replays it rejected.

How "the number of distinct samples the agent produced" is learned:

* The agent numbers every report with a per-process ``sequence`` that starts at 1
  and never resets (``agent/session/replay.py``); in V0.3 ``resource_update`` is
  the only report type, so the samples it produced are exactly ``1..N``.
* ``N`` comes from the agent's own log: ``agent.resources.stopped`` carries
  ``reports`` (samples handed to the session) when the agent is stopped with
  SIGINT at the end. The agent's ``agent.session.reconciled`` events say which
  sequences each reconnect replayed (``replay_from_sequence`` + ``replayed``).
* The last few of those ``N`` are never delivered: a report handed over during
  the graceful stop's close window is dropped (follow-up in the G6-B handoff). So
  ``M`` = the highest sequence the controller holds; the controller's live
  samples must be **exactly** ``{1..M}`` (no gap = nothing lost mid-run, no repeat
  = nothing double counted), and ``N - M`` must fit in the stop window measured
  from the agent's own ``stop_requested`` -> ``resources.stopped`` timestamps.

Buckets are then compared **per bucket** with the live samples' ``observed_at``.
"Force the flush": only closed buckets are written, so the test waits until the
bucket holding the last sample has ended plus three flush intervals.

LATE-1 (found here by G6-B; fixed in the G6 fix round by D26): a sample that
reached the controller *after its bucket was already flushed* was dropped from
history with no log line, although the live ring kept it - buckets closed at
``bucket_start + bucket_s`` with no allowance for delivery delay, so link latency
(100-300 ms here) and above all a reconnect's replay (seconds late) lost samples.
Buckets now close ``bucket_grace_s`` after they end (``GRACE_S`` here, sized to
this scenario's replay; 20 s by default) and a sample later than that is counted
and logged ``controller.resources.sample_late``. Scenario f still asserts the R1
property strictly (no bucket ever above its distinct samples) and bounds any loss
(every sample a bucket lacks arrived after its bucket ended); ``test_f2_...``
pins the replay case deterministically: the late replayed sample is now kept.
"""

from __future__ import annotations

import datetime as dt
import math
import time
from typing import Any

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")

from mf_cluster import FAST, Fabric, MakeFabric, Node

pytestmark = [pytest.mark.docker]

SAMPLE_S = 0.1
FLUSH_S = 0.2
GRACE_S = 3.0
"""``controller.resources.bucket_grace_s`` here (D26): covers test_f2's replay
(latency 300 ms + Hello round trip, ~1 s late) with margin; the shipped 20 s would
only make the run slower."""
RESETS = 4
RESET_SPACING_S = 0.4
EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


def _utc(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


def _bucket(moment: dt.datetime, bucket_s: float) -> dt.datetime:
    offset = (moment - EPOCH).total_seconds()
    return EPOCH + dt.timedelta(seconds=(offset // bucket_s) * bucket_s)


def _dense_fabric(make_fabric: MakeFabric, bucket_s: float) -> Fabric:
    return make_fabric(
        agents=1,
        timing=FAST,
        log_level="DEBUG",  # controller.resources.duplicate_update is DEBUG
        controller_resources={
            "bucket_s": bucket_s,
            "bucket_grace_s": GRACE_S,
            "flush_interval_s": FLUSH_S,
            "live_ring_size": 1000,
        },
        agent_resources={
            "sample_interval_s": SAMPLE_S,
            "max_report_interval_s": SAMPLE_S,  # every sample is a report
            "change_threshold_percent": 0.0,
        },
    )


def _reset_and_wait(fabric: Fabric, node: Node) -> None:
    before = len(node.process.log.events("agent.session.reconciled"))
    node.link.reset_peer()
    fabric.wait_log(node.process, "agent.session.reconciled", within=10.0, after=before)
    fabric.wait_state(node, "online", connected=True, within=10.0)


class Account:
    """What the agent says it produced vs what the controller holds, after a stop."""

    def __init__(self, fabric: Fabric, node: Node, bucket_s: float) -> None:
        proc = node.process
        stopped = fabric.wait_log(proc, "agent.resources.stopped", within=10.0)
        self.produced = int(stopped["reports"])
        requested = proc.log.events("agent.session.stop_requested")[0]
        self.stop_window_s = (
            _utc(stopped["timestamp"]) - _utc(requested["timestamp"])
        ).total_seconds()
        # V0.4 G4: a first connect sends a Reconcile too (it lists held attempts,
        # replays nothing); only reconnects are replays.
        self.replays = [
            r
            for r in proc.log.events("agent.session.reconciled")
            if not r.get("first_connect")
        ]
        self.replayed: set[int] = set()
        for r in self.replays:
            first = int(r["replay_from_sequence"])
            self.replayed.update(range(first, first + int(r["replayed"])))
        self.duplicates = [
            r
            for r in fabric.controller_records()
            if r.get("event") == "controller.resources.duplicate_update"
        ]
        body = fabric.resources(node, live_limit=1000, limit=1000)
        fetched_at = dt.datetime.now(dt.UTC)
        assert body is not None
        self.live: list[dict[str, Any]] = list(body["live"])
        self.bucket_s = bucket_s
        # age_s is the controller's monotonic age at fetch time, so the arrival
        # wall time is fetched_at - age_s (to within the REST round trip).
        self.arrived = {
            int(s["sequence"]): fetched_at - dt.timedelta(seconds=float(s["age_s"]))
            for s in self.live
        }
        self.sequences = [int(s["sequence"]) for s in self.live]
        self.highest = max(self.sequences)

        last = max(_utc(s["observed_at"]) for s in self.live)
        end = _bucket(last, bucket_s) + dt.timedelta(seconds=bucket_s)
        fabric.wait_until(
            lambda: (
                dt.datetime.now(dt.UTC)
                > end + dt.timedelta(seconds=GRACE_S + 3 * FLUSH_S)
            ),
            within=bucket_s + GRACE_S + 5.0,
            what="last bucket closed (after its grace) and flushed",
        )
        history = fabric.resources(node, limit=1000, live_limit=1)
        assert history is not None
        self.stored = {
            _utc(b["bucket_start"]): int(b["samples"]) for b in history["history"]
        }

    def expected(self) -> dict[dt.datetime, int]:
        out: dict[dt.datetime, int] = {}
        for s in self.live:
            start = _bucket(_utc(s["observed_at"]), self.bucket_s)
            out[start] = out.get(start, 0) + 1
        return out

    def late_replays(self) -> dict[dt.datetime, list[int]]:
        """Replayed samples that reached the controller after their bucket ended."""
        return self.late(replayed_only=True)

    def late(self, *, replayed_only: bool = False) -> dict[dt.datetime, list[int]]:
        """Samples that reached the controller after their bucket ended."""
        out: dict[dt.datetime, list[int]] = {}
        for s in self.live:
            seq = int(s["sequence"])
            start = _bucket(_utc(s["observed_at"]), self.bucket_s)
            end = start + dt.timedelta(seconds=self.bucket_s)
            if (seq in self.replayed or not replayed_only) and self.arrived[seq] > end:
                out.setdefault(start, []).append(seq)
        return out

    def describe(self) -> str:
        def fmt(b: dict[dt.datetime, int]) -> str:
            return ", ".join(f"{k:%H:%M:%S}={v}" for k, v in sorted(b.items()))

        late = {f"{k:%H:%M:%S}": v for k, v in self.late().items()}
        samples = "\n".join(
            f"  seq {s['sequence']} observed {_utc(s['observed_at']):%H:%M:%S.%f} "
            f"arrived {self.arrived[int(s['sequence'])]:%H:%M:%S.%f}"
            for s in self.live
        )
        return (
            f"produced {self.produced}, delivered 1..{self.highest}, "
            f"replayed {sorted(self.replayed)}\nstored   {fmt(self.stored)}\n"
            f"expected {fmt(self.expected())}\nlate arrivals {late}\n{samples}"
        )


def test_f_replayed_reports_are_never_counted_twice(make_fabric: MakeFabric) -> None:
    bucket_s = 5.0
    fabric = _dense_fabric(make_fabric, bucket_s)
    node = fabric.nodes[0]
    fabric.wait_until(
        lambda: (fabric.node_view(node) or {}).get("sample_age_s") is not None,
        within=10.0,
        what="first resource sample",
    )
    node.link.latency(100)  # acks ~200 ms behind: counted-but-unacked reports
    time.sleep(0.5)

    # RESETS resets spanning < bucket_s touch at most two buckets, so by
    # pigeonhole one bucket sees at least two resets.
    first_reset = time.monotonic()
    for i in range(RESETS):
        _reset_and_wait(fabric, node)
        if i < RESETS - 1:
            time.sleep(RESET_SPACING_S)
    assert time.monotonic() - first_reset < bucket_s
    node.link.latency(0)
    time.sleep(1.0)  # the last replay's acks land; steady ONLINE
    _at, code = node.stop_graceful()
    assert code == 0

    acc = Account(fabric, node, bucket_s)
    # Non-vacuous: replays happened, and some were of already counted samples.
    assert len(acc.replays) == RESETS
    assert acc.replayed, "no report was ever replayed: the scenario is vacuous"
    assert acc.duplicates, "no replayed report reached the controller twice"

    # The live ring: every delivered sequence exactly once, none missing.
    assert len(acc.sequences) == len(set(acc.sequences)), acc.describe()
    assert sorted(acc.sequences) == list(range(1, acc.highest + 1)), acc.describe()
    in_stop_window = math.ceil(acc.stop_window_s / SAMPLE_S) + 1
    assert 0 <= acc.produced - acc.highest <= in_stop_window, acc.describe()

    # THE R1 regression: no bucket ever holds more than its distinct samples.
    expected = acc.expected()
    late = acc.late()
    for start, count in acc.stored.items():
        assert count <= expected.get(start, 0), "double counted: " + acc.describe()
    # And every sample a bucket lacks reached the controller after that bucket
    # ended (LATE-1) - nothing is lost for any other reason.
    for start, want in expected.items():
        missing = want - acc.stored.get(start, 0)
        assert 0 <= missing <= len(late.get(start, [])), acc.describe()
    assert sum(acc.stored.values()) >= acc.highest - sum(len(v) for v in late.values())


def test_f2_replay_landing_after_its_bucket_was_flushed_is_kept(
    make_fabric: MakeFabric,
) -> None:
    """Deterministic LATE-1: reset right after a bucket boundary with 300 ms latency.

    Reports sent in the last 300 ms before the reset are still in the proxy's
    delay queue, which the reset discards: never delivered, so the replay is
    their first arrival. Some were observed before the boundary; the replay needs
    at least the Hello round trip (600 ms). Before D26 the flusher closed the old
    bucket within one flush interval (0.2 s) of the boundary and lost them; now it
    waits ``GRACE_S`` and they are counted.
    """
    bucket_s = 2.0
    fabric = _dense_fabric(make_fabric, bucket_s)
    node = fabric.nodes[0]
    fabric.wait_until(
        lambda: (fabric.node_view(node) or {}).get("sample_age_s") is not None,
        within=10.0,
        what="first resource sample",
    )
    node.link.latency(300)
    time.sleep(1.0)
    now = dt.datetime.now(dt.UTC)
    boundary = _bucket(now, bucket_s) + dt.timedelta(seconds=bucket_s)
    time.sleep((boundary - now).total_seconds() + 0.05)
    _reset_and_wait(fabric, node)
    node.link.latency(0)
    time.sleep(1.0)
    _at, code = node.stop_graceful()
    assert code == 0

    acc = Account(fabric, node, bucket_s)
    old_bucket = boundary - dt.timedelta(seconds=bucket_s)
    # The construction worked: a replayed sample of the old bucket arrived late.
    assert acc.late_replays().get(old_bucket), acc.describe()
    assert acc.stored.get(old_bucket) == acc.expected()[old_bucket], acc.describe()
