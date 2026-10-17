"""Event -> DOM latency while a steady job stream runs (skew-safe, V0.9 G0):
heartbeat samples timed from their arrival at the controller, ``job.state``
changes from the controller's transition time - both put on the browser's
clock with the measured offset (:mod:`.clocks`)."""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from meshforge.common.clock_offset import ClockOffset

from .clocks import clock_report, measure_clock
from .report import summary
from .scenarios import TERMINAL, Api, JobStream, wait_terminal

if TYPE_CHECKING:
    from playwright.sync_api import Page


def _parse_at(value: str) -> float:
    return datetime.fromisoformat(value).timestamp() * 1000.0


@dataclasses.dataclass
class HeartbeatSamples:
    """Heartbeat -> DOM latency, timed from the sample's ARRIVAL at the
    controller (V0.9 G0, D213). The page's "latest sample" changes are joined,
    by the sample's ``observed_at`` (an identifier here, never a time), to the
    ``node.metrics`` frames the page received; each frame's ``recv_at`` (the
    controller's clock) is put on the browser's clock with ``clock``. The
    agent's clock takes no part, so an agent whose clock is off by seconds
    gives the same numbers. A change with no frame (shown from a REST refetch,
    or the frame not seen) cannot be timed: counted in ``unmatched``."""

    lags: list[float] = dataclasses.field(default_factory=list)
    unmatched: int = 0

    @classmethod
    def build(
        cls,
        shown: Sequence[dict[str, Any]],
        received: dict[str, float],
        clock: ClockOffset,
    ) -> HeartbeatSamples:
        """``shown``: ``{"at": observed_at, "shown": browser epoch ms}`` per
        change of the page's latest sample (the first, already there when
        watching began, excluded by the caller); ``received``: observed_at ->
        ``recv_at`` (controller epoch seconds) of the node's frames."""
        out = cls()
        for change in shown:
            recv_at = received.get(str(change["at"]))
            if recv_at is None:
                out.unmatched += 1
                continue
            arrived = clock.to_local_ms(float(recv_at) * 1000.0)
            out.lags.append(float(change["shown"]) - arrived)
        return out


class MetricsFrames:
    """``node.metrics`` frames one page receives for one node: observed_at ->
    recv_at. Attach BEFORE the page opens its event socket (``page.goto``)."""

    def __init__(self, page: Page, node_id: str) -> None:
        self.node_id = node_id
        self.received: dict[str, float] = {}
        self.unreadable = 0
        page.on("websocket", self._socket)

    def _socket(self, ws: Any) -> None:
        if "/v1/events/ws" in ws.url:
            ws.on("framereceived", self.frame)

    def frame(self, payload: str | bytes) -> None:
        try:
            frame = json.loads(payload)
            if not isinstance(frame, dict) or frame.get("type") != "node.metrics":
                return
            data = frame["data"]
            if data.get("node_id") != self.node_id:
                return
            if data.get("recv_at") is None:
                self.unreadable += 1
                return
            self.received[str(data["observed_at"])] = float(data["recv_at"])
        except (ValueError, KeyError, TypeError, AttributeError):
            self.unreadable += 1


@dataclasses.dataclass
class JobStateSamples:
    """job.state -> DOM latency samples (G7 fix round 1, T-8). Every change
    is a sample: one the page showed is timed to when it showed it; one it
    never showed itself is timed to when it showed a LATER state of the job
    (``superseded`` - e.g. ``submitted`` and ``planning`` happen inside the
    submit call, so the list's first view of a job is ``queued``); one with no
    later state shown either is ``missed`` - a failure, never left out."""

    lags: list[float] = dataclasses.field(default_factory=list)
    terminal_lags: list[float] = dataclasses.field(default_factory=list)
    missed: list[str] = dataclasses.field(default_factory=list)
    superseded: int = 0
    missed_terminal: int = 0

    def add(
        self,
        job_id: str,
        transitions: Sequence[dict[str, Any]],
        shown: dict[str, float],
        clock: ClockOffset | None = None,
    ) -> None:
        """``transitions`` as the API lists them (oldest first, ``at`` on the
        controller's clock); ``shown`` maps a state to when the page first
        showed it (the browser's epoch ms); ``clock`` = controller minus
        browser (``None``: the same clock, as on one host)."""
        for index, transition in enumerate(transitions):
            state = transition["to_state"]
            at = _parse_at(transition["at"])
            if clock is not None:
                at = clock.to_local_ms(at)
            if state in shown:
                lag = shown[state] - at
                self.lags.append(lag)
                if state in TERMINAL:
                    self.terminal_lags.append(lag)
                continue
            later = [
                shown[t["to_state"]]
                for t in transitions[index + 1 :]
                if t["to_state"] in shown and shown[t["to_state"]] >= at
            ]
            if later:
                self.superseded += 1
                self.lags.append(min(later) - at)
                continue
            self.missed.append(f"{job_id}:{state}")
            if state in TERMINAL:
                self.missed_terminal += 1


