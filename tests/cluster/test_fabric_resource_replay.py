"""Cluster scenario f: G4's R1 regression on real processes (0.3.14), plus LATE-1.

R1 (G4) was a dedupe key that accepted *replayed* reports, so after a lossy
reconnect a sample could be counted twice in the snapshot buckets V0.15 trains
on. The component harness could not reproduce the real shape - "controller stays
up, the agent's TCP drops, the agent reconnects and replays" - which is why R1
survived a round. Here the controller and the agent are real processes and the
drop is a real RST from the fault proxy.

How a replay of an *already counted* sample is forced: the link delays only the
controller -> agent direction (V0.7 G4 fix round 1, D157), so a report reaches
the controller at once and is counted, while its Ack comes back ``ACK_DELAY_MS``
later. Every report sent in the last ``ACK_DELAY_MS`` before a ``reset_peer`` is
therefore counted but unacked; the reset leaves it in the agent's replay ring
and the next stream replays it. (Until D157 the latency was 100 ms in *both*
directions: then only reports sent in a 100 ms window - (T-200 ms, T-100 ms] -
were counted-but-unacked, while the agent really reports every ~230 ms (the
100 ms interval plus the sample itself) and is ONLINE only ~0.4 s between
resets, so ~1 run in 4-8 had no such report at any of the four resets and the
non-vacuity check failed - on the pre-G4 baseline ``c15a57d`` too, 2/8.) The test
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
GRACE_S = 6.0
"""``controller.resources.bucket_grace_s`` here (D26): covers test_f2's replay
(latency 300 ms + since V0.7 G3 the TCP + TLS 1.3 handshakes + HTTP/2 setup +
Hello round trips through that latency, ~2-3 s late) with margin; the shipped 20 s would
only make the run slower."""
RESETS = 4
RESET_SPACING_S = 0.4
ACK_DELAY_MS = 300.0
"""Controller -> agent latency in test_f: longer than one report period
(~230 ms), shorter than the ONLINE window between resets (D157)."""
FRESH_S = 0.1
"""test_f resets only when the controller's newest sample arrived less than this
ago (V0.7 G5, D158 F6): its Ack is then still in the proxy's delay queue."""
UNACKED_BUDGET_S = ACK_DELAY_MS / 1000.0 - 0.1
"""The sample's age at the reset must stay below this (100 ms margin under the
Ack delay), else the attempt is not used and the test waits for the next one."""
EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


def _utc(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


def _bucket(moment: dt.datetime, bucket_s: float) -> dt.datetime:
    offset = (moment - EPOCH).total_seconds()
    return EPOCH + dt.timedelta(seconds=(offset // bucket_s) * bucket_s)


def _dense_fabric(
    make_fabric: MakeFabric, bucket_s: float, *, cache_reports: bool = True
) -> Fabric:
    return make_fabric(
        # V0.5 G4: cache reports share the per-process report sequence; the
        # exact 1..N accounting of test_f needs resource reports only.
        agent_extra={} if cache_reports else {"cache": {"enabled": False}},
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


def _reset_with_a_counted_unacked_report(fabric: Fabric, node: Node) -> int:
    """Reset only once a report is counted but provably unacked (D158 F6).

    Poll the controller's newest live sample; when it arrived less than
    ``FRESH_S`` ago, reset at once - the controller counted it, and its Ack (sent
    after the count) is still ``ACK_DELAY_MS`` away in the proxy's delay queue,
    which the reset discards. The time from the fetch to the reset is measured;
    if the sample would have been older than ``UNACKED_BUDGET_S`` by then (a slow
    REST call) the reset is not done and the next report is used. Returns the
    sequence that must be replayed as a duplicate."""
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        fetched = time.monotonic()
        body = fabric.resources(node, live_limit=1, limit=1)
        live = [] if body is None else body["live"]
        if live and float(live[-1]["age_s"]) < FRESH_S:
            age_at_reset = float(live[-1]["age_s"]) + (time.monotonic() - fetched)
            if age_at_reset < UNACKED_BUDGET_S:
                sequence = int(live[-1]["sequence"])
                _reset_and_wait(fabric, node)
                return sequence
        time.sleep(0.01)
    fabric.fail(f"{node.name}: no fresh counted report to reset on within 15 s")


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

    def replays_with_duplicates(self) -> int:
        """Reconnects that replayed at least one already counted report."""
        dups = {int(d["sequence"]) for d in self.duplicates}
        return sum(
            1
            for r in self.replays
            if any(
                int(r["replay_from_sequence"])
                <= seq
                < int(r["replay_from_sequence"]) + int(r["replayed"])
                for seq in dups
            )
        )

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
    # V0.7 G3: each reconnect also does a TLS handshake; D157: every server ->
    # client flight of it is ACK_DELAY_MS late, ~1 s per reconnect.
    bucket_s = 12.0
    fabric = _dense_fabric(make_fabric, bucket_s, cache_reports=False)
    node = fabric.nodes[0]
    fabric.wait_until(
        lambda: (fabric.node_view(node) or {}).get("sample_age_s") is not None,
        within=10.0,
        what="first resource sample",
    )
    # Reports are counted at once, their Acks arrive ACK_DELAY_MS later: the
    # reports of the last ACK_DELAY_MS before each reset are counted-but-unacked.
    node.link.latency(ACK_DELAY_MS, direction="down")
    time.sleep(0.5)

    # RESETS resets spanning < bucket_s touch at most two buckets, so by
    # pigeonhole one bucket sees at least two resets. D158 F6: each reset waits
    # for a counted-but-unacked report, so every reconnect must replay one.
    forced: list[int] = []
    first_reset = time.monotonic()
    for i in range(RESETS):
        forced.append(_reset_with_a_counted_unacked_report(fabric, node))
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
    # D158 F6: deterministic by construction - every reset happened while a
    # counted report was unacked, so every reconnect replayed it and the
    # controller rejected it as a duplicate.
    dup_sequences = {int(d["sequence"]) for d in acc.duplicates}
    assert set(forced) <= acc.replayed, (forced, acc.describe())
    assert set(forced) <= dup_sequences, (forced, sorted(dup_sequences))
    assert acc.replays_with_duplicates() == RESETS, acc.describe()

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