def measure_events(
    jobs_page: Page,
    node_page: Page,
    base: str,
    api: Api,
    tree_id: str,
    node_id: str,
    *,
    min_jobs: int = 20,
    min_samples: int = 15,
    interval_s: float = 1.0,
    timeout_s: float = 240.0,
) -> dict[str, Any]:
    """Event -> DOM latency while a steady job stream runs (skew-safe: see
    the module docstring)."""
    clock = measure_clock(node_page)
    jobs_page.goto(base + "/jobs")
    jobs_page.locator("table").first.wait_for()
    jobs_page.evaluate(
        """() => {
          window.__mfSeen = {};
          const scan = () => {
            for (const r of document.querySelectorAll('table tbody tr')) {
              const a = r.querySelector('a[href^="/jobs/job_"]');
              if (!a || !r.cells[1]) continue;
              const id = a.getAttribute('href').split('/').pop();
              const st = r.cells[1].textContent.trim();
              const seen = (window.__mfSeen[id] ||= {});
              if (!(st in seen)) seen[st] = Date.now();
            }
          };
          scan();
          new MutationObserver(scan).observe(document.body,
            {subtree: true, childList: true, characterData: true, attributes: true});
        }"""
    )
    frames = MetricsFrames(node_page, node_id)
    node_page.goto(f"{base}/nodes/{node_id}")
    node_page.get_by_test_id("latest-sample").wait_for()
    node_page.evaluate(
        """() => {
          window.__mfLag = [];
          const card = document.querySelector('[data-testid="live-metrics"]');
          let last = null;
          const note = () => {
            const t = card.querySelector('[data-testid="latest-sample"]');
            const at = t && t.getAttribute("datetime");
            if (at && at !== last) {
              if (last !== null) window.__mfLag.push({at, shown: Date.now()});
              last = at;
            }
          };
          note();
          new MutationObserver(note).observe(card,
            {subtree: true, attributes: true, childList: true, characterData: true});
        }"""
    )
    stream = JobStream(api, tree_id, interval_s)
    stream.start()
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            samples = int(node_page.evaluate("window.__mfLag.length"))
            if len(stream.ids) >= min_jobs and samples >= min_samples:
                break
            jobs_page.wait_for_timeout(500)
    finally:
        stream.stop()
    ids = list(stream.ids)
    wait_terminal(api, ids, timeout_s)
    jobs_page.wait_for_timeout(3000)  # let the last events land in the page
    seen: dict[str, dict[str, float]] = jobs_page.evaluate("window.__mfSeen")
    states = JobStateSamples()
    for job_id in ids:
        states.add(job_id, api.job(job_id)["transitions"], seen.get(job_id, {}), clock)
    heartbeat = HeartbeatSamples.build(
        node_page.evaluate("window.__mfLag"), frames.received, clock
    )
    return {
        "clock": clock_report(clock),
        "stream_jobs": len(ids),
        "stream_errors": stream.errors,
        "job_state_latency": summary(states.lags),
        "job_terminal_latency": summary(states.terminal_lags),
        "job_terminal_missed": states.missed_terminal,
        "job_state_superseded": states.superseded,
        "job_state_missed": len(states.missed),
        "job_state_missed_examples": states.missed[:10],
        "heartbeat_latency": summary(heartbeat.lags),
        "heartbeat_unmatched": heartbeat.unmatched,
        "heartbeat_frames_unreadable": frames.unreadable,
    }
